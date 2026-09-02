import hashlib
import io
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.config import settings


logger = logging.getLogger(__name__)


class PDFVisionService:
    """筛选 PDF 中的功能性图片页，保存页面图并生成视觉说明。"""

    ACTION_KEYWORDS = {
        "install", "installation", "connect", "remove", "insert", "press",
        "click", "select", "open", "close", "turn on", "power on", "set",
        "draw", "edit", "map", "task", "schedule", "route", "area", "zone",
        "battery", "pedal", "button", "screen", "interface", "workstation",
        "安装", "连接", "拆卸", "插入", "点击", "选择", "打开", "关闭",
        "设置", "绘制", "编辑", "地图", "任务", "路线", "区域", "电池",
        "按钮", "界面", "工作站", "注意",
    }

    @classmethod
    def _importance_score(cls, page: Dict[str, Any]) -> float:
        text = str(page.get("text") or "")
        lowered = text.lower()
        image_count = int(page.get("image_count") or 0)
        keyword_hits = sum(1 for word in cls.ACTION_KEYWORDS if word in lowered)
        numbered_steps = len(re.findall(r"(?m)^\s*(?:\d+[.)、]|[①②③④⑤⑥])", text))
        short_visual_page = image_count >= 3 and len(text.strip()) < 1800
        substantive_images = page.get("substantive_images") or []
        return (
            min(image_count, 12) * 0.45
            + min(keyword_hits, 8) * 1.1
            + min(numbered_steps, 6) * 0.8
            + (2.0 if short_visual_page else 0.0)
            + (5.0 if substantive_images else 0.0)
        )

    @classmethod
    def select_important_pages(
        cls,
        pages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """根据图片数量、操作词和步骤编号选择功能性图片页。"""
        scored = []
        for page in pages:
            if int(page.get("image_count") or 0) <= 0:
                continue
            score = cls._importance_score(page)
            if score >= settings.pdf_image_importance_threshold:
                scored.append((score, int(page.get("page_number") or 0), page))

        scored.sort(key=lambda item: (-item[0], item[1]))
        # 大面积业务图片属于原文内容，必须完整保留；页数上限只约束普通页面截图。
        substantive = [item for item in scored if item[2].get("substantive_images")]
        substantive_pages = {item[1] for item in substantive}
        optional = [item for item in scored if item[1] not in substantive_pages]
        optional_limit = max(settings.pdf_image_max_pages - len(substantive), 0)
        selected = substantive + optional[:optional_limit]
        return [item[2] for item in sorted(selected, key=lambda item: item[1])]

    @staticmethod
    def _storage_root() -> Path:
        root = Path(settings.pdf_image_storage_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def _render_pages(
        cls,
        content: bytes,
        candidates: List[Dict[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        if not settings.pdf_image_assets_enabled or not candidates:
            return {}

        try:
            import pdfplumber
        except ImportError as exc:
            logger.warning(f"PDF 页面图片依赖未安装，跳过重要图片保存: {exc}")
            return {}

        file_hash = hashlib.sha256(content).hexdigest()
        target_dir = cls._storage_root() / file_hash
        target_dir.mkdir(parents=True, exist_ok=True)
        dpi = min(max(settings.pdf_image_dpi, 96), 220)
        result: Dict[int, Dict[str, Any]] = {}

        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page_info in candidates:
                    page_number = int(page_info.get("page_number") or 0)
                    if not 1 <= page_number <= len(pdf.pages):
                        continue
                    page = pdf.pages[page_number - 1]
                    rendered_page = page.to_image(
                        resolution=dpi,
                        antialias=True,
                    ).original.convert("RGB")
                    saved_images = []
                    for detail in page_info.get("substantive_images") or []:
                        bbox = detail.get("bbox") or []
                        image_index = int(detail.get("image_index") or 0)
                        if len(bbox) != 4 or image_index < 1:
                            continue
                        scale_x = rendered_page.width / max(float(page.width), 1.0)
                        scale_y = rendered_page.height / max(float(page.height), 1.0)
                        left = max(0, round(float(bbox[0]) * scale_x) - 4)
                        top = max(0, round(float(bbox[1]) * scale_y) - 4)
                        right = min(rendered_page.width, round(float(bbox[2]) * scale_x) + 4)
                        bottom = min(rendered_page.height, round(float(bbox[3]) * scale_y) + 4)
                        if right - left < 80 or bottom - top < 50:
                            continue
                        target = target_dir / f"page-{page_number}-image-{image_index}.jpg"
                        if not target.exists():
                            image = rendered_page.crop((left, top, right, bottom))
                            max_width = max(settings.pdf_image_max_width, 800)
                            if image.width > max_width:
                                height = round(image.height * max_width / image.width)
                                image = image.resize((max_width, height))
                            image.save(target, format="JPEG", quality=90, optimize=True)
                        saved_images.append({
                            "path": target,
                            "url": (
                                f"/api/upload/pdf-pages/{file_hash}/"
                                f"page-{page_number}-image-{image_index}.jpg"
                            ),
                            "kind": "embedded",
                        })

                    if not saved_images:
                        target = target_dir / f"page-{page_number}.jpg"
                        if not target.exists():
                            image = rendered_page
                            max_width = max(settings.pdf_image_max_width, 800)
                            if image.width > max_width:
                                height = round(image.height * max_width / image.width)
                                image = image.resize((max_width, height))
                            image.save(target, format="JPEG", quality=88, optimize=True)
                        saved_images.append({
                            "path": target,
                            "url": f"/api/upload/pdf-pages/{file_hash}/page-{page_number}.jpg",
                            "kind": "page",
                        })

                    primary = saved_images[0]
                    result[page_number] = {
                        "path": primary["path"],
                        "url": primary["url"],
                        "kind": primary["kind"],
                        "images": saved_images,
                        "score": round(cls._importance_score(page_info), 2),
                    }
        except Exception as exc:
            logger.warning(f"PDF 重要图片保存失败: {exc}")
        return result

    @classmethod
    def _vision_page_limit(
        cls,
        total_pages: int,
        candidate_count: int,
    ) -> int:
        """根据 PDF 总页数动态计算视觉分析页数，并保留安全上限。"""
        total_pages = max(int(total_pages or 0), 0)
        candidate_count = max(int(candidate_count or 0), 0)
        if total_pages == 0 or candidate_count == 0:
            return 0

        ratio = min(max(float(settings.pdf_vision_page_ratio), 0.0), 1.0)
        minimum = max(int(settings.pdf_vision_min_pages), 0)
        hard_maximum = max(int(settings.pdf_vision_hard_max_pages), 0)
        dynamic_limit = max(int(math.ceil(total_pages * ratio)), minimum)
        if hard_maximum > 0:
            dynamic_limit = min(dynamic_limit, hard_maximum)
        return min(dynamic_limit, total_pages, candidate_count)

    @classmethod
    def _select_vision_candidates(
        cls,
        candidates: List[Dict[str, Any]],
        total_pages: int | None = None,
    ) -> List[Dict[str, Any]]:
        """兼顾重要性与文档前中后段覆盖，避免只分析图片最多的章节。"""
        if total_pages is None:
            total_pages = max(
                (int(page.get("page_number") or 0) for page in candidates),
                default=0,
            )
        limit = cls._vision_page_limit(total_pages, len(candidates))
        if limit == 0 or not candidates:
            return []
        if len(candidates) <= limit:
            return candidates

        layout_keywords = (
            "map", "area", "zone", "route", "arrow", "button", "screen",
            "地图", "区域", "路线", "箭头", "按钮", "界面",
        )
        screenshot_like = []
        for page in candidates:
            text = str(page.get("text") or "")
            image_count = int(page.get("image_count") or 0)
            is_dense_screenshot = image_count >= 4 and len(text) < 450
            has_layout_semantics = image_count >= 3 and any(
                keyword in text.lower() for keyword in layout_keywords
            )
            if is_dense_screenshot or has_layout_semantics:
                screenshot_like.append(page)
        screenshot_like.sort(key=lambda page: int(page.get("page_number") or 0))
        selected: List[Dict[str, Any]] = screenshot_like[:limit]
        selected_pages = {
            int(page.get("page_number") or 0) for page in selected
        }
        if len(selected) >= limit:
            return selected

        max_page = max(int(page.get("page_number") or 0) for page in candidates)
        bucket_count = min(6, max_page, limit)
        bucket_size = max(1, (max_page + bucket_count - 1) // bucket_count)
        per_bucket = max(1, limit // bucket_count)
        bucket_picks: List[Dict[str, Any]] = []
        bucket_seen = set(selected_pages)
        for bucket_index in range(bucket_count):
            start = bucket_index * bucket_size + 1
            end = min(max_page, (bucket_index + 1) * bucket_size)
            bucket = [
                page for page in candidates
                if start <= int(page.get("page_number") or 0) <= end
            ]
            bucket.sort(key=lambda page: cls._importance_score(page), reverse=True)
            for page in bucket[:per_bucket]:
                page_number = int(page.get("page_number") or 0)
                if page_number not in bucket_seen:
                    bucket_picks.append(page)
                    bucket_seen.add(page_number)

        bucket_picks.sort(key=lambda page: cls._importance_score(page), reverse=True)
        chosen_bucket_picks = bucket_picks[:max(0, limit - len(selected))]
        selected.extend(chosen_bucket_picks)
        selected_pages.update(
            int(page.get("page_number") or 0) for page in chosen_bucket_picks
        )

        if len(selected) < limit:
            remaining = [
                page for page in candidates
                if int(page.get("page_number") or 0) not in selected_pages
            ]
            remaining.sort(key=lambda page: cls._importance_score(page), reverse=True)
            selected.extend(remaining[:limit - len(selected)])
        return sorted(selected[:limit], key=lambda page: int(page.get("page_number") or 0))

    @classmethod
    def _analyze_image(
        cls,
        image_path: Path,
        page_number: int,
        source_text: str,
    ) -> Dict[str, Any] | None:
        """委托视觉 Provider：VLM 优先、不可用自动退化 OCR（P1-BE-06）。"""
        from app.core.vision_provider import get_vision_provider

        return get_vision_provider().analyze(image_path, page_number, source_text)

    @classmethod
    def analyze_important_pages(
        cls,
        content: bytes,
        pages: List[Dict[str, Any]],
    ) -> Tuple[Dict[int, Dict[str, Any]], int, int]:
        """返回重要图片页、候选页数量和成功生成说明的页数。"""
        candidates = cls.select_important_pages(pages)
        assets = cls._render_pages(content, candidates)
        analyzed = 0
        vision_candidates = cls._select_vision_candidates(candidates, len(pages))
        worker_count = min(max(settings.pdf_vision_concurrency, 1), 4)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for page_info in vision_candidates:
                page_number = int(page_info.get("page_number") or 0)
                asset = assets.get(page_number)
                if not asset:
                    continue
                future = executor.submit(
                    cls._analyze_image,
                    asset["path"],
                    page_number,
                    str(page_info.get("text") or ""),
                )
                futures[future] = (page_number, asset)
            for future in as_completed(futures):
                page_number, asset = futures[future]
                try:
                    analysis = future.result()
                except Exception as exc:
                    logger.warning(f"PDF 第 {page_number} 页视觉理解线程失败: {exc}")
                    continue
                if analysis:
                    asset["analysis"] = analysis
                    analyzed += 1
                    logger.info(f"PDF 第 {page_number} 页视觉功能说明已生成")
                else:
                    logger.info(f"PDF 第 {page_number} 页保留原图，视觉说明待补充")
        return assets, len(candidates), analyzed

    @staticmethod
    def to_markdown(page_number: int, asset: Dict[str, Any]) -> str:
        """把页面图片及结构化视觉说明转换为 Markdown。"""
        parts = [
            "### 本页重要业务图片",
        ]
        images = asset.get("images") or [{
            "url": asset["url"],
            "kind": asset.get("kind") or "page",
        }]
        for image_index, image in enumerate(images, 1):
            if image.get("kind") == "embedded":
                alt = f"第 {page_number} 页业务图片 {image_index}"
            else:
                alt = f"第 {page_number} 页重要操作图"
            parts.append(f"![{alt}]({image['url']})")
        analysis = asset.get("analysis")
        if not analysis:
            parts.append(
                "> 已保留本页具有业务内容的原图；当前未生成视觉说明，请结合原图核对。"
            )
            return "\n\n".join(parts)

        image_type = str(analysis.get("image_type") or "功能性图片").strip()
        summary = str(analysis.get("summary") or "").strip()
        confidence = str(analysis.get("confidence") or "unknown").strip()
        parts.append(f"> 图片类型：{image_type}；视觉分析置信度：{confidence}")
        if summary:
            parts.append(f"**图片功能：** {summary}")

        steps = [str(item).strip() for item in analysis.get("steps") or [] if str(item).strip()]
        if steps:
            parts.append("**图片操作顺序：**\n\n" + "\n".join(
                f"{index}. {step}" for index, step in enumerate(steps, 1)
            ))

        callouts = []
        for item in analysis.get("callouts") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "标记").strip()
            target = str(item.get("target") or "").strip()
            function = str(item.get("function") or "").strip()
            detail = "；".join(value for value in (target, function) if value)
            if detail:
                callouts.append(f"- {label}：{detail}")
        if callouts:
            parts.append("**标号与箭头对应关系：**\n\n" + "\n".join(callouts))

        relations = [
            str(item).strip()
            for item in analysis.get("spatial_relations") or []
            if str(item).strip()
        ]
        if relations:
            parts.append("**区域与位置关系：**\n\n" + "\n".join(
                f"- {item}" for item in relations
            ))

        warnings = [
            str(item).strip()
            for item in analysis.get("warnings") or []
            if str(item).strip()
        ]
        if warnings:
            parts.append("**图片核对提示：**\n\n" + "\n".join(
                f"- {item}" for item in warnings
            ))
        return "\n\n".join(parts)
