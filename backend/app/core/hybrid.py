import math
import re
import uuid
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models.database import Page, PageTerm
from app.core.rag import EmbeddingService, JIEBA_AVAILABLE

_IN_CHUNK = 500


def tokenize(text: str) -> List[str]:
    """Chinese/English tokenizer shared by BM25 and entity matching."""
    if not text:
        return []
    cleaned = re.sub(r'```[\s\S]*?```', ' ', text)
    cleaned = re.sub(r'`[^`]*`', ' ', cleaned)
    cleaned = re.sub(r'https?://\S+', ' ', cleaned)
    cleaned = re.sub(r'[^\u4e00-\u9fff\w]', ' ', cleaned)

    if JIEBA_AVAILABLE:
        import jieba
        words = jieba.cut(cleaned)
    else:
        words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', cleaned)

    tokens = []
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        if w in EmbeddingService.STOP_WORDS:
            continue
        if re.match(r'^[\u4e00-\u9fff]+$', w):
            if len(w) >= 2:
                tokens.append(w)
        elif len(w) >= 3:
            tokens.append(w)
    return tokens


class HybridIndex:
    """BM25 sparse index over a per-page term-frequency table.

    Works on both SQLite and PostgreSQL without extra extensions.  Terms are
    extracted with the same tokenizer used everywhere else, so keyword recall
    finally gets real ranking (TF-IDF-style) instead of naive LIKE matching.
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, db: Session):
        self.db = db

    def index_page(
        self,
        page_id: str,
        title: str = "",
        content: str = "",
        keywords: Optional[str] = None,
    ) -> int:
        self.delete_page(page_id)
        tokens = tokenize((title or "") + " " + (content or ""))
        if not tokens:
            return 0

        counts = Counter(tokens)
        for term, tf in counts.items():
            self.db.add(PageTerm(
                id=str(uuid.uuid4()),
                page_id=page_id,
                term=term,
                tf=tf,
            ))
        self.db.query(Page).filter(Page.id == page_id).update(
            {"term_count": len(tokens)}
        )
        self.db.flush()
        return len(counts)

    def delete_page(self, page_id: str) -> None:
        self.db.query(PageTerm).filter(PageTerm.page_id == page_id).delete()
        self.db.query(Page).filter(Page.id == page_id).update({"term_count": 0})
        self.db.flush()

    def search(
        self,
        query: str,
        visible_ids: Optional[Set[str]] = None,
        top_k: int = 50,
    ) -> List[Tuple[str, float]]:
        terms = sorted(set(tokenize(query)))
        if not terms:
            return []

        if not visible_ids:
            return []

        visible = list(visible_ids)
        n = len(visible)

        avgdl_row = self.db.execute(
            sql_text(
                "SELECT AVG(COALESCE(term_count, 0)) FROM pages "
                "WHERE id IN ({})".format(_placeholders(visible))
            ),
            {f"v{i}": pid for i, pid in enumerate(visible)},
        ).fetchone()
        avgdl = avgdl_row[0] if avgdl_row and avgdl_row[0] else 0.0
        if avgdl <= 0:
            return []

        term_df: Dict[str, int] = {}
        postings: List[Tuple[str, str, int]] = []
        for chunk in _chunk_list(visible):
            ph = _placeholders(chunk)
            params = {f"v{i}": pid for i, pid in enumerate(chunk)}
            rows = self.db.execute(
                sql_text(
                    "SELECT term, page_id, tf FROM page_terms "
                    "WHERE page_id IN ({})".format(ph)
                ),
                params,
            ).fetchall()
            for term, pid, tf in rows:
                if term in terms:
                    postings.append((term, pid, tf))

        if not postings:
            return []

        # document frequency per term (across the visible corpus)
        df_counter: Dict[str, Set[str]] = {}
        for term, pid, _tf in postings:
            df_counter.setdefault(term, set()).add(pid)

        dl_map: Dict[str, int] = {}
        for term, pid, _tf in postings:
            if pid not in dl_map:
                dl_map[pid] = 0

        if dl_map:
            dl_rows = self.db.execute(
                sql_text(
                    "SELECT id, COALESCE(term_count, 0) FROM pages "
                    "WHERE id IN ({})".format(_placeholders(list(dl_map.keys())))
                ),
                {f"v{i}": pid for i, pid in enumerate(dl_map.keys())},
            ).fetchall()
            for pid, tc in dl_rows:
                dl_map[pid] = tc or 0

        scores: Dict[str, float] = {}
        for term, pid, tf in postings:
            df = len(df_counter.get(term, ()))
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            dl = dl_map.get(pid, 0)
            denom = tf + self.K1 * (1.0 - self.B + self.B * dl / avgdl)
            scores[pid] = scores.get(pid, 0.0) + idf * (tf * (self.K1 + 1.0)) / denom

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]


def _placeholders(ids: List[str]) -> str:
    return ",".join(f":v{i}" for i in range(len(ids)))


def _chunk_list(items: List[str]) -> List[List[str]]:
    return [items[i:i + _IN_CHUNK] for i in range(0, len(items), _IN_CHUNK)]
