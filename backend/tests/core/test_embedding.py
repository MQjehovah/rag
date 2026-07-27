import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.core.embedding.encoder import EmbeddingEncoder
from app.core.embedding.store import VectorStore

class TestEmbeddingEncoder:
    @pytest.mark.asyncio
    async def test_encode_query(self):
        encoder = EmbeddingEncoder()
        with patch.object(encoder.client, 'post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"embedding": [0.1] * 384}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response
            
            result = await encoder.encode_query("测试")
            assert len(result) == 384