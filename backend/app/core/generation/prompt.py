class PromptTemplate:
    @staticmethod
    def format_context(documents: list[dict]) -> str:
        sections = []
        for index, document in enumerate(documents, start=1):
            metadata = document.get("metadata") or {}
            title = metadata.get("title") or f"Document {index}"
            source = metadata.get("source") or "unknown"
            sections.append(f"[{index}] {title} ({source})\n{document.get('content') or ''}")
        return "\n\n".join(sections)
