"""
Semantic stock search — nomic-embed-text-v1.5 + Pinecone + RRF.

search()       — single-query search
multi_search() — parallel multi-query search with Reciprocal Rank Fusion
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from pinecone import Pinecone
from sentence_transformers import SentenceTransformer

load_dotenv(Path(__file__).parent / ".env")

PINECONE_API_KEY = os.environ["PINECONE_API_KEY"]
PINECONE_INDEX   = os.environ.get("PINECONE_INDEX", "meta-analyst-stocks")
EMBED_MODEL      = "nomic-ai/nomic-embed-text-v1.5"

pc    = Pinecone(api_key=PINECONE_API_KEY)
index = pc.Index(PINECONE_INDEX)


def embed_query(model: SentenceTransformer, text: str, prefix: str = "search_query") -> np.ndarray:
    return model.encode(f"{prefix}: {text}", normalize_embeddings=True).astype(np.float32)


def _pinecone_query(search_vec: list[float], fetch_n: int) -> object:
    return index.query(
        vector=search_vec,
        top_k=fetch_n,
        include_metadata=True,
    )


def _parse_match(match, rrf_score: float | None = None) -> dict:
    meta = match.metadata or {}
    return {
        "id":            match.id,
        "score":         float(match.score),
        "rrf_score":     rrf_score,
        "ticker":        meta.get("ticker", ""),
        "stock_name":    meta.get("stock_name", ""),
        "sector":        meta.get("sector", ""),
        "type":          meta.get("type", "stock_profile"),
        "is_candidate":  bool(meta.get("is_candidate", False)),
        "health_pass":   bool(meta.get("health_pass", False)),
        "insider_flag":  bool(meta.get("insider_flag", False)),
        "best_wr6":      float(meta.get("best_wr6", 0.0)),
        "total_signals": int(meta.get("total_signals", 0)),
        "document":      meta.get("document", ""),
    }


def search(
    query: str,
    model: SentenceTransformer,
    top_n: int = 10,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """Single-query Pinecone search."""
    vec      = embed_query(model, query).tolist()
    fetch_n  = min(top_n * 4, 200)
    response = _pinecone_query(vec, fetch_n)

    results  = []
    seen     = exclude_ids or set()
    for match in response.matches:
        if len(results) >= top_n:
            break
        if match.id in seen or match.metadata.get("type") == "market_overview":
            continue
        results.append(_parse_match(match))

    return results


def multi_search(
    query_texts: list[str],
    query_prefixes: list[str],
    model: SentenceTransformer,
    top_n: int = 10,
    exclude_ids: set[str] | None = None,
    rrf_k: int = 60,
) -> list[dict]:
    """
    Parallel Pinecone queries merged with Reciprocal Rank Fusion.

    query_texts   — [original_query, hyde_hypothesis, rephrased_query]
    query_prefixes — nomic task prefix per text ("search_query" or "search_document")
    rrf_k          — RRF constant: higher = smoother rank weighting (standard: 60)
    """
    fetch_n     = min(top_n * 4, 150)
    search_vecs = [
        embed_query(model, text, prefix).tolist()
        for text, prefix in zip(query_texts, query_prefixes)
    ]

    with ThreadPoolExecutor(max_workers=len(search_vecs)) as pool:
        responses = list(pool.map(lambda sv: _pinecone_query(sv, fetch_n), search_vecs))

    rrf_scores  : dict[str, float] = {}
    best_cosine : dict[str, float] = {}
    best_match  : dict[str, object] = {}

    for response in responses:
        for rank, match in enumerate(response.matches):
            mid    = match.id
            cosine = float(match.score)
            rrf_scores[mid]  = rrf_scores.get(mid, 0.0) + 1.0 / (rank + rrf_k)
            if mid not in best_cosine or cosine > best_cosine[mid]:
                best_cosine[mid] = cosine
                best_match[mid]  = match

    sorted_ids = sorted(rrf_scores, key=rrf_scores.__getitem__, reverse=True)
    seen       = exclude_ids or set()

    results: list[dict] = []
    for mid in sorted_ids:
        if len(results) >= top_n:
            break
        if mid in seen:
            continue
        meta = (best_match[mid].metadata or {})
        if meta.get("type") == "market_overview":
            continue
        doc = _parse_match(best_match[mid], rrf_score=rrf_scores[mid])
        doc["score"] = best_cosine[mid]
        results.append(doc)

    return results
