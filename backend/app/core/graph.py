import math
import logging
import re
from collections import Counter
from typing import List, Dict, Tuple, Set
from sqlalchemy.orm import Session

from app.models.database import Page, GraphEdge

logger = logging.getLogger(__name__)

try:
    import jieba
    import jieba.analyse
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False


class GraphBuilder:
    WEIGHT_VECTOR = 3.0
    WEIGHT_KEYWORD = 2.0
    WEIGHT_NOTEBOOK = 0.5
    SIMILARITY_THRESHOLD = 0.3
    KEYWORD_TOP = 15

    @staticmethod
    def extract_keywords(text: str, top_k: int = 15) -> Set[str]:
        if not text or not text.strip():
            return set()
        if JIEBA_AVAILABLE:
            tags = jieba.analyse.extract_tags(text, topK=top_k)
            return set(tags)
        words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', text.lower())
        counter = Counter(words)
        return set(w for w, _ in counter.most_common(top_k))

    @staticmethod
    def cosine_similarity(v1: List[float], v2: List[float]) -> float:
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot = sum(a * b for a, b in zip(v1, v2))
        n1 = math.sqrt(sum(a * a for a in v1))
        n2 = math.sqrt(sum(b * b for b in v2))
        if n1 == 0 or n2 == 0:
            return 0.0
        return dot / (n1 * n2)

    @staticmethod
    def jaccard_similarity(s1: Set[str], s2: Set[str]) -> float:
        if not s1 and not s2:
            return 0.0
        intersection = s1 & s2
        union = s1 | s2
        return len(intersection) / len(union) if union else 0.0

    def compute_edge_weight(
        self,
        vector_sim: float,
        keyword_sim: float,
        same_notebook: bool,
    ) -> float:
        w = (
            vector_sim * self.WEIGHT_VECTOR
            + keyword_sim * self.WEIGHT_KEYWORD
            + (1.0 if same_notebook else 0.0) * self.WEIGHT_NOTEBOOK
        )
        total = self.WEIGHT_VECTOR + self.WEIGHT_KEYWORD + self.WEIGHT_NOTEBOOK
        return w / total

    def build_graph(
        self,
        pages: List[Page],
        embeddings: Dict[str, List[float]],
        db: Session,
    ) -> int:
        db.query(GraphEdge).delete()

        page_keywords: Dict[str, Set[str]] = {}
        for p in pages:
            page_keywords[p.id] = self.extract_keywords(
                (p.title or "") + " " + (p.content or ""),
                self.KEYWORD_TOP,
            )

        edges_created = 0
        for i, p1 in enumerate(pages):
            for j, p2 in enumerate(pages):
                if j <= i:
                    continue

                e1 = embeddings.get(p1.id, [])
                e2 = embeddings.get(p2.id, [])
                if not e1 or not e2:
                    continue

                vec_sim = self.cosine_similarity(e1, e2)
                kw_sim = self.jaccard_similarity(
                    page_keywords.get(p1.id, set()),
                    page_keywords.get(p2.id, set()),
                )
                same_nb = (
                    p1.notebook_id
                    and p1.notebook_id == p2.notebook_id
                )

                weight = self.compute_edge_weight(vec_sim, kw_sim, same_nb)
                if weight < self.SIMILARITY_THRESHOLD:
                    continue

                edge = GraphEdge(
                    source_id=p1.id,
                    target_id=p2.id,
                    weight=str(round(weight, 4)),
                    edge_type="similarity",
                )
                db.add(edge)
                edges_created += 1

        db.commit()
        return edges_created
