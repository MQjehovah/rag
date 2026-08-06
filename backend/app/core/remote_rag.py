import asyncio
import hashlib
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import httpx

from app.config import settings
from app.core.content_quality import detect_binary_text
from app.core.dingtalk_converter import CONVERSION_PIPELINE_VERSION
from app.core.dingtalk_rag_importer import DingTalkRAGImporter
from app.core.dingtalk_storage import DingTalkLocalStorage


PDF_IMAGE_PATTERN = re.compile(
    r"/api/upload/pdf-pages/([0-9a-f]{64})/"
    r"(page-[1-9]\d*(?:-image-[1-9]\d*)?\.jpg)"
)
MERGE_METADATA_PATTERN = re.compile(
    r"(?m)^<!-- rag-table-merges: (\{[^\n]*\}) -->\s*$"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RemoteImageAsset:
    markdown_url: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class PreparedRemoteDocument:
    document_id: str
    title: str
    content: str
    markdown_hash: str
    content_hash: str
    source_path: str = ""
    source_space_id: str = ""
    source_url: str = ""
    source_file_hash: str = ""
    source_file_size: int = 0
    source_mime_type: str = ""
    pipeline_version: str = CONVERSION_PIPELINE_VERSION
    images: List[RemoteImageAsset] = field(default_factory=list)


class RemoteRAGQualityGate:
    """远程导入硬性质量门禁；失败文档不得进入老师服务器。"""

    def __init__(self, storage: DingTalkLocalStorage | None = None):
        self.storage = storage or DingTalkLocalStorage()

    def _markdown_path(self, entry: Dict[str, Any]) -> Path:
        relative = str(entry.get("markdown_path") or "").strip()
        if not relative:
            raise ValueError("同步清单缺少markdown_path")
        path = (self.storage.root / relative).resolve()
        self.storage._assert_contained(path, self.storage.markdown_root)
        if not path.is_file():
            raise FileNotFoundError(f"本地Markdown不存在: {path}")
        return path

    @staticmethod
    def _frontmatter_value(markdown: str, key: str) -> str:
        if not markdown.startswith("---"):
            return ""
        match = re.search(
            rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$",
            markdown.split("\n---", 1)[0],
        )
        if not match:
            return ""
        raw = match.group(1).strip()
        try:
            value = json.loads(raw)
            return str(value or "")
        except json.JSONDecodeError:
            return raw.strip("\"'")

    @staticmethod
    def _split_table_row(line: str) -> List[str]:
        source = line.strip().strip("|")
        cells: List[str] = []
        current: List[str] = []
        escaped = False
        for character in source:
            if escaped:
                current.append(character)
                escaped = False
            elif character == "\\":
                current.append(character)
                escaped = True
            elif character == "|":
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        cells.append("".join(current).strip())
        return cells

    @classmethod
    def _validate_tables(cls, content: str) -> None:
        lines = content.splitlines()
        is_row = lambda value: (
            value.lstrip().startswith("|") and value.rstrip().endswith("|")
        )
        is_separator = lambda value: (
            "-" in value and bool(re.fullmatch(r"[\s|:\-]+", value))
        )

        for separator_index, separator in enumerate(lines):
            if not is_row(separator) or not is_separator(separator):
                continue
            if separator_index == 0 or not is_row(lines[separator_index - 1]):
                raise ValueError("Markdown表格分隔行前缺少表头")

            expected_width = len(cls._split_table_row(separator))
            header_width = len(cls._split_table_row(lines[separator_index - 1]))
            if header_width != expected_width:
                raise ValueError(
                    f"Markdown表格表头列数与分隔行不一致: "
                    f"[{header_width}, {expected_width}]"
                )

            data_widths: List[int] = []
            row_index = separator_index + 1
            while row_index < len(lines) and is_row(lines[row_index]):
                # 允许不同列数的两个表格直接相邻：下一张表的表头紧跟在当前表数据后。
                if (
                    row_index + 1 < len(lines)
                    and is_row(lines[row_index + 1])
                    and is_separator(lines[row_index + 1])
                ):
                    break
                data_widths.append(len(cls._split_table_row(lines[row_index])))
                row_index += 1
            if any(width != expected_width for width in data_widths):
                raise ValueError(
                    f"Markdown表格列数不一致: "
                    f"{[expected_width, *data_widths]}"
                )

    @staticmethod
    def _validate_merge_metadata(content: str) -> None:
        for match in MERGE_METADATA_PATTERN.finditer(content):
            try:
                metadata = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise ValueError("表格合并元数据不是有效JSON") from exc
            if metadata.get("version") != 1 or not isinstance(metadata.get("cells"), list):
                raise ValueError("表格合并元数据版本或结构无效")
            for cell in metadata["cells"]:
                if not isinstance(cell, dict):
                    raise ValueError("表格合并单元格结构无效")
                values = [cell.get(key) for key in ("row", "col", "rowspan", "colspan")]
                if not all(isinstance(value, int) for value in values):
                    raise ValueError("表格合并坐标必须为整数")
                row, col, rowspan, colspan = values
                if row < 0 or col < 0 or not 1 <= rowspan <= 100 or not 1 <= colspan <= 100:
                    raise ValueError("表格合并坐标超出安全范围")

    @staticmethod
    def _image_root() -> Path:
        return Path(settings.pdf_image_storage_dir).resolve()

    def _collect_images(self, content: str) -> List[RemoteImageAsset]:
        root = self._image_root()
        result: List[RemoteImageAsset] = []
        seen = set()
        for match in PDF_IMAGE_PATTERN.finditer(content):
            markdown_url = match.group(0)
            if markdown_url in seen:
                continue
            seen.add(markdown_url)
            path = (root / match.group(1) / match.group(2)).resolve()
            if root not in path.parents or not path.is_file():
                raise ValueError(f"Markdown引用的PDF图片不存在: {markdown_url}")
            payload = path.read_bytes()
            if not payload:
                raise ValueError(f"Markdown引用的PDF图片为空: {markdown_url}")
            result.append(RemoteImageAsset(
                markdown_url=markdown_url,
                path=path,
                sha256=hashlib.sha256(payload).hexdigest(),
            ))
        return result

    def validate_entry(self, entry: Dict[str, Any]) -> PreparedRemoteDocument:
        document_id = str(entry.get("document_id") or "").strip()
        if not document_id:
            raise ValueError("同步清单缺少钉钉文档ID")
        if entry.get("source_status", "active") != "active":
            raise ValueError("钉钉源文档不是有效状态")
        if entry.get("conversion_status") != "converted":
            raise ValueError("文档尚未成功转换")

        path = self._markdown_path(entry)
        markdown_bytes = path.read_bytes()
        markdown_hash = hashlib.sha256(markdown_bytes).hexdigest()
        expected_hash = str(entry.get("markdown_hash") or "").strip()
        if expected_hash and markdown_hash != expected_hash:
            raise ValueError("Markdown文件哈希与同步清单不一致")
        try:
            markdown = markdown_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown文件不是有效UTF-8") from exc
        if "\x00" in markdown:
            raise ValueError("Markdown包含二进制空字节")

        manifest_pipeline_version = str(
            entry.get("conversion_pipeline_version") or ""
        ).strip()
        markdown_pipeline_version = self._frontmatter_value(
            markdown,
            "conversion_pipeline_version",
        )
        if (
            manifest_pipeline_version != CONVERSION_PIPELINE_VERSION
            or markdown_pipeline_version != CONVERSION_PIPELINE_VERSION
        ):
            raise ValueError(
                f"Markdown不是最新转换版本，要求{CONVERSION_PIPELINE_VERSION}，"
                f"清单标记{manifest_pipeline_version or '未标记'}，"
                f"文件标记{markdown_pipeline_version or '未标记'}"
            )

        content = DingTalkRAGImporter._strip_frontmatter(markdown)
        if len(re.sub(r"\s+", "", content)) < 20:
            raise ValueError("Markdown有效正文过短")
        if "\ufffd" in content:
            raise ValueError("Markdown仍包含Unicode替换字符")
        if "【待核对】" in content:
            raise ValueError("Markdown仍包含待人工核对内容")
        if re.search(
            r"\]\((?:file://|[A-Za-z]:[/\\]|http://127\.0\.0\.1)",
            content,
            flags=re.IGNORECASE,
        ):
            raise ValueError("Markdown包含无法在老师服务器访问的本机路径")

        self._validate_tables(content)
        self._validate_merge_metadata(content)
        images = self._collect_images(content)
        return PreparedRemoteDocument(
            document_id=document_id,
            title=str(entry.get("name") or "未命名").strip() or "未命名",
            content=content,
            markdown_hash=markdown_hash,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_path=str(entry.get("dingtalk_path") or ""),
            source_space_id=str(entry.get("space_id") or ""),
            source_url=str(entry.get("source_url") or ""),
            source_file_hash=str(entry.get("source_file_hash") or ""),
            source_file_size=int(entry.get("source_file_size") or 0),
            source_mime_type=str(entry.get("source_mime_type") or ""),
            pipeline_version=manifest_pipeline_version,
            images=images,
        )


class RemoteRAGSyncState:
    """保存本地钉钉ID与远程页面ID映射，确保重复执行不会重复创建。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or settings.remote_rag_state_file).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._read()

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "target": {}, "documents": {}, "images": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"远程同步状态文件损坏: {self.path}") from exc
        if value.get("version") != 1:
            raise ValueError("远程同步状态文件版本不受支持")
        value.setdefault("target", {})
        value.setdefault("documents", {})
        value.setdefault("images", {})
        return value

    def save(self) -> None:
        self.data["updated_at"] = _utc_now()
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        last_error: PermissionError | None = None
        for attempt in range(12):
            try:
                temporary.replace(self.path)
                return
            except PermissionError as exc:
                # Windows上的杀毒软件、索引器或只读核查可能短暂占用目标文件。
                last_error = exc
                time.sleep(min(0.05 * (attempt + 1), 0.5))
        raise last_error or PermissionError(f"无法更新远程同步状态文件: {self.path}")

    def bind_target(self, base_url: str, notebook: Dict[str, Any]) -> None:
        expected = {
            "base_url": base_url.rstrip("/"),
            "notebook_id": str(notebook.get("id") or ""),
            "notebook_name": str(notebook.get("name") or ""),
        }
        current = self.data.get("target") or {}
        if self.data.get("documents") and current and current != expected:
            raise ValueError("远程同步状态已绑定其他服务器或知识库，拒绝混用")
        self.data["target"] = expected
        self.save()

    def document(self, document_id: str) -> Dict[str, Any]:
        return dict(self.data["documents"].get(document_id) or {})

    def update_document(self, document_id: str, **values: Any) -> None:
        current = dict(self.data["documents"].get(document_id) or {})
        current.update(values)
        current["updated_at"] = _utc_now()
        self.data["documents"][document_id] = current
        self.save()

    def image_url(self, sha256: str) -> str:
        return str((self.data["images"].get(sha256) or {}).get("url") or "")

    def update_image(self, sha256: str, url: str, name: str = "") -> None:
        self.data["images"][sha256] = {
            "url": url,
            "name": name,
            "uploaded_at": _utc_now(),
        }
        self.save()


class RemoteRAGClient:
    """老师服务器RAG后端接口客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = str(base_url or settings.remote_rag_base_url).rstrip("/")
        self.username = str(username or settings.remote_rag_username)
        self.password = str(password or settings.remote_rag_password)
        if not self.base_url:
            raise ValueError("REMOTE_RAG_BASE_URL未配置")
        if not self.username or not self.password:
            raise ValueError("远程RAG账号或密码未配置")
        self.token = ""
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=max(int(settings.remote_rag_timeout_seconds), 5),
            follow_redirects=True,
            transport=transport,
        )

    async def _login(self) -> None:
        response = await self.client.post(
            "/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        data = response.json()
        self.token = str(data.get("token") or "")
        if not self.token:
            raise RuntimeError("老师服务器登录成功但未返回令牌")

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempts = max(int(settings.remote_rag_max_retries), 1)
        last_error: Exception | None = None
        base_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(attempts):
            if not self.token:
                try:
                    await self._login()
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(min(2 ** attempt, 5))
                        continue
                    raise
            # 每次重试都重新复制请求头，避免首次请求修改参数后导致后续重试丢失自定义头。
            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {self.token}"
            try:
                response = await self.client.request(method, path, headers=headers, **kwargs)
                if response.status_code == 401 and attempt + 1 < attempts:
                    self.token = ""
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < attempts:
                        await asyncio.sleep(min(2 ** attempt, 5))
                        continue
                response.raise_for_status()
                return response
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2 ** attempt, 5))
                    continue
                raise
        raise RuntimeError(f"远程RAG请求失败: {last_error}")

    async def resolve_notebook(self, name: str, notebook_id: str = "") -> Dict[str, Any]:
        notebooks = (await self.request("GET", "/api/notebooks")).json()
        if notebook_id:
            matches = [item for item in notebooks if str(item.get("id")) == notebook_id]
            if len(matches) != 1:
                raise ValueError(f"老师服务器不存在指定知识库ID: {notebook_id}")
            if str(matches[0].get("name") or "").strip() != name:
                raise ValueError("指定知识库ID的名称不是“钉钉知识库”")
            return matches[0]
        matches = [
            item for item in notebooks
            if str(item.get("name") or "").strip() == name
        ]
        if len(matches) != 1:
            raise ValueError(
                f"老师服务器中名称为“{name}”的知识库数量为{len(matches)}，"
                "必须唯一后才能导入"
            )
        return matches[0]

    async def list_pages(self, notebook_id: str) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        page_number = 1
        while True:
            payload = (await self.request(
                "GET",
                "/api/pages",
                params={
                    "notebook_id": notebook_id,
                    "page": page_number,
                    "page_size": 200,
                },
            )).json()
            items = list(payload.get("items") or [])
            result.extend(items)
            if len(result) >= int(payload.get("total") or 0) or not items:
                return result
            page_number += 1

    async def get_page(self, page_id: str) -> Optional[Dict[str, Any]]:
        try:
            return (await self.request("GET", f"/api/pages/{page_id}")).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def upload_image(self, asset: RemoteImageAsset) -> Dict[str, Any]:
        mime_type = mimetypes.guess_type(asset.path.name)[0] or "image/jpeg"
        response = await self.request(
            "POST",
            "/api/upload/image",
            files={"file": (asset.path.name, asset.path.read_bytes(), mime_type)},
        )
        data = response.json()
        if not data.get("url"):
            raise RuntimeError("老师服务器图片上传接口未返回访问地址")
        return data

    async def verify_asset(self, url: str) -> None:
        await self.request("GET", url)

    async def create_page(self, title: str, content: str, notebook_id: str) -> Dict[str, Any]:
        return (await self.request(
            "POST",
            "/api/pages",
            json={"title": title, "content": content, "notebook_id": notebook_id},
        )).json()

    async def update_page(
        self,
        page_id: str,
        title: str,
        content: str,
        notebook_id: str,
    ) -> Dict[str, Any]:
        return (await self.request(
            "PUT",
            f"/api/pages/{page_id}",
            json={
                "title": title,
                "content": content,
                "notebook_id": notebook_id,
                "allow_source_edit": True,
            },
        )).json()

    async def import_source_page(
        self,
        document: PreparedRemoteDocument,
        content: str,
        content_hash: str,
        notebook_id: str,
        page_id: str = "",
    ) -> Dict[str, Any]:
        """通过来源文档专用接口写入，禁止降级为普通笔记写入。"""
        return (await self.request(
            "POST",
            "/api/pages/source-import",
            json={
                "page_id": page_id or None,
                "title": document.title,
                "content": content,
                "notebook_id": notebook_id,
                "source_type": "dingtalk",
                "source_id": document.document_id,
                "source_path": document.source_path,
                "source_space_id": document.source_space_id,
                "source_url": document.source_url,
                "source_file_hash": document.source_file_hash,
                "source_file_size": document.source_file_size,
                "source_mime_type": document.source_mime_type,
                "source_markdown_hash": document.markdown_hash,
                "source_pipeline_version": document.pipeline_version,
                "published_content_hash": content_hash,
            },
        )).json()

    async def index_page(self, page_id: str) -> Dict[str, Any]:
        return (await self.request("POST", f"/api/pages/{page_id}/index")).json()

    async def search(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        return (await self.request(
            "POST",
            "/api/search",
            json={"query": query, "top_k": top_k},
        )).json()

    async def close(self) -> None:
        await self.client.aclose()


class DingTalkRemoteRAGImporter:
    """将最新版本地Markdown通过HTTP幂等导入老师服务器。"""

    def __init__(
        self,
        storage: DingTalkLocalStorage | None = None,
        client: RemoteRAGClient | None = None,
        state: RemoteRAGSyncState | None = None,
    ):
        self.storage = storage or DingTalkLocalStorage()
        self.client = client or RemoteRAGClient()
        self.state = state or RemoteRAGSyncState()
        self.quality_gate = RemoteRAGQualityGate(self.storage)
        self.backup_root = Path(settings.remote_rag_backup_dir).resolve()

    def _entries(
        self,
        document_ids: Optional[Iterable[str]] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        selected_ids = {
            str(document_id).strip()
            for document_id in (document_ids or [])
            if str(document_id).strip()
        }
        entries = [
            entry for entry in self.storage.read_manifest()["documents"]
            if entry.get("source_status", "active") == "active"
            and entry.get("conversion_status") == "converted"
            and (
                not selected_ids
                or str(entry.get("document_id") or "") in selected_ids
            )
        ]
        entries.sort(key=lambda entry: (
            str(entry.get("dingtalk_path") or ""),
            str(entry.get("name") or ""),
            str(entry.get("document_id") or ""),
        ))
        return entries[:limit] if limit > 0 else entries

    def preflight(
        self,
        document_ids: Optional[Iterable[str]] = None,
        limit: int = 0,
    ) -> Dict[str, Any]:
        prepared: List[PreparedRemoteDocument] = []
        failures = []
        for entry in self._entries(document_ids, limit):
            try:
                prepared.append(self.quality_gate.validate_entry(entry))
            except Exception as exc:
                failures.append({
                    "document_id": str(entry.get("document_id") or ""),
                    "name": str(entry.get("name") or "未命名"),
                    "error": str(exc),
                })
        return {
            "total": len(prepared) + len(failures),
            "passed": len(prepared),
            "failed": len(failures),
            "documents": prepared,
            "failures": failures,
        }

    def write_preflight_plan(
        self,
        preflight: Dict[str, Any],
        path: str | Path | None = None,
    ) -> Path:
        """生成不含账号密钥的最终候选清单，供导入前审阅和留档。"""
        target = Path(path or (self.storage.root / "remote-import-plan.json")).resolve()
        self.storage._assert_contained(target, self.storage.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        documents: List[PreparedRemoteDocument] = preflight["documents"]
        payload = {
            "created_at": _utc_now(),
            "pipeline_version": CONVERSION_PIPELINE_VERSION,
            "target": {
                "base_url": self.client.base_url,
                "notebook_name": "钉钉知识库",
                "notebook_id": str(settings.remote_rag_notebook_id or "").strip(),
            },
            "total": preflight["total"],
            "passed": preflight["passed"],
            "failed": preflight["failed"],
            "documents": [
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "markdown_hash": document.markdown_hash,
                    "content_hash": document.content_hash,
                    "image_count": len(document.images),
                }
                for document in documents
            ],
            "failures": preflight["failures"],
        }
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    async def inspect_remote_target(self) -> Dict[str, Any]:
        """只读确认唯一目标知识库，不创建、修改或索引任何页面。"""
        notebook_name = str(settings.remote_rag_notebook_name or "钉钉知识库").strip()
        if notebook_name != "钉钉知识库":
            raise ValueError("远程目标知识库名称必须为“钉钉知识库”")
        notebook = await self.client.resolve_notebook(
            notebook_name,
            str(settings.remote_rag_notebook_id or "").strip(),
        )
        pages = await self.client.list_pages(str(notebook["id"]))
        preflight = self.preflight()
        local_by_title: Dict[str, List[str]] = {}
        for document in preflight["documents"]:
            local_by_title.setdefault(document.title, []).append(document.document_id)
        remote_by_title: Dict[str, List[str]] = {}
        for page in pages:
            remote_by_title.setdefault(str(page.get("title") or ""), []).append(
                str(page.get("id") or "")
            )
        tracked_pages = {
            str(value.get("remote_page_id") or "")
            for value in self.state.data.get("documents", {}).values()
            if str(value.get("remote_page_id") or "")
        }
        title_overlaps = []
        for title in sorted(set(local_by_title) & set(remote_by_title)):
            remote_ids = remote_by_title[title]
            title_overlaps.append({
                "title": title,
                "local_document_ids": local_by_title[title],
                "remote_page_ids": remote_ids,
                "untracked_remote_page_ids": [
                    page_id for page_id in remote_ids if page_id not in tracked_pages
                ],
            })
        return {
            "base_url": self.client.base_url,
            "notebook_id": str(notebook["id"]),
            "notebook_name": str(notebook["name"]),
            "page_count": len(pages),
            "candidate_count": preflight["passed"],
            "candidate_failure_count": preflight["failed"],
            "title_overlap_count": len(title_overlaps),
            "untracked_title_overlap_count": sum(
                bool(item["untracked_remote_page_ids"])
                for item in title_overlaps
            ),
            "title_overlaps": title_overlaps,
            "read_only": True,
        }

    async def audit_remote_integrity(
        self,
        concurrency: int = 4,
        write_report: bool = True,
    ) -> Dict[str, Any]:
        """按同步状态回读远端页面，检查缺失、内容漂移和二进制污染。"""
        notebook_name = str(settings.remote_rag_notebook_name or "钉钉知识库").strip()
        notebook = await self.client.resolve_notebook(
            notebook_name,
            str(settings.remote_rag_notebook_id or "").strip(),
        )
        self.state.bind_target(self.client.base_url, notebook)
        tracked = dict(self.state.data.get("documents", {}))
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 12)))

        async def inspect(document_id: str, state_entry: Dict[str, Any]):
            async with semaphore:
                page_id = str(state_entry.get("remote_page_id") or "")
                if not page_id:
                    return {
                        "document_id": document_id,
                        "title": state_entry.get("title"),
                        "remote_page_id": "",
                        "issue": "missing_mapping",
                    }
                page = await self.client.get_page(page_id)
                if not page:
                    return {
                        "document_id": document_id,
                        "title": state_entry.get("title"),
                        "remote_page_id": page_id,
                        "issue": "missing_page",
                    }
                content = str(page.get("content") or "")
                actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                expected_hash = str(state_entry.get("published_content_hash") or "")
                binary_reason = detect_binary_text(content)
                hash_drift = bool(expected_hash and actual_hash != expected_hash)
                metadata_drift = not (
                    str(page.get("source_type") or "") == "dingtalk"
                    and str(page.get("source_id") or "") == document_id
                    and str(page.get("source_pipeline_version") or "")
                    == CONVERSION_PIPELINE_VERSION
                )
                if not binary_reason and not hash_drift and not metadata_drift:
                    return None
                issue_parts = []
                if binary_reason:
                    issue_parts.append("binary_content")
                if hash_drift:
                    issue_parts.append("hash_drift")
                if metadata_drift:
                    issue_parts.append("metadata_drift")
                return {
                    "document_id": document_id,
                    "title": state_entry.get("title"),
                    "remote_page_id": page_id,
                    "issue": "+".join(issue_parts),
                    "binary_reason": binary_reason,
                    "expected_hash": expected_hash,
                    "actual_hash": actual_hash,
                    "content_chars": len(content),
                    "content_prefix": content[:32].replace("\r", "\\r").replace("\n", "\\n"),
                    "updated_at": page.get("updated_at"),
                    "source_type": page.get("source_type"),
                    "source_id": page.get("source_id"),
                    "source_pipeline_version": page.get("source_pipeline_version"),
                }

        inspected = await asyncio.gather(*(
            inspect(document_id, state_entry)
            for document_id, state_entry in tracked.items()
        ))
        issues = [item for item in inspected if item]
        content_issues = [
            item for item in issues
            if "binary_content" in item["issue"] or "hash_drift" in item["issue"]
        ]
        metadata_issues = [
            item for item in issues if "metadata_drift" in item["issue"]
        ]
        repairable_document_ids = [
            str(item["document_id"])
            for item in issues
            if item["issue"] != "missing_mapping"
        ]
        report = {
            "audited_at": _utc_now(),
            "target": {
                "base_url": self.client.base_url,
                "notebook_id": str(notebook["id"]),
                "notebook_name": str(notebook["name"]),
            },
            "pipeline_version": CONVERSION_PIPELINE_VERSION,
            "tracked": len(tracked),
            "passed": len(tracked) - len(issues),
            "issue_count": len(issues),
            "content_issue_count": len(content_issues),
            "metadata_issue_count": len(metadata_issues),
            "repairable_document_ids": repairable_document_ids,
            "issues": issues,
        }
        if write_report:
            self.backup_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.backup_root / f"remote-integrity-audit-{timestamp}.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            report["report_path"] = str(path)
        return report

    async def repair_remote_integrity(
        self,
        concurrency: int = 4,
    ) -> Dict[str, Any]:
        """仅修复审计发现的已映射异常文档，并在修复前后保留报告。"""
        before = await self.audit_remote_integrity(
            concurrency=concurrency,
            write_report=True,
        )
        document_ids = list(before["repairable_document_ids"])
        if not document_ids:
            return {
                "status": "already_consistent",
                "before": before,
                "repair": None,
                "after": before,
            }
        repair = await self.import_manifest(
            document_ids=document_ids,
            force=False,
            snapshot=True,
        )
        after = await self.audit_remote_integrity(
            concurrency=concurrency,
            write_report=True,
        )
        return {
            "status": "repaired" if not repair.get("failed") and not after["issue_count"] else "incomplete",
            "before": before,
            "repair": repair,
            "after": after,
        }

    async def verify_remote_search(
        self,
        queries: Iterable[str],
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """验证检索接口能返回目标“钉钉知识库”中的页面。"""
        notebook = await self.client.resolve_notebook(
            "钉钉知识库",
            str(settings.remote_rag_notebook_id or "").strip(),
        )
        checks = []
        for query in queries:
            payload = await self.client.search(str(query), top_k=top_k)
            target_results = []
            for item in payload.get("results") or []:
                page = await self.client.get_page(str(item.get("id") or ""))
                if page and str(page.get("notebook_id") or "") == str(notebook["id"]):
                    target_results.append({
                        "id": str(item.get("id") or ""),
                        "title": str(item.get("title") or ""),
                        "score": item.get("score"),
                        "source": item.get("source"),
                        "page_number": item.get("page_number"),
                    })
            checks.append({
                "query": str(query),
                "result_count": len(payload.get("results") or []),
                "target_result_count": len(target_results),
                "target_results": target_results,
            })
        return {
            "notebook_id": str(notebook["id"]),
            "notebook_name": str(notebook["name"]),
            "passed": all(item["target_result_count"] > 0 for item in checks),
            "checks": checks,
            "read_only": True,
        }

    def _backup_remote_page(self, page: Dict[str, Any]) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = self.backup_root / timestamp
        directory.mkdir(parents=True, exist_ok=True)
        page_id = re.sub(r"[^0-9A-Za-z-]+", "_", str(page.get("id") or "page"))
        path = directory / f"{page_id}.json"
        path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    async def snapshot_remote(self, notebook: Dict[str, Any]) -> Path:
        pages = await self.client.list_pages(str(notebook["id"]))
        details = []
        for item in pages:
            page = await self.client.get_page(str(item.get("id") or ""))
            if page:
                details.append(page)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        path = self.backup_root / f"remote-snapshot-{timestamp}.json"
        path.write_text(json.dumps({
            "created_at": _utc_now(),
            "base_url": self.client.base_url,
            "notebook": notebook,
            "pages": details,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    async def _publish_images(self, document: PreparedRemoteDocument) -> str:
        content = document.content
        for asset in document.images:
            remote_url = self.state.image_url(asset.sha256)
            if not remote_url:
                uploaded = await self.client.upload_image(asset)
                remote_url = str(uploaded["url"])
                await self.client.verify_asset(remote_url)
                self.state.update_image(
                    asset.sha256,
                    remote_url,
                    str(uploaded.get("name") or ""),
                )
            content = content.replace(asset.markdown_url, remote_url)
        if PDF_IMAGE_PATTERN.search(content):
            raise ValueError("仍有本地PDF图片地址未迁移")
        return content

    async def _find_identical_page(
        self,
        pages: List[Dict[str, Any]],
        title: str,
        content_hash: str,
    ) -> Optional[Dict[str, Any]]:
        for item in pages:
            if str(item.get("title") or "") != title:
                continue
            detail = await self.client.get_page(str(item.get("id") or ""))
            if detail and hashlib.sha256(
                str(detail.get("content") or "").encode("utf-8")
            ).hexdigest() == content_hash:
                return detail
        return None

    def _existing_page_assignments(
        self,
        documents: List[PreparedRemoteDocument],
        remote_pages: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """仅在未跟踪文档与未跟踪同名旧页数量完全一致时建立确定映射。"""
        remote_page_ids = {
            str(page.get("id") or "") for page in remote_pages
            if str(page.get("id") or "")
        }
        tracked_page_ids = {
            str(value.get("remote_page_id") or "")
            for value in self.state.data.get("documents", {}).values()
            if str(value.get("remote_page_id") or "") in remote_page_ids
        }
        pending_by_title: Dict[str, List[PreparedRemoteDocument]] = {}
        for document in documents:
            current_page_id = str(
                self.state.document(document.document_id).get("remote_page_id") or ""
            )
            if current_page_id in remote_page_ids:
                continue
            pending_by_title.setdefault(document.title, []).append(document)

        untracked_by_title: Dict[str, List[Dict[str, Any]]] = {}
        for page in remote_pages:
            page_id = str(page.get("id") or "")
            if not page_id or page_id in tracked_page_ids:
                continue
            untracked_by_title.setdefault(str(page.get("title") or ""), []).append(page)

        assignments: Dict[str, str] = {}
        for title, pending_documents in pending_by_title.items():
            old_pages = untracked_by_title.get(title, [])
            if not old_pages or len(old_pages) != len(pending_documents):
                continue
            ordered_documents = sorted(
                pending_documents,
                key=lambda document: document.document_id,
            )
            ordered_pages = sorted(
                old_pages,
                key=lambda page: str(page.get("id") or ""),
            )
            for document, page in zip(ordered_documents, ordered_pages):
                assignments[document.document_id] = str(page["id"])
        return assignments

    async def import_manifest(
        self,
        document_ids: Optional[Iterable[str]] = None,
        limit: int = 0,
        force: bool = False,
        snapshot: bool = True,
        on_progress: Optional[
            Callable[[PreparedRemoteDocument, int, int, str], None]
        ] = None,
    ) -> Dict[str, Any]:
        preflight = self.preflight(document_ids, limit)
        if preflight["failed"]:
            return {
                "status": "preflight_failed",
                "total": preflight["total"],
                "imported": 0,
                "updated": 0,
                "skipped": 0,
                "failed": preflight["failed"],
                "failures": preflight["failures"],
            }

        notebook_name = str(settings.remote_rag_notebook_name or "钉钉知识库").strip()
        if notebook_name != "钉钉知识库":
            raise ValueError("远程目标知识库名称必须为“钉钉知识库”")
        notebook = await self.client.resolve_notebook(
            notebook_name,
            str(settings.remote_rag_notebook_id or "").strip(),
        )
        self.state.bind_target(self.client.base_url, notebook)
        snapshot_path = str(await self.snapshot_remote(notebook)) if snapshot else ""
        remote_pages = await self.client.list_pages(str(notebook["id"]))

        imported = updated = skipped = adopted_existing = 0
        failures = []
        prepared_documents: List[PreparedRemoteDocument] = preflight["documents"]
        existing_assignments = self._existing_page_assignments(
            prepared_documents,
            remote_pages,
        )
        for index, document in enumerate(prepared_documents, 1):
            status = "imported"
            page_id = ""
            adopted_page = False
            try:
                published_content = await self._publish_images(document)
                published_hash = hashlib.sha256(
                    published_content.encode("utf-8")
                ).hexdigest()
                current_state = self.state.document(document.document_id)
                page_id = str(current_state.get("remote_page_id") or "")
                if not page_id and document.document_id in existing_assignments:
                    page_id = existing_assignments[document.document_id]
                    adopted_page = True
                remote_page = await self.client.get_page(page_id) if page_id else None
                if remote_page is None:
                    identical = await self._find_identical_page(
                        remote_pages,
                        document.title,
                        published_hash,
                    )
                    if identical:
                        remote_page = identical
                        page_id = str(identical["id"])

                remote_hash = (
                    hashlib.sha256(
                        str(remote_page.get("content") or "").encode("utf-8")
                    ).hexdigest()
                    if remote_page else ""
                )
                source_metadata_current = bool(
                    remote_page
                    and str(remote_page.get("source_type") or "") == "dingtalk"
                    and str(remote_page.get("source_id") or "") == document.document_id
                    and str(remote_page.get("source_pipeline_version") or "")
                    == document.pipeline_version
                )
                needs_write = (
                    force
                    or remote_page is None
                    or remote_hash != published_hash
                    or not source_metadata_current
                )
                if remote_page is None:
                    remote_page = await self.client.import_source_page(
                        document,
                        published_content,
                        published_hash,
                        str(notebook["id"]),
                    )
                    page_id = str(remote_page["id"])
                    self.state.update_document(
                        document.document_id,
                        remote_page_id=page_id,
                        status="page_written",
                        title=document.title,
                    )
                    remote_pages.append(remote_page)
                    status = "imported"
                elif needs_write:
                    backup_path = self._backup_remote_page(remote_page)
                    remote_page = await self.client.import_source_page(
                        document,
                        published_content,
                        published_hash,
                        str(notebook["id"]),
                        page_id=page_id,
                    )
                    self.state.update_document(
                        document.document_id,
                        remote_page_id=page_id,
                        status="page_written",
                        title=document.title,
                        previous_page_backup=str(backup_path),
                    )
                    status = "updated"
                else:
                    status = "skipped"

                if status != "skipped" or str(remote_page.get("index_status") or "") != "current":
                    index_result = await self.client.index_page(page_id)
                else:
                    index_result = {"message": "索引已是最新"}

                verified = await self.client.get_page(page_id)
                if not verified:
                    raise RuntimeError("远程页面写入后无法读取")
                verified_hash = hashlib.sha256(
                    str(verified.get("content") or "").encode("utf-8")
                ).hexdigest()
                if verified_hash != published_hash:
                    raise RuntimeError("远程页面内容哈希与本地发布内容不一致")
                if str(verified.get("notebook_id") or "") != str(notebook["id"]):
                    raise RuntimeError("远程页面写入了错误的知识库")
                if str(verified.get("source_type") or "") != "dingtalk":
                    raise RuntimeError("远程页面未保存钉钉来源类型")
                if str(verified.get("source_id") or "") != document.document_id:
                    raise RuntimeError("远程页面钉钉文档ID与本地不一致")
                if (
                    str(verified.get("source_pipeline_version") or "")
                    != document.pipeline_version
                ):
                    raise RuntimeError("远程页面转换管线版本与本地不一致")

                self.state.update_document(
                    document.document_id,
                    remote_page_id=page_id,
                    status="imported",
                    title=document.title,
                    markdown_hash=document.markdown_hash,
                    local_content_hash=document.content_hash,
                    published_content_hash=published_hash,
                    notebook_id=str(notebook["id"]),
                    index_result=index_result,
                    imported_at=_utc_now(),
                    error="",
                    adopted_existing=adopted_page or bool(
                        current_state.get("adopted_existing")
                    ),
                )
                if adopted_page:
                    adopted_existing += 1
                if status == "imported":
                    imported += 1
                elif status == "updated":
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:
                error_text = str(exc).strip() or repr(exc)
                self.state.update_document(
                    document.document_id,
                    remote_page_id=page_id,
                    status="failed",
                    title=document.title,
                    error=error_text,
                )
                status = "failed"
                failures.append({
                    "document_id": document.document_id,
                    "name": document.title,
                    "remote_page_id": page_id,
                    "error": error_text,
                })
            if on_progress:
                on_progress(document, index, len(prepared_documents), status)

        return {
            "status": "completed" if not failures else "completed_with_failures",
            "base_url": self.client.base_url,
            "notebook_id": str(notebook["id"]),
            "notebook_name": str(notebook["name"]),
            "snapshot_path": snapshot_path,
            "total": len(prepared_documents),
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "adopted_existing": adopted_existing,
            "failed": len(failures),
            "failures": failures,
        }

    async def close(self) -> None:
        await self.client.close()
