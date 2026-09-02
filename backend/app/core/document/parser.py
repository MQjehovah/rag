from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MarkdownHeader:
    level: int
    text: str
    line: int


class MarkdownParser:
    def extract_headers(self, content: str) -> list[MarkdownHeader]:
        headers = []
        for line_number, line in enumerate((content or "").splitlines(), start=1):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match:
                headers.append(MarkdownHeader(len(match.group(1)), match.group(2), line_number))
        return headers

    def extract_plain_text(self, content: str) -> str:
        text = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", content or "")
        text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"[`*_~>]", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
