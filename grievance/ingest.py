"""Idempotent corpus pipeline: read -> parse -> chunk -> embed -> index (D2, L1).

    python -m grievance.ingest build              # incremental: only new/changed chunks are embedded
    python -m grievance.ingest build --full       # rebuild index (embeddings still come from the cache)
    python -m grievance.ingest build --reembed    # force re-embedding of every chunk (e.g. new model)
    python -m grievance.ingest status

Chunking (clause strategy): one chunk per clause, split further only if a clause is longer than
MAX_CHARS. Each chunk is prefixed with a context header "[Regulator | Document | Clause | type]" before
embedding, so a passage carries *which framework it belongs to*. That is what lets a regulator-scoped
query ("Regulated Entity ... deficiency in service" @RBI) find the RBI scheme even though the complaint
speaks SEBI's vocabulary (section 3). Metadata (regulator, clause_type, entities) enables filtered search.

Re-embedding without rebuilding: chunk ids are stable (doc_id:clause_id:n) and every chunk carries a content
hash. On a rerun, unchanged chunks are untouched; changed/removed chunks are deleted from the FAISS index by id
and new vectors are added in place. Embeddings are cached by sha256(model + text).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pickle
import re
import sys
import time
from pathlib import Path

MAX_CHARS = 1200
FIXED_WINDOW, FIXED_OVERLAP = 500, 100
HEADER_RE = re.compile(r"^@(\w+):\s*(.*)$")
CLAUSE_RE = re.compile(r"^##\s*\[([^\]]+)\]\s*(.*)$")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def parse_txt(path: Path) -> dict:
    meta, clauses, cur = {}, [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not clauses and cur is None and (m := HEADER_RE.match(line)):
            meta[m.group(1)] = m.group(2).strip()
            continue
        if m := CLAUSE_RE.match(line):
            parts = [p.strip() for p in m.group(2).split("|")]
            tags = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            cur = {"clause_id": m.group(1).strip(), "title": parts[0], "clause_type": tags.get("type", "other"),
                   "entities": [e for e in tags.get("entities", "").split(",") if e], "text": ""}
            clauses.append(cur)
        elif cur is not None and line.strip():
            cur["text"] += (" " if cur["text"] else "") + line.strip()
    meta.setdefault("doc_id", path.stem)
    meta.setdefault("regulator", "unknown")
    return {"meta": meta, "clauses": clauses, "raw": path.read_text(encoding="utf-8")}


def parse_pdf(path: Path) -> dict:
    from pypdf import PdfReader
    text = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if len(text.strip()) < 200:
        raise ValueError(f"{path.name}: no text layer - run OCR (e.g. ocrmypdf) before indexing")
    reg = "RBI" if re.search(r"reserve bank", text[:3000], re.I) else "SEBI"
    doc_id = path.stem.upper()[:40]
    parts = re.split(r"\n(?=\s*\d{1,2}\.\s+[A-Z])", text)
    clauses = [{"clause_id": f"{doc_id}-P{i}", "title": p.strip().split("\n")[0][:80], "clause_type": "other",
                "entities": [], "text": " ".join(p.split())} for i, p in enumerate(parts) if len(p.strip()) > 80]
    return {"meta": {"doc_id": doc_id, "regulator": reg, "title": path.stem, "version": dt.date.fromtimestamp(
        path.stat().st_mtime).isoformat(), "reference": path.name}, "clauses": clauses, "raw": text}


def chunk_doc(doc: dict, strategy: str) -> list[dict]:
    m = doc["meta"]
    out = []
    if strategy == "fixed":
        # Deliberately naive baseline used for the before/after evaluation (L3): fixed character windows,
        # no clause boundaries, no context header. Clause metadata is kept only for the clause the window starts in.
        flat, owners = "", []
        for c in doc["clauses"]:
            owners.append((len(flat), c))
            flat += c["text"] + " "
        i, n = 0, 0
        while i < len(flat):
            win = flat[i:i + FIXED_WINDOW]
            owner = [c for start, c in owners if start <= i][-1]
            out.append(_chunk(m, owner, win, n, header=False))
            i += FIXED_WINDOW - FIXED_OVERLAP
            n += 1
        return out
    for c in doc["clauses"]:
        text = c["text"]
        pieces = [text] if len(text) <= MAX_CHARS else _split_long(text)
        for n, p in enumerate(pieces):
            out.append(_chunk(m, c, p, n, header=True))
    return out


def _split_long(text: str) -> list[str]:
    sents, cur, out = re.split(r"(?<=[.;])\s+", text), "", []
    for s in sents:
        if len(cur) + len(s) > MAX_CHARS and cur:
            out.append(cur.strip())
            cur = cur[-200:]  # small overlap keeps the sentence context
        cur += " " + s
    if cur.strip():
        out.append(cur.strip())
    return out


def _chunk(m: dict, c: dict, text: str, n: int, header: bool) -> dict:
    head = (f"[Regulator: {m['regulator']} | {m.get('title', m['doc_id'])} | Clause {c['clause_id']}: "
            f"{c['title']} | type: {c['clause_type']}]\n") if header else ""
    return {"id": f"{m['doc_id']}:{c['clause_id']}:{n}", "doc_id": m["doc_id"], "clause_id": c["clause_id"],
            "title": c["title"], "regulator": m["regulator"], "clause_type": c["clause_type"],
            "entities": c["entities"], "reference": m.get("reference", ""), "doc_version": m.get("version", ""),
            "text": text, "embed_text": head + text, "hash": sha(head + text)}


class EmbeddingCache:
    def __init__(self, path: Path, model: str):
        self.path, self.model = path, model
        self.data: dict[str, list[float]] = pickle.loads(path.read_bytes()) if path.exists() else {}

    def key(self, text: str) -> str:
        return sha(self.model + "\x00" + text)

    def save(self):
        self.path.write_bytes(pickle.dumps(self.data))


def get_embeddings(model: str):
    from langchain_huggingface import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(model_name=model, encode_kwargs={"normalize_embeddings": True})


def build(corpus_dir: str, index_dir: str, model: str, strategy: str = "clause", full: bool = False,
          reembed: bool = False) -> dict:
    from langchain_community.vectorstores import FAISS
    from langchain_community.vectorstores.utils import DistanceStrategy

    t0 = time.perf_counter()
    out = Path(index_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    docs, chunks = [], []
    for p in sorted(Path(corpus_dir).glob("*")):
        if p.suffix.lower() not in (".txt", ".pdf"):
            continue
        d = parse_txt(p) if p.suffix.lower() == ".txt" else parse_pdf(p)
        docs.append({"doc_id": d["meta"]["doc_id"], "file": p.name, "regulator": d["meta"]["regulator"],
                     "version": d["meta"].get("version", ""), "reference": d["meta"].get("reference", ""),
                     "sha256": sha(d["raw"]), "clauses": len(d["clauses"])})
        chunks += chunk_doc(d, strategy)
    if not chunks:
        raise SystemExit(f"no corpus documents found in {corpus_dir}")

    emb_model = get_embeddings(model)
    cache = EmbeddingCache(out / "embedding_cache.pkl", model)
    need = [c for c in chunks if reembed or cache.key(c["embed_text"]) not in cache.data]
    t_embed = time.perf_counter()
    if need:
        vecs = emb_model.embed_documents([c["embed_text"] for c in need])
        for c, v in zip(need, vecs):
            cache.data[cache.key(c["embed_text"])] = v
        cache.save()
    embed_s = time.perf_counter() - t_embed

    old_chunks = {c["id"]: c["hash"] for c in old.get("chunks", [])}
    new_chunks = {c["id"]: c["hash"] for c in chunks}
    can_increment = (not full and not reembed and (out / "index.faiss").exists()
                     and old.get("embedding_model") == model and old.get("chunking") == strategy)
    meta_of = lambda c: {k: c[k] for k in ("doc_id", "clause_id", "title", "regulator", "clause_type", "entities",
                                           "reference", "doc_version", "text")} | {"passage_id": c["id"]}
    if can_increment:
        vs = FAISS.load_local(str(out), emb_model, allow_dangerous_deserialization=True,
                              distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
        stale = [i for i, h in old_chunks.items() if new_chunks.get(i) != h]
        added = [c for c in chunks if old_chunks.get(c["id"]) != c["hash"]]
        if stale:
            vs.delete(ids=stale)
        if added:
            vs.add_embeddings([(c["embed_text"], cache.data[cache.key(c["embed_text"])]) for c in added],
                              metadatas=[meta_of(c) for c in added], ids=[c["id"] for c in added])
        mode = f"incremental (removed {len(stale)}, added {len(added)})"
    else:
        vs = FAISS.from_embeddings([(c["embed_text"], cache.data[cache.key(c["embed_text"])]) for c in chunks],
                                   emb_model, metadatas=[meta_of(c) for c in chunks], ids=[c["id"] for c in chunks],
                                   distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
        mode = "full"
    vs.save_local(str(out))
    elapsed = time.perf_counter() - t0
    manifest = {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(), "embedding_model": model, "chunking": strategy,
        "index_version": sha(json.dumps(sorted(new_chunks.items())) + model)[:12], "documents": docs,
        "n_chunks": len(chunks), "n_embedded_this_build": len(need), "mode": mode,
        "elapsed_s": round(elapsed, 3), "embed_s": round(embed_s, 3),
        # throughput of the embedding stage (excludes model load); falls back to whole-pipeline rate when nothing changed
        "throughput_chunks_per_s": round(len(need) / embed_s, 2) if need else round(len(chunks) / elapsed, 2),
        "chunks": [{k: c[k] for k in ("id", "doc_id", "clause_id", "title", "regulator", "clause_type", "entities",
                                      "reference", "hash", "text")} for c in chunks],
    }
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="grievance.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--full", action="store_true")
    b.add_argument("--reembed", action="store_true")
    b.add_argument("--chunking", default=os.getenv("CHUNKING_STRATEGY", "clause"), choices=["clause", "fixed"])
    b.add_argument("--corpus", default=os.getenv("CORPUS_DIR", "/corpus/raw"))
    b.add_argument("--out", default=os.getenv("INDEX_DIR", "/data/index"))
    s = sub.add_parser("status")
    s.add_argument("--out", default=os.getenv("INDEX_DIR", "/data/index"))
    a = ap.parse_args(argv)
    if a.cmd == "status":
        m = json.loads((Path(a.out) / "manifest.json").read_text())
        m.pop("chunks")
        print(json.dumps(m, indent=1))
        return 0
    model = os.getenv("EMBEDDING_MODEL")
    if not model:
        print("EMBEDDING_MODEL is not set (see .env.example)", file=sys.stderr)
        return 2
    m = build(a.corpus, a.out, model, a.chunking, a.full, a.reembed)
    print(json.dumps({k: v for k, v in m.items() if k != "chunks"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
