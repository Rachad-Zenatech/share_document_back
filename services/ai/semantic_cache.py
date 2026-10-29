"""Semantic cache: reuses an earlier AI answer when a new question means the same thing, so repeated questions
don't spend Gemini quota.

A lookup first tries an exact match on the normalised question, then a meaning match: the question is embedded
(Gemini embeddings cut to AI_SEMANTIC_CACHE_DIMENSIONS, 256 by default, and normalised so a dot product is the
cosine similarity) and compared in one SQL query with the cached questions of the same feature and scope. A hit
needs similarity of at least AI_SEMANTIC_CACHE_THRESHOLD (0.95) AND exactly the same numbers in both questions:
"is a $300 hotel OK?" and "is a $3000 hotel OK?" are close in meaning but need different answers.

Scope keeps answers from crossing between people who would get different answers. The chat uses a fingerprint of
the tools the person may use, so people with the same access share answers and nobody sees an answer built from
tools they can't use. Entries expire after AI_SEMANTIC_CACHE_TTL_HOURS (24); clear() drops a feature's entries when
what the answers were built from changes.

Only used for answers that depend on nothing but the question (single-question chats without attachments).
Off with AI_SEMANTIC_CACHE_ENABLED=false or without GEMINI_API_KEY.
"""

import hashlib
import logging
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

from google import genai
from google.genai import types

from postgresql_db.database import get_pool
from services.ai.ai_observability import span

logger = logging.getLogger(__name__)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


THRESHOLD = _env_float("AI_SEMANTIC_CACHE_THRESHOLD", 0.95)
TTL_HOURS = _env_float("AI_SEMANTIC_CACHE_TTL_HOURS", 24)
DIMENSIONS = int(_env_float("AI_SEMANTIC_CACHE_DIMENSIONS", 256))
CANDIDATES = 2000  # newest live entries compared per lookup


def is_enabled() -> bool:
    return (os.getenv("AI_SEMANTIC_CACHE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
            and bool(os.getenv("GEMINI_API_KEY")))


@dataclass
class Hit:
    response: str
    score: float
    model: Optional[str]
    exact: bool


@dataclass
class Lookup:
    """What a lookup learned, so storing the answer afterwards doesn't embed the question again."""
    hit: Optional[Hit]
    normalised: str
    query_hash: str
    numbers: List[str]
    embedding: Optional[List[float]] = None


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%$.,-]", " ", text)).strip(" .,")


def numbers(text: str) -> List[str]:
    return sorted({n.replace(",", "") for n in _NUMBER.findall(text)})


_client: Optional[genai.Client] = None


def _gemini_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


async def _embed(text: str) -> List[float]:
    client = _gemini_client()
    model = (os.getenv("AI_SEMANTIC_CACHE_EMBEDDING_MODEL", "").strip()
             or os.getenv("VECTOR_EMBEDDING_MODEL", "").strip() or "gemini-embedding-2")
    async with span("embedding", "gemini.embed_content", model=model, texts=1, chars=len(text), purpose="semantic_cache"):
        res = await client.aio.models.embed_content(
            model=model,
            contents=[types.Content(parts=[types.Part(text=text)])],
            config=types.EmbedContentConfig(output_dimensionality=DIMENSIONS, task_type="SEMANTIC_SIMILARITY"),
        )
    values = list((res.embeddings or [])[0].values or [])
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


async def lookup(feature: str, scope: str, question: str) -> Lookup:
    """A cached answer to the question, if any. Never raises: a cache problem just means answering normally."""
    norm = normalise(question)
    found = Lookup(None, norm, hashlib.sha256(norm.encode("utf-8")).hexdigest(), numbers(norm))
    if not is_enabled() or not norm:
        return found
    try:
        return await _lookup(feature, scope, found)
    except Exception as e:
        logger.warning("Semantic cache lookup failed (answering without it): %s", e)
        found.hit = None
        return found


async def _lookup(feature: str, scope: str, found: Lookup) -> Lookup:
    norm = found.normalised
    async with span("cache", "semantic_cache.lookup", feature=feature) as s:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE ai_semantic_cache SET hits = hits + 1, last_hit_at = NOW()
                WHERE feature = $1 AND scope = $2 AND query_hash = $3 AND expires_at > NOW()
                RETURNING response, model
                """,
                feature, scope, found.query_hash,
            )
            if row:
                found.hit = Hit(row["response"], 1.0, row["model"], exact=True)
            else:
                try:
                    found.embedding = await _embed(norm)
                except Exception as e:
                    logger.warning("Semantic cache: couldn't embed the question (%s); answering without the cache", e)
                    s.attributes["result"] = "embed_failed"
                    return found
                row = await conn.fetchrow(
                    """
                    WITH candidates AS (
                        SELECT id, response, model, embedding FROM ai_semantic_cache
                        WHERE feature = $1 AND scope = $2 AND numbers = $3::text[] AND expires_at > NOW()
                          AND cardinality(embedding) = cardinality($4::real[])
                        ORDER BY created_at DESC LIMIT $6
                    ), best AS (
                        SELECT id, response, model,
                               (SELECT sum(a * b) FROM unnest(embedding, $4::real[]) AS t(a, b)) AS score
                        FROM candidates ORDER BY score DESC LIMIT 1
                    )
                    UPDATE ai_semantic_cache c SET hits = c.hits + 1, last_hit_at = NOW()
                    FROM best WHERE c.id = best.id AND best.score >= $5
                    RETURNING best.response, best.model, best.score
                    """,
                    feature, scope, found.numbers, found.embedding, THRESHOLD, CANDIDATES,
                )
                if row:
                    found.hit = Hit(row["response"], float(row["score"]), row["model"], exact=False)
        s.attributes.update(
            result="hit" if found.hit else "miss",
            exact=found.hit.exact if found.hit else None,
            score=round(found.hit.score, 4) if found.hit else None,
        )
    return found


async def store(feature: str, scope: str, looked_up: Lookup, response: str, model: Optional[str]) -> None:
    """Cache an answer for the question of an earlier lookup. Failures are logged, never raised."""
    if not is_enabled() or not looked_up.normalised or not response.strip():
        return
    try:
        embedding = looked_up.embedding or await _embed(looked_up.normalised)
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_semantic_cache (feature, scope, query_hash, query_text, numbers, embedding, response,
                                               model, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() + make_interval(secs => $9))
                ON CONFLICT (feature, scope, query_hash) DO UPDATE SET
                    response = EXCLUDED.response, model = EXCLUDED.model, embedding = EXCLUDED.embedding,
                    created_at = NOW(), expires_at = EXCLUDED.expires_at, hits = 0
                """,
                feature, scope, looked_up.query_hash, looked_up.normalised, looked_up.numbers, embedding,
                response, model, TTL_HOURS * 3600,
            )
    except Exception as e:
        logger.warning("Semantic cache: couldn't store the answer: %s", e)


async def clear(feature: Optional[str] = None) -> int:
    """Drop cached answers (one feature's, or all) when what they were built from has changed."""
    pool = get_pool()
    async with pool.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM ai_semantic_cache WHERE $1::text IS NULL OR feature = $1", feature,
        )
    return int(status.split()[-1]) if status.startswith("DELETE") else 0
