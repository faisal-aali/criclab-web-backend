"""Retrieval for the CricLab assistant.

The assistant answers from a fixed set of user-facing documents and nothing
else. That is the whole design: it cannot describe how CricLab works internally
because it has never been shown anything internal, and it cannot invent a
feature because it has no source to cite for one.

Retrieval runs on embeddings when a local embedding model is reachable, and
falls back to term overlap when it is not. The fallback is deliberately kept
working rather than treated as an error path — a support assistant that stops
answering because a model is down is worse than one that answers a little less
precisely.

Embeddings are cached by content hash, so a restart re-embeds nothing and an
edit to one document re-embeds only that document's chunks.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from pathlib import Path
from typing import Any, Iterable

import httpx

from app.config import get_settings
from app.db.mongo import get_db

log = logging.getLogger("criclab.assistant")

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

# Chunks are one heading's worth of prose. Long sections are split on paragraph
# boundaries rather than mid-sentence — a chunk cut in half retrieves badly and
# reads worse when it is quoted back.
MAX_CHUNK_CHARS = 1100
MIN_CHUNK_CHARS = 120
TOP_K = 4

# Embedding models do not put unrelated text near zero. Two sentences with
# nothing in common still score around 0.45 cosine with a general-purpose
# model, so a raw similarity compared against a small threshold admits
# everything — which is how "what is the airspeed of a swallow?" came back with
# a confident paragraph about ball speed.
#
# Cosine is therefore rescaled onto [0, 1] across the band where the signal
# actually lives before any threshold is applied.
SEMANTIC_FLOOR = 0.46
SEMANTIC_CEILING = 0.80

# Below this a passage is not really about the question.
MIN_RELEVANCE = 0.26
# And the *best* passage has to clear a higher bar than the rest, otherwise the
# whole set is discarded and the assistant says it does not know. Four weak
# passages are not evidence; they are four ways to be wrong.
MIN_TOP_RELEVANCE = 0.38

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does",
    "for", "from", "get", "has", "have", "how", "i", "if", "in", "is", "it",
    "its", "me", "my", "not", "of", "on", "or", "s", "so", "that", "the",
    "their", "them", "then", "there", "they", "this", "to", "up", "was", "we",
    "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your",
}

_cache: dict[str, list[dict[str, Any]]] = {}


# --------------------------------------------------------------------------- #
# Loading and chunking
# --------------------------------------------------------------------------- #

def _title_of(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Break a document at its `##` headings into (heading, body) pairs."""
    sections: list[tuple[str, str]] = []
    heading = ""
    buffer: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if buffer:
                sections.append((heading, "\n".join(buffer).strip()))
            heading = line[3:].strip()
            buffer = []
        elif line.startswith("# "):
            continue
        else:
            buffer.append(line)
    if buffer:
        sections.append((heading, "\n".join(buffer).strip()))
    return [(h, b) for h, b in sections if b]


def _pack(body: str) -> list[str]:
    """Group paragraphs up to the chunk ceiling, never splitting one."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) + 2 > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    if current:
        chunks.append(current)
    return chunks


def load_chunks() -> list[dict[str, Any]]:
    """Read and chunk every knowledge document. Cached after the first call."""
    if _cache.get("chunks"):
        return _cache["chunks"]

    chunks: list[dict[str, Any]] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("knowledge file unreadable: %s (%s)", path.name, exc)
            continue
        doc_title = _title_of(text, path.stem)
        for heading, body in _split_sections(text):
            for piece in _pack(body):
                if len(piece) < MIN_CHUNK_CHARS and heading == "":
                    continue
                label = f"{doc_title} — {heading}" if heading else doc_title
                chunks.append(
                    {
                        "id": hashlib.sha1(f"{path.name}:{label}:{piece[:80]}".encode()).hexdigest()[:16],
                        "source": path.name,
                        "title": label,
                        "doc": doc_title,
                        "heading": heading,
                        # The heading rides along in the embedded text: "Reminders"
                        # alone is meaningless, "Coaching sessions — Reminders" is not.
                        "text": f"{label}\n\n{piece}",
                        "body": piece,
                        "hash": hashlib.sha256(piece.encode()).hexdigest(),
                        "terms": _terms(f"{label} {piece}"),
                    }
                )
    _cache["chunks"] = chunks
    return chunks


# --------------------------------------------------------------------------- #
# Lexical scoring — the fallback, and a tiebreaker for the embedding path
# --------------------------------------------------------------------------- #

def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _lexical_score(question_terms: set[str], chunk: dict[str, Any]) -> float:
    if not question_terms:
        return 0.0
    overlap = question_terms & chunk["terms"]
    if not overlap:
        return 0.0
    # Normalised against the question, so a long chunk does not win by volume.
    coverage = len(overlap) / len(question_terms)
    # A hit in the heading says more about relevance than one buried in prose.
    heading_terms = _terms(chunk["title"])
    bonus = 0.25 * (len(question_terms & heading_terms) / len(question_terms))
    return min(1.0, coverage + bonus)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #

async def _embed(texts: list[str]) -> list[list[float]] | None:
    """Embed with the local model. Returns None if it is not reachable."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/embed",
                json={"model": settings.ollama_embed_model, "input": texts},
            )
            if response.status_code == 404:
                # Older builds only have the single-input endpoint.
                out = []
                for text in texts:
                    r = await client.post(
                        f"{settings.ollama_base_url}/api/embeddings",
                        json={"model": settings.ollama_embed_model, "prompt": text},
                    )
                    r.raise_for_status()
                    out.append(r.json()["embedding"])
                return out
            response.raise_for_status()
            return response.json()["embeddings"]
    except Exception as exc:
        log.info("embedding unavailable (%s) — using term matching", type(exc).__name__)
        return None


