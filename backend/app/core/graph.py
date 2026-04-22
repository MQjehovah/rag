import math
import logging
import re
import json
import numpy as np
from collections import Counter
from typing import List, Dict, Tuple, Set
from sqlalchemy.orm import Session
from sqlalchemy import text

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
    ANN_CANDIDATE_LIMIT = 50

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
    def cosine_similarity(v1, v2) -> float:
        a = np.array(v1)
        b = np.array(v2)
        dot = np.dot(a, b)
        n1 = np.linalg.norm(a)
        n2 = np.linalg.norm(b)
        if n1 == 0 or n2 == 0:
            return 0.0
        return float(dot / (n1 * n2))

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
        db: Session,
    ) -> int:
        db.query(GraphEdge).delete()
        db.flush()

        page_data: Dict[str, Dict] = {}

        for p in pages:
            text = (p.title or "") + " " + (p.content or "")
            keywords = self.extract_keywords(text, self.KEYWORD_TOP)
            page_data[p.id] = {
                "page": p,
                "keywords": keywords,
                "embedding": None,
            }

        for p in pages:
            result = db.execute(
                text("SELECT embedding FROM page_chunks WHERE page_id = :pid ORDER BY chunk_index LIMIT 1"),
                {"pid": p.id}
            ).fetchone()
            if result and result[0]:
                try:
                    page_data[p.id]["embedding"] = json.loads(result[0])
                except Exception:
                    pass

        page_list = list(page_data.values())
        n = len(page_list)
        edges_created = 0
        batch_edges = []

        for i in range(n):
            pd1 = page_list[i]
            emb1 = pd1["embedding"]
            if not emb1:
                continue

            for j in range(i + 1, n):
                pd2 = page_list[j]
                emb2 = pd2["embedding"]
                if not emb2:
                    continue

                vec_sim = self.cosine_similarity(emb1, emb2)
                if vec_sim < 0.3:
                    continue

                kw_sim = self.jaccard_similarity(
                    pd1["keywords"],
                    pd2["keywords"],
                )
                same_nb = (
                    pd1["page"].notebook_id
                    and pd1["page"].notebook_id == pd2["page"].notebook_id
                )

                weight = self.compute_edge_weight(vec_sim, kw_sim, same_nb)
                if weight < self.SIMILARITY_THRESHOLD:
                    continue

                batch_edges.append({
                    "source_id": pd1["page"].id,
                    "target_id": pd2["page"].id,
                    "weight": round(weight, 4),
                })
                edges_created += 1

                if len(batch_edges) >= 500:
                    self._batch_insert_edges(db, batch_edges)
                    batch_edges = []

        if batch_edges:
            self._batch_insert_edges(db, batch_edges)

        db.commit()
        return edges_created

    def _batch_insert_edges(self, db: Session, edges: List[Dict]):
        import uuid
        for e in edges:
            edge = GraphEdge(
                id=str(uuid.uuid4()),
                source_id=e["source_id"],
                target_id=e["target_id"],
                weight=e["weight"],
                edge_type="similarity",
            )
            db.add(edge)
        db.flush()
