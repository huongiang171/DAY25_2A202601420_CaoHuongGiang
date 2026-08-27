from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache with semantic similarity and false-hit guardrails.

    Uses n-gram cosine similarity for semantic matching.
    Privacy guardrails via _is_uncacheable().
    False-hit detection via _looks_like_false_hit().
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity.

        1. Return (None, 0.0) if _is_uncacheable(query) — privacy check
        2. Evict expired entries (compare time.time() - created_at vs ttl_seconds)
        3. Find best matching entry using self.similarity(query, entry.key)
        4. If best_score >= similarity_threshold:
           a. Check _looks_like_false_hit(query, best_key) — if true, log and return (None, best_score)
           b. Otherwise return (best_value, best_score)
        5. Return (None, best_score) if no match above threshold
        """
        if _is_uncacheable(query):
            return None, 0.0

        # Evict expired entries
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        # Find best match
        best_score = 0.0
        best_entry: CacheEntry | None = None
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append({
                    "query": query,
                    "cached_key": best_entry.key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                })
                return None, best_score
            return best_entry.value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache.

        1. Return immediately if _is_uncacheable(query)
        2. Append a CacheEntry to self._entries
        """
        if _is_uncacheable(query):
            return
        self._entries.append(
            CacheEntry(key=query, value=value, created_at=time.time(), metadata=metadata or {})
        )

    def flush(self) -> None:
        """Remove all entries from cache (for testing)."""
        self._entries = []
        self.false_hit_log = []

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings using cosine over character n-grams + word tokens.

        1. If a == b, return 1.0
        2. Tokenize both strings: split into words + character n-grams (n=3)
           e.g., "hello world" → ["hello", "world", "hel", "ell", "llo", "wor", "orl", "rld"]
        3. Build Counter (bag-of-words) vectors from these tokens
        4. Compute cosine similarity: dot(a,b) / (|a| * |b|)
        """
        if a == b:
            return 1.0

        def tokenize(s: str) -> list[str]:
            lowered = s.lower()
            words = lowered.split()
            trigrams = [lowered[i : i + 3] for i in range(len(lowered) - 2)]
            return words + trigrams

        va = Counter(tokenize(a))
        vb = Counter(tokenize(b))

        dot = sum(va[t] * vb[t] for t in va if t in vb)
        norm_a = math.sqrt(sum(v * v for v in va.values()))
        norm_b = math.sqrt(sum(v * v for v in vb.values()))

        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model:
        Key    = "{prefix}{query_hash}"   (Redis namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        
        # Local fallback cache for graceful degradation
        self._fallback_cache = ResponseCache(ttl_seconds, similarity_threshold)
        self._redis_down = False
        
        try:
            self._redis: Any | None = redis_lib.Redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
        except Exception:
            self._redis = None
            self._redis_down = True

    def ping(self) -> bool:
        """Check Redis connectivity."""
        if self._redis_down or self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            self._redis_down = True
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        1. Return (None, 0.0) if _is_uncacheable(query)
        2. Build exact-match key and try hget — if found return (response, 1.0)
        3. Otherwise scan all keys and find best similarity match
        4. Apply false-hit check before returning
        """
        if _is_uncacheable(query):
            return None, 0.0

        if self._redis_down or self._redis is None:
            return self._fallback_cache.get(query)

        try:
            # Exact match first
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            response = self._redis.hget(exact_key, "response")
            if response:
                return response, 1.0

            # Similarity scan
            best_score = 0.0
            best_response: str | None = None
            best_cached_query: str | None = None

            for k in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(k, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_response = self._redis.hget(k, "response")
                    best_cached_query = cached_query

            if best_response is not None and best_score >= self.similarity_threshold:
                if _looks_like_false_hit(query, best_cached_query or ""):
                    self.false_hit_log.append({
                        "query": query,
                        "cached_key": best_cached_query,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                    })
                    return None, best_score
                return best_response, best_score

            return None, best_score
        except Exception:
            self._redis_down = True
            return self._fallback_cache.get(query)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        1. Return immediately if _is_uncacheable(query)
        2. Build key: f"{self.prefix}{self._query_hash(query)}"
        3. hset with mapping {"query": query, "response": value}
        4. expire with self.ttl_seconds
        """
        if _is_uncacheable(query):
            return
            
        if self._redis_down or self._redis is None:
            self._fallback_cache.set(query, value, metadata)
            return

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            self._redis_down = True
            self._fallback_cache.set(query, value, metadata)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        self._fallback_cache.flush()
        if not self._redis_down and self._redis is not None:
            try:
                for key in self._redis.scan_iter(f"{self.prefix}*"):
                    self._redis.delete(key)
            except Exception:
                self._redis_down = True

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
