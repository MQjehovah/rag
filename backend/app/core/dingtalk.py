import asyncio
import hashlib
import io
import json
import logging
import math
import mimetypes
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Awaitable

from alibabacloud_dingtalk.wiki_2_0.client import Client as WikiClient
from alibabacloud_dingtalk.wiki_2_0 import models as wiki_models
from alibabacloud_dingtalk.storage_1_0.client import Client as StorageClient
from alibabacloud_dingtalk.storage_1_0 import models as storage_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
import httpx

from app.config import settings
from app.core.dingtalk_storage import DingTalkLocalStorage

logger = logging.getLogger(__name__)

API_CALL_INTERVAL = 0.15
MAX_RETRIES = 3

WIKI_EXTENSIONS = {"", "wiki", "mindmap", "note"}
SKIP_EXTENSIONS = {
    "mp4", "avi", "mov", "mkv", "mp3", "wav",
    "exe", "dll", "bin", "pak", "dat",
    "jpg", "jpeg", "png", "gif", "bmp",
    "zip", "rar", "7z", "tar", "gz",
    "apk", "ipa", "dlink",
}


@dataclass(frozen=True)
class PDFMarkdownResult:
    """PDF增强解析生成的正文及可写入同步清单的统计信息。"""

    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class DingTalkClient:
    def __init__(self):
        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        self.wiki = WikiClient(config)
        self.storage = StorageClient(config)
        self._access_token: str = ""
        self._http = httpx.AsyncClient(timeout=30.0)
        self._last_call = 0.0
        self._dentry_cache: Dict[str, Dict[str, str]] = {}
        self._on_progress: Optional[Callable] = None
        self._collected_count: int = 0

    @staticmethod
    def supported_extensions() -> set[str]:
        """读取当前配置允许下载并转换的文件扩展名。"""
        return {
            item.strip().lower().lstrip(".")
            for item in settings.dingtalk_supported_extensions.split(",")
            if item.strip()
        }

    @staticmethod
    def _target_workspace_id(requested_workspace_id: str = None) -> Optional[str]:
        """解析同步范围；默认只允许同步已配置的知识库。"""
        if requested_workspace_id:
            return requested_workspace_id

        scope = settings.dingtalk_sync_scope.strip().lower()
        if scope == "all_authorized":
            return None
        if scope != "configured":
            raise ValueError(
                "DINGTALK_SYNC_SCOPE 只支持 configured 或 all_authorized"
            )
        if not settings.dingtalk_knowledge_base_id:
            raise ValueError(
                "同步范围为 configured 时必须配置 DINGTALK_KNOWLEDGE_BASE_ID"
            )
        return settings.dingtalk_knowledge_base_id

    @classmethod
    def _is_wiki_document(cls, extension: str, node_type: str) -> bool:
        return settings.dingtalk_include_wiki and (
            extension.lower() in WIKI_EXTENSIONS or node_type == "DOC"
        )

    @classmethod
    def _is_supported_document(cls, extension: str, node_type: str) -> bool:
        return (
            extension.lower() in cls.supported_extensions()
            or cls._is_wiki_document(extension, node_type)
        )

    async def _throttle(self):
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_call
        if elapsed < API_CALL_INTERVAL:
            await asyncio.sleep(API_CALL_INTERVAL - elapsed)
        self._last_call = asyncio.get_event_loop().time()

    async def _get_token(self) -> str:
        if self._access_token:
            return self._access_token
        r = await self._http.get(
            "https://oapi.dingtalk.com/gettoken",
            params={
                "appkey": settings.dingtalk_app_key,
                "appsecret": settings.dingtalk_app_secret,
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode") != 0:
            raise Exception(f"DingTalk auth failed: {data}")
        self._access_token = data["access_token"]
        return self._access_token

    def _runtime(self) -> util_models.RuntimeOptions:
        rt = util_models.RuntimeOptions()
        rt.read_timeout = 30000
        rt.connect_timeout = 10000
        return rt

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        token = await self._get_token()
        headers = wiki_models.ListWorkspacesHeaders()
        headers.x_acs_dingtalk_access_token = token
        result = []

        req = wiki_models.ListWorkspacesRequest(
            max_results=50,
            order_by="VIEW_TIME_DESC",
            with_permission_role=False,
            operator_id=settings.dingtalk_operator_id,
        )
        await self._throttle()
        resp = await self.wiki.list_workspaces_with_options_async(
            req, headers, self._runtime()
        )
        body = resp.body
        workspaces = body.workspaces if body.workspaces else []
        for ws in workspaces:
            result.append({
                "id": getattr(ws, "workspace_id", ""),
                "name": getattr(ws, "name", ""),
                "root_node_id": getattr(ws, "root_node_id", ""),
            })

        return result

    async def list_nodes(self, parent_node_id: str) -> List[Dict[str, Any]]:
        token = await self._get_token()
        headers = wiki_models.ListNodesHeaders()
        headers.x_acs_dingtalk_access_token = token
        result = []
        next_token = None

        while True:
            req = wiki_models.ListNodesRequest(
                max_results=30,
                parent_node_id=parent_node_id,
                operator_id=settings.dingtalk_operator_id,
                with_permission_role=False,
            )
            if next_token:
                req.next_token = next_token

            await self._throttle()
            resp = await self.wiki.list_nodes_with_options_async(
                req, headers, self._runtime()
            )
            body = resp.body
            nodes = body.nodes if body.nodes else []
            for n in nodes:
                result.append({
                    "node_id": getattr(n, "node_id", ""),
                    "name": getattr(n, "name", ""),
                    "type": getattr(n, "type", ""),
                    "extension": getattr(n, "extension", "") or "",
                    "category": getattr(n, "category", "") or "",
                    "has_children": getattr(n, "has_children", False),
                    "updated_at": (
                        getattr(n, "updated_at", None)
                        or getattr(n, "modified_at", None)
                        or getattr(n, "update_time", None)
                        or ""
                    ),
                    "file_size": (
                        getattr(n, "file_size", None)
                        or getattr(n, "size", None)
                        or None
                    ),
                    "url": getattr(n, "url", "") or "",
                })

            next_token = getattr(body, "next_token", None) or ""
            if not next_token or not nodes:
                break
            await asyncio.sleep(0.2)

        return result

    async def _query_dentry_id(self, dentry_uuid: str) -> Optional[Dict[str, str]]:
        if dentry_uuid in self._dentry_cache:
            return self._dentry_cache[dentry_uuid]

        token = await self._get_token()
        for attempt in range(MAX_RETRIES):
            try:
                await self._throttle()
                r = await self._http.get(
                    f"https://api.dingtalk.com/v2.0/doc/dentries/{dentry_uuid}/queryDentryId",
                    headers={
                        "x-acs-dingtalk-access-token": token,
                    },
                    params={"operatorId": settings.dingtalk_operator_id},
                )
                r.raise_for_status()
                data = r.json()
                result = {
                    "space_id": data["spaceId"],
                    "dentry_id": data["dentryId"],
                }
                self._dentry_cache[dentry_uuid] = result
                return result
            except Exception as e:
                if "QpsLimit" in str(e) and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                logger.warning(f"queryDentryId failed for {dentry_uuid}: {e}")
                return None

    async def _download_file_to_path(
        self,
        space_id: str,
        dentry_id: str,
        temporary_path: Path,
    ) -> Optional[Dict[str, Any]]:
        """将钉钉原文件分块写入临时文件，避免大文件全部进入内存。"""
        token = await self._get_token()
        for attempt in range(MAX_RETRIES):
            try:
                await self._throttle()
                headers = storage_models.GetFileDownloadInfoHeaders()
                headers.x_acs_dingtalk_access_token = token
                option = storage_models.GetFileDownloadInfoRequestOption(
                    prefer_intranet=False
                )
                request = storage_models.GetFileDownloadInfoRequest(
                    union_id=settings.dingtalk_operator_id,
                    option=option,
                )
                response = await self.storage.get_file_download_info_with_options_async(
                    space_id,
                    dentry_id,
                    request,
                    headers,
                    self._runtime(),
                )
                signature = response.body.header_signature_info
                urls = signature.resource_urls if signature.resource_urls else []
                request_headers = dict(signature.headers) if signature.headers else {}
                if not urls:
                    raise RuntimeError("钉钉未返回文件下载地址")

                digest = hashlib.sha256()
                size = 0
                async with self._http.stream(
                    "GET",
                    urls[0],
                    headers=request_headers,
                    follow_redirects=True,
                ) as download_response:
                    download_response.raise_for_status()
                    with temporary_path.open("wb") as handle:
                        async for chunk in download_response.aiter_bytes(1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                if size <= 0:
                    raise RuntimeError("钉钉返回了空文件")
                return {"source_file_hash": digest.hexdigest(), "source_file_size": size}
            except Exception as exc:
                temporary_path.unlink(missing_ok=True)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                logger.warning(
                    f"Stream download failed {space_id}/{dentry_id}: {exc}"
                )
                return None

    @staticmethod
    def _extract_pdf_pages(content: bytes) -> Optional[List[Dict[str, Any]]]:
        """逐页提取 PDF 文字，并记录图片数量及具有业务意义的大图位置。"""
        image_details_by_page: Dict[int, List[Dict[str, Any]]] = {}
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page_number, page in enumerate(pdf.pages, 1):
                    page_area = max(float(page.width) * float(page.height), 1.0)
                    details = []
                    for image_index, image in enumerate(page.images or [], 1):
                        x0 = float(image.get("x0") or 0)
                        top = float(image.get("top") or 0)
                        x1 = float(image.get("x1") or x0)
                        bottom = float(image.get("bottom") or top)
                        width = max(x1 - x0, 0.0)
                        height = max(bottom - top, 0.0)
                        area_ratio = width * height / page_area
                        width_ratio = width / max(float(page.width), 1.0)
                        height_ratio = height / max(float(page.height), 1.0)
                        source_size = image.get("srcsize") or (0, 0)
                        details.append({
                            "image_index": image_index,
                            "bbox": [x0, top, x1, bottom],
                            "area_ratio": area_ratio,
                            "width_ratio": width_ratio,
                            "height_ratio": height_ratio,
                            "pixel_width": int(source_size[0] or 0),
                            "pixel_height": int(source_size[1] or 0),
                            # 面积和边长同时达标，可排除页眉 Logo、小图标及水印。
                            "is_substantive": (
                                area_ratio >= 0.04
                                and width_ratio >= 0.25
                                and height_ratio >= 0.08
                            ),
                        })
                    image_details_by_page[page_number] = details
        except Exception:
            pass

        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = []
            for page_number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                try:
                    image_count = len(page.images)
                except Exception:
                    image_count = 0
                pages.append({
                    "page_number": page_number,
                    "text": text,
                    "image_count": max(
                        image_count,
                        len(image_details_by_page.get(page_number, [])),
                    ),
                    "image_objects": image_details_by_page.get(page_number, []),
                    "substantive_images": [
                        image
                        for image in image_details_by_page.get(page_number, [])
                        if image.get("is_substantive")
                    ],
                })
            if pages:
                return pages
        except Exception:
            pass
        try:
            import pdfplumber
            pages = []
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page_number, page in enumerate(pdf.pages, 1):
                    pages.append({
                        "page_number": page_number,
                        "text": page.extract_text() or "",
                        "image_count": len(page.images or []),
                        "image_objects": image_details_by_page.get(page_number, []),
                        "substantive_images": [
                            image
                            for image in image_details_by_page.get(page_number, [])
                            if image.get("is_substantive")
                        ],
                    })
            if pages:
                return pages
        except Exception:
            pass
        try:
            import fitz
            doc = fitz.open(stream=content, filetype="pdf")
            pages = []
            for page_number, page in enumerate(doc, 1):
                pages.append({
                    "page_number": page_number,
                    "text": page.get_text() or "",
                    "image_count": len(page.get_images(full=True)),
                })
            doc.close()
            if pages:
                return pages
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_pdf_text(content: bytes) -> Optional[str]:
        """兼容旧调用：返回 PDF 的纯文字内容。"""
        pages = DingTalkClient._extract_pdf_pages(content)
        if not pages:
            return None
        texts = [str(page.get("text") or "") for page in pages]
        return "\n\n".join(text for text in texts if text)

    @staticmethod
    def _rebuild_table_from_ocr(
        raw_rows: List[List[Any]],
        table: Any,
        detections: List[Dict[str, Any]],
    ) -> Optional[List[List[Any]]]:
        """利用表格网格和OCR坐标重建字体编码损坏的单元格。"""
        cells = list(getattr(table, "cells", None) or ())
        if not cells or not detections:
            return None

        x_bounds = sorted({round(float(value), 3) for cell in cells for value in (cell[0], cell[2])})
        y_bounds = sorted({round(float(value), 3) for cell in cells for value in (cell[1], cell[3])})
        if len(x_bounds) < 3 or len(y_bounds) < 3:
            return None

        row_count = len(y_bounds) - 1
        column_count = len(x_bounds) - 1
        positioned: List[List[List[tuple[float, float, str]]]] = [
            [[] for _ in range(column_count)]
            for _ in range(row_count)
        ]
        table_bbox = tuple(float(value) for value in getattr(table, "bbox", ()))
        if len(table_bbox) != 4:
            return None

        for detection in detections:
            text = str(detection.get("text") or "").strip()
            bbox = tuple(float(value) for value in detection.get("bbox") or ())
            if not text or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = bbox
            center_x = (x0 + x1) / 2
            center_y = (y0 + y1) / 2
            anchor_y = y0 + min(max((y1 - y0) * 0.08, 0.5), 3.0)
            anchor_x = x0 + min(max((x1 - x0) * 0.08, 0.5), 3.0)
            if not (
                table_bbox[0] - 2 <= anchor_x <= table_bbox[2] + 2
                and table_bbox[1] - 2 <= anchor_y <= table_bbox[3] + 2
            ):
                continue
            row_index = next((
                index
                for index in range(row_count)
                if y_bounds[index] - 1 <= anchor_y <= y_bounds[index + 1] + 1
            ), None)
            containing_cells = [
                cell
                for cell in cells
                if (
                    float(cell[0]) - 1 <= center_x <= float(cell[2]) + 1
                    and float(cell[1]) - 1 <= center_y <= float(cell[3]) + 1
                )
            ]
            if containing_cells:
                containing_cell = min(
                    containing_cells,
                    key=lambda cell: (
                        (float(cell[2]) - float(cell[0]))
                        * (float(cell[3]) - float(cell[1]))
                    ),
                )
                cell_row_start = min(
                    range(len(y_bounds)),
                    key=lambda index: abs(y_bounds[index] - float(containing_cell[1])),
                )
                cell_row_end = min(
                    range(len(y_bounds)),
                    key=lambda index: abs(y_bounds[index] - float(containing_cell[3])),
                )
                if cell_row_end - cell_row_start > 1:
                    row_index = cell_row_start
            column_index = next((
                index
                for index in range(column_count)
                if x_bounds[index] - 1 <= anchor_x <= x_bounds[index + 1] + 1
            ), None)
            if row_index is None or column_index is None:
                continue
            positioned[row_index][column_index].append((y0, x0, text))

        fallback = [
            list(row or []) + [None] * max(column_count - len(row or []), 0)
            for row in raw_rows[:row_count]
        ]
        fallback.extend(
            [[None] * column_count for _ in range(row_count - len(fallback))]
        )
        merged_covered_positions = set()
        for cell in cells:
            row_start = min(
                range(len(y_bounds)),
                key=lambda index: abs(y_bounds[index] - float(cell[1])),
            )
            row_end = min(
                range(len(y_bounds)),
                key=lambda index: abs(y_bounds[index] - float(cell[3])),
            )
            column_start = min(
                range(len(x_bounds)),
                key=lambda index: abs(x_bounds[index] - float(cell[0])),
            )
            column_end = min(
                range(len(x_bounds)),
                key=lambda index: abs(x_bounds[index] - float(cell[2])),
            )
            for covered_row in range(row_start, row_end):
                for covered_column in range(column_start, column_end):
                    if (covered_row, covered_column) != (row_start, column_start):
                        merged_covered_positions.add((covered_row, covered_column))

        rebuilt: List[List[Any]] = []
        for row_index in range(row_count):
            row: List[Any] = []
            for column_index in range(column_count):
                items = sorted(positioned[row_index][column_index])
                if items:
                    value = "\n".join(item[2] for item in items)
                elif (row_index, column_index) in merged_covered_positions:
                    value = ""
                else:
                    raw_value = fallback[row_index][column_index]
                    value = raw_value if "\ufffd" not in str(raw_value or "") else ""
                row.append(value)
            rebuilt.append(row)

        non_empty = sum(bool(str(cell or "").strip()) for row in rebuilt for cell in row)
        if non_empty < 4 or any("\ufffd" in str(cell or "") for row in rebuilt for cell in row):
            return None
        return rebuilt

    @staticmethod
    def _extract_pdf_tables(
        content: bytes,
        ocr_detections_by_page: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ) -> Dict[int, List[str]]:
        """提取可可靠识别的 PDF 表格，并转换为 Markdown。"""
        result: Dict[int, List[str]] = {}
        ocr_detections_by_page = ocr_detections_by_page or {}
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page_number, page in enumerate(pdf.pages, 1):
                    markdown_tables = []
                    for found_table in page.find_tables() or []:
                        raw_table = found_table.extract()
                        if any(
                            "\ufffd" in str(cell or "")
                            for row in raw_table or []
                            for cell in row or []
                        ):
                            raw_table = DingTalkClient._rebuild_table_from_ocr(
                                list(raw_table or []),
                                found_table,
                                ocr_detections_by_page.get(page_number, []),
                            )
                            # OCR无法可靠恢复时不输出伪正确表格，正文仍会保留OCR结果供核对。
                            if not raw_table:
                                continue
                        rows = [list(row or []) for row in raw_table or []]
                        if len(rows) < 2:
                            continue

                        width = max((len(row) for row in rows), default=0)
                        if width < 2:
                            continue
                        normalized = [row + [None] * (width - len(row)) for row in rows]
                        active_columns = [
                            index
                            for index in range(width)
                            if any(row[index] not in (None, "") for row in normalized)
                        ]
                        if len(active_columns) < 2:
                            continue

                        normalized = [
                            [row[index] for index in active_columns]
                            for row in normalized
                        ]
                        non_empty_cells = sum(
                            cell not in (None, "")
                            for row in normalized
                            for cell in row
                        )
                        if non_empty_cells < 4:
                            continue

                        header = [
                            cell if cell not in (None, "") else f"列{index + 1}"
                            for index, cell in enumerate(normalized[0])
                        ]
                        table_lines = [
                            "| " + " | ".join(
                                DingTalkClient._markdown_cell(cell) for cell in header
                            ) + " |",
                            "| " + " | ".join("---" for _ in header) + " |",
                        ]
                        table_lines.extend(
                            "| " + " | ".join(
                                DingTalkClient._markdown_cell(cell) for cell in row
                            ) + " |"
                            for row in normalized[1:]
                        )
                        merge_cells = DingTalkClient._table_merge_cells(
                            found_table,
                            len(normalized),
                            width,
                            active_columns,
                        )
                        if merge_cells:
                            table_lines.append(
                                "<!-- rag-table-merges: "
                                + json.dumps(
                                    {"version": 1, "cells": merge_cells},
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                + " -->"
                            )
                        markdown_tables.append("\n".join(table_lines))

                    if markdown_tables:
                        result[page_number] = markdown_tables
        except Exception as exc:
            logger.debug(f"PDF table extraction failed: {exc}")
        return result

    @staticmethod
    def _table_merge_cells(
        table: Any,
        row_count: int,
        column_count: int,
        active_columns: List[int],
    ) -> List[Dict[str, int]]:
        """把 PDF 表格网格中的跨行、跨列关系映射到输出表格坐标。"""
        cells = list(getattr(table, "cells", None) or ())
        if not cells or row_count < 1 or column_count < 1:
            return []

        x_bounds = sorted({round(float(value), 3) for cell in cells for value in (cell[0], cell[2])})
        y_bounds = sorted({round(float(value), 3) for cell in cells for value in (cell[1], cell[3])})
        if len(x_bounds) < 2 or len(y_bounds) < 2:
            return []

        active_map = {
            original_index: output_index
            for output_index, original_index in enumerate(active_columns)
        }
        result: List[Dict[str, int]] = []
        seen = set()
        for cell in cells:
            row_start = min(
                range(len(y_bounds)),
                key=lambda index: abs(y_bounds[index] - float(cell[1])),
            )
            row_end = min(
                range(len(y_bounds)),
                key=lambda index: abs(y_bounds[index] - float(cell[3])),
            )
            column_start = min(
                range(len(x_bounds)),
                key=lambda index: abs(x_bounds[index] - float(cell[0])),
            )
            column_end = min(
                range(len(x_bounds)),
                key=lambda index: abs(x_bounds[index] - float(cell[2])),
            )
            mapped_columns = [
                active_map[index]
                for index in range(column_start, column_end)
                if index in active_map
            ]
            if not mapped_columns or row_start >= row_count:
                continue
            rowspan = min(row_end, row_count) - row_start
            colspan = len(mapped_columns)
            if rowspan <= 1 and colspan <= 1:
                continue
            item = (
                row_start,
                min(mapped_columns),
                max(rowspan, 1),
                max(colspan, 1),
            )
            if item in seen:
                continue
            seen.add(item)
            result.append({
                "row": item[0],
                "col": item[1],
                "rowspan": item[2],
                "colspan": item[3],
            })
        return sorted(result, key=lambda item: (item["row"], item["col"]))

    @staticmethod
    def _normalize_table_text(value: str) -> str:
        value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"<[^>]+>", " ", value)
        value = value.replace("\\", "")
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())

    @classmethod
    def _deduplicate_table_lines(
        cls,
        body_lines: List[str],
        markdown_tables: List[str],
    ) -> List[str]:
        """表格提取成功后，删除正文中重复出现的扁平表格文字。"""
        if not body_lines or not markdown_tables:
            return body_lines

        table_tokens = set()
        short_numeric_table_tokens = set()
        table_blobs = []
        table_words = set()
        for table in markdown_tables:
            table_words.update(
                re.findall(
                    r"[0-9a-z]+|[\u4e00-\u9fff]{2,}",
                    re.sub(r"<br\s*/?>", " ", table, flags=re.IGNORECASE).lower(),
                )
            )
            cells = []
            for row in table.splitlines():
                if not row.lstrip().startswith("|") or re.fullmatch(
                    r"[\s|:\-]+", row
                ):
                    continue
                for raw_cell in row.strip().strip("|").split("|"):
                    for part in re.split(r"<br\s*/?>|[;；]", raw_cell):
                        normalized = cls._normalize_table_text(part)
                        if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*%\s*", part):
                            short_numeric_table_tokens.add(normalized)
                        if len(normalized) >= 3:
                            table_tokens.add(normalized)
                            cells.append(normalized)
            table_blobs.append("".join(cells))

        duplicate_flags = []
        for line in body_lines:
            normalized = cls._normalize_table_text(line)
            duplicated = (
                normalized in short_numeric_table_tokens
                or (
                    len(normalized) >= 3
                    and (
                        normalized in table_tokens
                        or (
                            len(normalized) >= 8
                            and any(normalized in blob for blob in table_blobs)
                        )
                    )
                )
            )
            duplicate_flags.append(duplicated)

        # 对单元格顺序已经错乱、但词汇仍与结构化表格高度重合的残片做二次去重。
        for index, line in enumerate(body_lines):
            if duplicate_flags[index] or line.lstrip().startswith("|"):
                continue
            line_words = re.findall(
                r"[0-9a-z]+|[\u4e00-\u9fff]{2,}",
                re.sub(r"<br\s*/?>", " ", line, flags=re.IGNORECASE).lower(),
            )
            if len(line_words) < 3:
                continue
            overlap = sum(word in table_words for word in line_words)
            if overlap / len(line_words) >= 0.72:
                duplicate_flags[index] = True

        # 表格编号有时会脱离表格成为单独一行，仅在紧邻已确认残片时删除。
        for index, line in enumerate(body_lines):
            if not re.fullmatch(r"\s*\d{1,3}\s*", line):
                continue
            if (
                (index > 0 and duplicate_flags[index - 1])
                or (
                    index + 1 < len(duplicate_flags)
                    and duplicate_flags[index + 1]
                )
            ):
                duplicate_flags[index] = True

        for index, line in enumerate(body_lines):
            if not re.fullmatch(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*", line):
                continue
            adjacent_table_row_removed = (
                (index > 0 and duplicate_flags[index - 1])
                or (
                    index + 1 < len(duplicate_flags)
                    and duplicate_flags[index + 1]
                )
            )
            if adjacent_table_row_removed:
                duplicate_flags[index] = True

        # MarkItDown 遇到合并单元格时可能生成错位表格，并把单元格内容拆成普通行。
        # 结构化表格已经成功提取时，将相邻的 Markdown 表格行连同夹在其中的残片一起删除。
        pipe_indexes = [
            index
            for index, line in enumerate(body_lines)
            if line.lstrip().startswith("|")
        ]
        if pipe_indexes:
            group_start = pipe_indexes[0]
            group_end = pipe_indexes[0]
            for index in pipe_indexes[1:] + [len(body_lines) + 4]:
                if index - group_end <= 3:
                    group_end = index
                    continue
                for duplicate_index in range(group_start, group_end + 1):
                    duplicate_flags[duplicate_index] = True
                group_start = index
                group_end = index

        return [
            line
            for line, duplicated in zip(body_lines, duplicate_flags)
            if not duplicated
        ]

    @staticmethod
    def _recover_fragmented_table_heading(lines: List[str]) -> Optional[str]:
        """恢复被PDF表格单元格拆散的英文标题，例如 Trou|b|le|shooting。"""
        for line in lines[:12]:
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            fragments = [re.sub(r"[^A-Za-z]", "", cell) for cell in cells]
            fragments = [fragment for fragment in fragments if fragment]
            spaced_fragments = sum(
                bool(re.fullmatch(r"[A-Za-z]+(?:\s+[A-Za-z]+)+", cell))
                for cell in cells
            )
            if len(fragments) < 3 or spaced_fragments < 2:
                continue
            candidate = "".join(fragments)
            if 6 <= len(candidate) <= 80 and candidate.isalpha():
                return candidate
        return None

    @staticmethod
    def _pdf_page_title(lines: List[str], page_number: int) -> tuple[str, int]:
        """从页面前部选择最像章节标题的文本，并返回其位置。"""
        if not lines:
            return f"第 {page_number} 页", -1

        def clean_title(value: str) -> str:
            return re.sub(r"^#{1,6}\s+", "", value).strip()

        candidates = list(enumerate(lines[:12]))
        for index, line in candidates:
            if re.match(r"^#{1,6}\s+\S", line):
                return clean_title(line), index

        for index, line in candidates:
            title = clean_title(line)
            if re.fullmatch(r"\d+[.、][\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z\s]{1,18}[：:]?", title):
                return title, index

        for index, line in candidates:
            if re.match(r"^(?:[•●▪]|\d+[.)、])", line):
                continue
            if line.lstrip().startswith(("|", "![", ">", "```")):
                continue
            if re.search(r"\s[-–—]\s", line) and len(line) <= 100:
                return clean_title(line), index

        known_titles = {
            "document description",
            "table of contents",
            "robot deployment",
            "path planning",
            "setting up a task",
            "set the schedule",
        }
        for index, line in candidates:
            if clean_title(line).lower() in known_titles:
                return clean_title(line), index

        for index, line in candidates:
            title = clean_title(line)
            if not title or len(title) > 100:
                continue
            if re.match(r"^(?:[•●▪]|\d+[.)、])", title):
                continue
            if line.lstrip().startswith(("|", "![", ">", "```")):
                continue
            return title, index
        return f"第 {page_number} 页", -1

    @staticmethod
    def _pdf_to_markdown_result(
        content: bytes,
        page_text_overrides: Optional[Dict[int, str]] = None,
    ) -> Optional[PDFMarkdownResult]:
        """将PDF转换为分页Markdown，并允许使用MarkItDown逐页正文覆盖文本层。"""
        pages = DingTalkClient._extract_pdf_pages(content)
        if not pages:
            return None

        overrides = {
            int(page_number): str(text or "").strip()
            for page_number, text in (page_text_overrides or {}).items()
            if int(page_number) > 0 and str(text or "").strip()
        }
        for page in pages:
            page_number = int(page.get("page_number") or 0)
            if page_number in overrides:
                page["text"] = overrides[page_number]

        from app.core.pdf_ocr import PDFOCRService
        from app.core.pdf_vision import PDFVisionService

        corrupted_page_numbers = [
            int(page.get("page_number") or 0)
            for page in pages
            if PDFOCRService.has_replacement_chars(str(page.get("text") or ""))
        ]
        (
            recovered_ocr_by_page,
            recovery_detections_by_page,
            recovery_scanned_pages,
        ) = PDFOCRService.extract_corrupted_pages(content, pages)
        unresolved_recovery_pages: List[int] = []
        for page in pages:
            page_number = int(page.get("page_number") or 0)
            recovered_lines = recovered_ocr_by_page.get(page_number, [])
            if recovered_lines:
                page["text"] = "\n".join(recovered_lines)
                continue
            page_text = str(page.get("text") or "")
            if PDFOCRService.has_replacement_chars(page_text):
                unresolved_recovery_pages.append(page_number)
                page["text"] = re.sub(r"\ufffd+", "【待核对】", page_text)

        page_line_sets = []
        for page in pages:
            lines = {
                re.sub(r"\s+", " ", line).strip()
                for line in str(page.get("text") or "").splitlines()
                if line.strip()
            }
            page_line_sets.append(lines)

        line_frequency = Counter()
        for lines in page_line_sets:
            line_frequency.update(lines)
        repeat_threshold = max(3, math.ceil(len(pages) * 0.18))
        boilerplate_lines = {
            line
            for line, count in line_frequency.items()
            if count >= repeat_threshold and len(line) <= 100
        }

        if recovery_detections_by_page:
            tables_by_page = DingTalkClient._extract_pdf_tables(
                content,
                recovery_detections_by_page,
            )
        else:
            tables_by_page = DingTalkClient._extract_pdf_tables(content)
        ocr_by_page, ocr_scanned_pages = PDFOCRService.extract_novel_text(
            content,
            pages,
        )
        visual_by_page, visual_candidate_count, visual_analyzed_count = (
            PDFVisionService.analyze_important_pages(content, pages)
        )
        ocr_line_count = sum(len(lines) for lines in ocr_by_page.values())
        total_images = sum(int(page.get("image_count") or 0) for page in pages)
        if settings.pdf_ocr_enabled:
            recovery_message = ""
            if corrupted_page_numbers:
                recovery_message = (
                    f"检测到 {len(corrupted_page_numbers)} 页字体编码异常，"
                    f"已通过整页OCR恢复 {len(recovered_ocr_by_page)} 页；"
                )
                if unresolved_recovery_pages:
                    recovery_message += (
                        f"仍有 {len(unresolved_recovery_pages)} 页包含待人工核对内容；"
                    )
            visual_message = (
                f"已对 {ocr_scanned_pages} 页执行图片文字识别，"
                f"补充 {ocr_line_count} 行文本层中没有的文字；"
                f"{recovery_message}"
                "复杂示意图的空间关系仍需结合钉钉原文核对。"
            )
        else:
            visual_message = (
                "图片文字识别未启用；截图、照片和示意图请结合钉钉原文核对。"
            )
        parts = [
            (
                f"> PDF 解析提示：共 {len(pages)} 页，检测到 {total_images} 个图片对象。"
                f"{visual_message}"
                f"筛选 {visual_candidate_count} 个重要图片页，"
                f"其中 {visual_analyzed_count} 页已生成视觉功能说明。"
            )
        ]

        for page in pages:
            page_number = int(page.get("page_number") or 0)
            raw_lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in str(page.get("text") or "").splitlines()
                if line.strip()
            ]
            page_title, title_index = DingTalkClient._pdf_page_title(
                raw_lines, page_number
            )
            page_tables = tables_by_page.get(page_number, [])
            recovered_table_title = DingTalkClient._recover_fragmented_table_heading(
                raw_lines
            )
            normalized_page_title = DingTalkClient._normalize_table_text(page_title)
            normalized_table_text = "".join(
                DingTalkClient._normalize_table_text(table)
                for table in page_tables
            )
            if recovered_table_title and (
                title_index < 0
                or (
                    len(normalized_page_title) >= 3
                    and normalized_page_title in normalized_table_text
                )
            ):
                page_title = recovered_table_title
            if len(page_title) > 100:
                page_title = page_title[:97].rstrip() + "..."

            body_lines = [
                line
                for index, line in enumerate(raw_lines)
                if index != title_index and line not in boilerplate_lines
            ]
            body_lines = DingTalkClient._deduplicate_table_lines(
                body_lines,
                page_tables,
            )
            section_parts = [f"## 第 {page_number} 页 - {page_title}"]
            if body_lines:
                section_parts.append("\n".join(body_lines))
            elif not page_tables:
                section_parts.append("_本页未提取到除标题外的文字。_")

            if page_tables:
                section_parts.append(
                    "### 本页识别表格\n\n" + "\n\n".join(page_tables)
                )
            page_visual = visual_by_page.get(page_number)
            if page_visual:
                section_parts.append(
                    PDFVisionService.to_markdown(page_number, page_visual)
                )
            page_ocr_lines = ocr_by_page.get(page_number, [])
            if page_ocr_lines:
                section_parts.append(
                    "### 本页图片文字（OCR）\n\n"
                    "> 以下内容来自页面图片自动识别，可能存在少量误差。\n\n"
                    + "\n".join(f"- {line}" for line in page_ocr_lines)
                )
            parts.append("\n\n".join(section_parts))

        text = "\n\n".join(parts).strip()
        return PDFMarkdownResult(
            text=text,
            metadata={
                "pdf_total_pages": len(pages),
                "pdf_total_images": total_images,
                "pdf_ocr_scanned_pages": ocr_scanned_pages,
                "pdf_ocr_added_lines": ocr_line_count,
                "pdf_ocr_corrupted_pages": len(corrupted_page_numbers),
                "pdf_ocr_recovered_pages": len(recovered_ocr_by_page),
                "pdf_ocr_recovery_scanned_pages": recovery_scanned_pages,
                "pdf_ocr_unresolved_pages": unresolved_recovery_pages,
                "pdf_visual_candidate_pages": visual_candidate_count,
                "pdf_visual_analyzed_pages": visual_analyzed_count,
                "pdf_markitdown_pages": len(overrides),
            },
        )

    @staticmethod
    def _pdf_to_markdown(
        content: bytes,
        page_text_overrides: Optional[Dict[int, str]] = None,
    ) -> Optional[str]:
        """兼容旧调用：返回PDF增强解析生成的Markdown正文。"""
        result = DingTalkClient._pdf_to_markdown_result(
            content,
            page_text_overrides=page_text_overrides,
        )
        return result.text if result else None

    @staticmethod
    def _markdown_cell(value: Any) -> str:
        """转义 Excel 单元格中会破坏 Markdown 表格的字符。"""
        if value is None:
            return ""
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")

    @staticmethod
    def _extract_text(content: bytes, extension: str) -> str:
        ext = extension.lower()
        if content[:4] == b"%PDF" or ext == "pdf":
            text = DingTalkClient._pdf_to_markdown(content)
            if text:
                return text
        if ext == "docx" and content.startswith(b"PK"):
            try:
                return DingTalkClient._docx_to_markdown(content)
            except Exception:
                pass
        if ext == "xlsx":
            try:
                from openpyxl import load_workbook
                wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
                sections = []
                for ws in wb.worksheets:
                    rows = [
                        list(row)
                        for row in ws.iter_rows(values_only=True)
                        if any(cell is not None for cell in row)
                    ]
                    if not rows:
                        continue

                    width = max(len(row) for row in rows)
                    normalized = [
                        row + [None] * (width - len(row))
                        for row in rows
                    ]
                    table = [
                        "| " + " | ".join(
                            DingTalkClient._markdown_cell(cell) for cell in normalized[0]
                        ) + " |",
                        "| " + " | ".join("---" for _ in range(width)) + " |",
                    ]
                    table.extend(
                        "| " + " | ".join(
                            DingTalkClient._markdown_cell(cell) for cell in row
                        ) + " |"
                        for row in normalized[1:]
                    )
                    sections.append(f"## {ws.title}\n\n" + "\n".join(table))
                wb.close()
                return "\n\n".join(sections)
            except Exception:
                pass
        if ext == "pptx":
            try:
                buf = io.BytesIO(content)
                with zipfile.ZipFile(buf) as z:
                    slides = []
                    slide_files = sorted([n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")])
                    for name in slide_files:
                        with z.open(name) as f:
                            tree = ET.parse(f)
                            ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
                            texts = [t.text for t in tree.getroot().iter(f"{{{ns}}}t") if t.text]
                            if texts:
                                slides.append(" ".join(texts))
                    return "\n\n---\n\n".join(slides)
            except Exception:
                pass
        try:
            decoded = content.decode("utf-8", errors="ignore")
            if any("\u4e00" <= c <= "\u9fff" for c in decoded[:2000]):
                return decoded
        except Exception:
            pass
        return ""

    @staticmethod
    def _docx_to_markdown(content: bytes) -> str:
        buf = io.BytesIO(content)
        lines = []
        with zipfile.ZipFile(buf) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
                root = tree.getroot()
                ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                ns_p = ns
                ns_r = ns

                for para in root.iter(f"{{{ns_p}}}p"):
                    style_el = para.find(f".//{{{ns_p}}}pStyle")
                    style = style_el.get(f"{{{ns_p}}}val") if style_el is not None else ""

                    runs = []
                    for run in para.iter(f"{{{ns_r}}}r"):
                        rpr = run.find(f"{{{ns_r}}}rPr")
                        bold = rpr is not None and rpr.find(f"{{{ns_r}}}b") is not None
                        italic = rpr is not None and rpr.find(f"{{{ns_r}}}i") is not None
                        text_els = run.findall(f"{{{ns_r}}}t")
                        text = "".join(t.text or "" for t in text_els)
                        if not text:
                            continue
                        if bold and italic:
                            text = f"***{text}***"
                        elif bold:
                            text = f"**{text}**"
                        elif italic:
                            text = f"*{text}*"
                        runs.append(text)

                    line = "".join(runs)
                    if not line.strip():
                        lines.append("")
                        continue

                    num_el = para.find(f".//{{{ns_p}}}numPr")
                    if num_el is not None:
                        lines.append(f"- {line}")
                    elif "Heading1" in style or style == "1":
                        lines.append(f"# {line}")
                    elif "Heading2" in style or style == "2":
                        lines.append(f"## {line}")
                    elif "Heading3" in style or style == "3":
                        lines.append(f"### {line}")
                    elif "Heading4" in style or style == "4":
                        lines.append(f"#### {line}")
                    elif "Title" in style:
                        lines.append(f"# {line}")
                    else:
                        lines.append(line)

        return "\n\n".join(lines)

    async def _get_wiki_content(self, node_id: str) -> Optional[str]:
        token = await self._get_token()
        try:
            r = await self._http.post(
                "https://api.dingtalk.com/v1.0/doc/wikiContent",
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json={"wikiContentReq": {"nodeId": node_id}},
            )
            r.raise_for_status()
            data = r.json()
            md = data.get("content", "")
            if md and md.strip():
                return md
        except Exception as e:
            logger.debug(f"wikiContent API failed for {node_id}: {e}")

        try:
            r = await self._http.post(
                f"https://api.dingtalk.com/v1.0/doc/{node_id}/content",
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json={},
            )
            r.raise_for_status()
            data = r.json()
            md = data.get("content", "")
            if md and md.strip():
                return md
        except Exception as e:
            logger.debug(f"doc content API failed for {node_id}: {e}")

        return None

    async def download_node_raw_to_local(
        self,
        document: Dict[str, Any],
        extension: str,
        storage: DingTalkLocalStorage,
    ) -> Optional[Dict[str, Any]]:
        """将单个钉钉原文件流式下载到本地raw目录。"""
        node_id = str(document.get("id") or "").strip()
        dentry = await self._query_dentry_id(node_id)
        if not dentry:
            return None
        mime_type = mimetypes.guess_type(f"file.{extension}")[0] if extension else None
        temporary = storage.raw_temporary_path(document, extension)
        try:
            streamed = await self._download_file_to_path(
                dentry["space_id"],
                dentry["dentry_id"],
                temporary,
            )
            if not streamed:
                return None
            metadata = storage.commit_raw_temporary_file(
                document,
                temporary,
                extension=extension,
                mime_type=mime_type or "application/octet-stream",
                expected_hash=streamed["source_file_hash"],
                expected_size=streamed["source_file_size"],
            )
            metadata["source_extension"] = extension
            return metadata
        finally:
            temporary.unlink(missing_ok=True)

    async def list_all_docs(
        self, workspace_id: str = None,
        on_progress: Optional[Callable[[Dict[str, Any], int], Awaitable[None]]] = None,
    ) -> List[Dict[str, Any]]:
        target_id = self._target_workspace_id(workspace_id)
        logger.info(f"Listing DingTalk docs, workspace_id={target_id or 'all'}")

        self._on_progress = on_progress
        self._collected_count = 0
        self._inventory_had_errors = False
        self._last_inventory_complete = False
        self._last_inventory_scope_ids = []

        spaces = await self.list_workspaces()

        if target_id:
            spaces = [s for s in spaces if s["id"] == target_id]
            if not spaces:
                return []

        self._last_inventory_scope_ids = [str(space["id"]) for space in spaces]

        all_docs = []
        for sp in spaces:
            root_id = sp.get("root_node_id") or sp["id"]
            try:
                docs = await self._list_recursive(sp["id"], sp["name"], root_id)
                all_docs.extend(docs)
            except Exception as e:
                self._inventory_had_errors = True
                logger.error(f"Failed to list workspace {sp['name']}: {e}")

        self._last_inventory_complete = bool(
            self._last_inventory_scope_ids
        ) and not self._inventory_had_errors
        return all_docs

    async def _list_recursive(
        self, workspace_id: str, workspace_name: str, parent_id: str, path: str = ""
    ) -> List[Dict[str, Any]]:
        docs = []
        try:
            nodes = await self.list_nodes(parent_id)
        except Exception as e:
            self._inventory_had_errors = True
            logger.error(f"Failed to list nodes: {e}")
            return docs

        for node in nodes:
            node_id = node["node_id"]
            node_type = node["type"]
            name = node["name"] or "无标题"
            ext = node.get("extension", "")
            has_children = node.get("has_children", False)
            current_path = f"{path}/{name}" if path else name

            if node_type == "FOLDER" or has_children:
                if not settings.dingtalk_include_subfolders:
                    continue
                child_docs = await self._list_recursive(
                    workspace_id, workspace_name, node_id, current_path
                )
                docs.extend(child_docs)
                continue

            if ext.lower() in SKIP_EXTENSIONS:
                continue

            supported = self._is_supported_document(ext, node_type)
            if not supported:
                continue

            doc = {
                "id": node_id,
                "title": name,
                "extension": ext,
                "node_type": node_type,
                "space_id": workspace_id,
                "space_name": workspace_name,
                "path": current_path,
                "source_url": (
                    node.get("url")
                    or f"https://alidocs.dingtalk.com/i/nodes/{node_id}"
                ),
                "updated_at": str(node.get("updated_at") or ""),
                "file_size": node.get("file_size") or None,
            }
            docs.append(doc)
            self._collected_count += 1
            if self._on_progress:
                try:
                    await self._on_progress(doc, self._collected_count)
                except Exception:
                    pass

        return docs

    async def download_selected_raw_files(
        self,
        selected: List[Dict[str, Any]],
        on_progress: Optional[Callable[[Dict[str, Any], int], Awaitable[None]]] = None,
    ) -> List[Dict[str, Any]]:
        """批量下载原文件到本地，不执行Markdown转换或RAG入库。"""
        storage = DingTalkLocalStorage()
        storage.record_inventory(selected)
        semaphore = asyncio.Semaphore(
            min(max(settings.dingtalk_download_concurrency, 1), 8)
        )
        progress_lock = asyncio.Lock()
        completed = 0

        async def download_one(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            nonlocal completed
            node_id = str(item.get("id") or "").strip()
            extension = str(item.get("extension") or "").lower().lstrip(".")
            node_type = str(item.get("node_type") or "")
            try:
                async with semaphore:
                    metadata = None
                    if self._is_wiki_document(extension, node_type):
                        wiki_content = await self._get_wiki_content(node_id)
                        if wiki_content:
                            source_bytes = wiki_content.encode("utf-8")
                            metadata = storage.persist_raw_file(
                                item,
                                source_bytes,
                                extension="md",
                                mime_type="text/markdown",
                            )
                            metadata["source_extension"] = "md"
                        else:
                            metadata = await self.download_node_raw_to_local(
                                item, "docx", storage
                            )
                            if not metadata:
                                metadata = await self.download_node_raw_to_local(
                                    item, "", storage
                                )
                    elif extension in self.supported_extensions():
                        metadata = await self.download_node_raw_to_local(
                            item, extension, storage
                        )

                if not metadata:
                    raise RuntimeError("钉钉原文件下载失败")

                result = dict(item)
                result.update(metadata)
                return result
            except Exception as exc:
                storage.update_document_status(item, "failed", error=str(exc))
                logger.warning(
                    f"Download raw file failed for {item.get('title') or node_id}: {exc}"
                )
                return None
            finally:
                async with progress_lock:
                    completed += 1
                    current = completed
                if on_progress:
                    try:
                        await on_progress(item, current)
                    except Exception:
                        pass

        results = await asyncio.gather(*(download_one(item) for item in selected))
        return [result for result in results if result is not None]

    async def close(self):
        await self._http.aclose()
