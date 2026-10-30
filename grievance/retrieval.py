"""Retrieval layer (LangChain + FAISS) with query-embedding and retrieval caches (L1, D7)."""
from __future__ import annotations

import functools
import hashlib
import json
import threading
from pathlib import Path

from .config import settings
from .observability import INDEX_BUILT_TS, INDEX_CHUNKS, INDEX_DOCS, INGEST_EMBEDDED, INGEST_THROUGHPUT


class Retriever:
    def __init__(self, index_dir: str, model: str):
        from langchain_community.vectorstores import FAISS
        from langchain_community.vectorstores.utils import DistanceStrategy
        from .ingest import get_embeddings

        self.index_dir = Path(index_dir)
        self.manifest = json.loads((self.index_dir / "manifest.json").read_text())
        if self.manifest["embedding_model"] != model:
            raise RuntimeError("index was built with a different embedding model; run ingest build --reembed")
        self.emb = get_embeddings(model)
        self.vs = FAISS.load_local(str(self.index_dir), self.emb, allow_dangerous_deserialization=True,
                                   distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
        self.by_clause: dict[str, dict] = {}
        for c in self.manifest["chunks"]:
            self.by_clause.setdefault(c["clause_id"], c)
        self.version = self.manifest["index_version"]
        self._lock = threading.Lock()
        self._embed = functools.lru_cache(maxsize=2048)(self._embed_uncached)  # query-embedding cache

    def _embed_uncached(self, q: str) -> tuple:
        return tuple(self.emb.embed_query(q))

    def search(self, query: str, k: int = 4, regulator: str | None = None, clause_type: str | None = None) -> list[dict]:
        flt = {}
        if regulator:
            flt["regulator"] = regulator
        if clause_type:
            flt["clause_type"] = clause_type
        with self._lock:  # FAISS search is not guaranteed thread-safe; it is also ~1 ms, so contention is low
            vec = list(self._embed(query))
            hits = self.vs.similarity_search_with_score_by_vector(vec, k=k, filter=flt or None, fetch_k=60)
        return [_passage(d.metadata, float(s)) for d, s in hits]

    def get_clause(self, clause_id: str) -> dict | None:
        c = self.by_clause.get(clause_id)
        if not c:
            return None
        return _passage({**c, "passage_id": c["id"]}, 1.0)

    def scope(self, regulator: str) -> list[dict]:
        return [_passage({**c, "passage_id": c["id"]}, 1.0) for c in self.manifest["chunks"]
                if c["regulator"] == regulator and c["clause_type"] in ("scope", "definition", "grounds")]

    def export_metrics(self):
        import datetime as dt
        m = self.manifest
        INDEX_BUILT_TS.set(dt.datetime.fromisoformat(m["built_at"]).timestamp())
        INDEX_CHUNKS.set(m["n_chunks"])
        INDEX_DOCS.set(len(m["documents"]))
        INGEST_THROUGHPUT.set(m["throughput_chunks_per_s"])
        INGEST_EMBEDDED.set(m["n_embedded_this_build"])


def _passage(md: dict, score: float) -> dict:
    return {"passage_id": md["passage_id"], "doc_id": md["doc_id"], "clause_id": md["clause_id"],
            "title": md.get("title", ""), "regulator": md["regulator"], "clause_type": md["clause_type"],
            "entities": md.get("entities", []), "reference": md.get("reference", ""), "text": md["text"],
            "score": round(score, 4)}


_retriever: Retriever | None = None


def retriever() -> Retriever:
    global _retriever
    if _retriever is None:
        s = settings()
        _retriever = Retriever(s.index_dir, s.embedding_model)
        _retriever.export_metrics()
    return _retriever


def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]