def _rescale(cosine: float) -> float:
    """Map raw similarity onto [0, 1] across the band that carries signal."""
    span = SEMANTIC_CEILING - SEMANTIC_FLOOR
    return max(0.0, min(1.0, (cosine - SEMANTIC_FLOOR) / span))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def build_index(*, force: bool = False) -> dict[str, Any]:
    """Embed anything whose text has changed since it was last embedded."""
    chunks = load_chunks()
    db = get_db()
    model = get_settings().ollama_embed_model

    stored = {
        row["_id"]: row
        async for row in db.kb_chunks.find({}, {"hash": 1, "vector": 1, "model": 1})
    }
    stale = [
        c for c in chunks
        if force
        or c["id"] not in stored
        or stored[c["id"]].get("hash") != c["hash"]
        or stored[c["id"]].get("model") != model
    ]

    embedded = 0
    if stale:
        vectors = await _embed([c["text"] for c in stale])
        if vectors and len(vectors) == len(stale):
            for chunk, vector in zip(stale, vectors):
                await db.kb_chunks.update_one(
                    {"_id": chunk["id"]},
                    {
                        "$set": {
                            "source": chunk["source"],
                            "title": chunk["title"],
                            "body": chunk["body"],
                            "hash": chunk["hash"],
                            "model": model,
                            "vector": vector,
                        }
                    },
                    upsert=True,
                )
            embedded = len(stale)

    # Rows for documents that no longer exist would keep being retrieved.
    live_ids = {c["id"] for c in chunks}
    removed = await db.kb_chunks.delete_many({"_id": {"$nin": list(live_ids)}})

    return {
        "chunks": len(chunks),
        "embedded": embedded,
        "removed": int(removed.deleted_count),
        "mode": "embeddings" if embedded or stored else "terms",
    }


async def _vectors() -> dict[str, list[float]]:
    model = get_settings().ollama_embed_model
    return {
        row["_id"]: row["vector"]
        async for row in get_db().kb_chunks.find({"model": model}, {"vector": 1})
    }


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

async def retrieve(question: str, *, k: int = TOP_K) -> list[dict[str, Any]]:
    """Best passages for a question, most relevant first.

    Scores blend semantic similarity with term overlap. Overlap alone misses
    paraphrases; similarity alone drifts onto passages that share a mood but not
    a subject. Together they are steadier than either.
    """
    chunks = load_chunks()
    if not chunks:
        return []
    question_terms = _terms(question)
    lexical = {c["id"]: _lexical_score(question_terms, c) for c in chunks}

    semantic: dict[str, float] = {}
    stored = await _vectors()
    if stored:
        embedded = await _embed([question])
        if embedded:
            query_vector = embedded[0]
            semantic = {
                cid: _rescale(_cosine(query_vector, vector))
                for cid, vector in stored.items()
            }

    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in chunks:
        lex = lexical.get(chunk["id"], 0.0)
        sem = semantic.get(chunk["id"])
        score = (0.65 * sem + 0.35 * lex) if sem is not None else lex
        if score >= MIN_RELEVANCE:
            scored.append((score, chunk))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] < MIN_TOP_RELEVANCE:
        return []
    return [
        {
            "id": c["id"],
            "title": c["title"],
            "doc": c["doc"],
            "source": c["source"],
            "body": c["body"],
            "score": round(score, 3),
        }
        for score, c in scored[:k]
    ]


def sources_of(passages: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Distinct documents behind an answer, in the order they were used."""
    seen: dict[str, dict[str, str]] = {}
    for passage in passages:
        seen.setdefault(passage["doc"], {"title": passage["doc"], "section": passage["title"]})
    return list(seen.values())
