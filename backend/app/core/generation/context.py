class ContextAssembler:
    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Chinese characters are generally close to one token; latin text is
        # approximated at four characters per token.
        if not text:
            return 0
        chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
        return max(1, chinese + (len(text) - chinese + 3) // 4)
