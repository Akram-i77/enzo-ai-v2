#!/usr/bin/env python3
"""
ENZO - Dual-Tier Caching System (L1 In-Memory TTL + L2 SQLite Store)
"""
import time
import threading
from typing import Any, Tuple, Optional
from enzo.core.db import cache_get as l2_cache_get, cache_set as l2_cache_set, cache_delete as l2_cache_delete

_L1_CACHE = {}
_L1_LOCK = threading.Lock()
_L1_MAXSIZE = 500  # bounded L1: evict oldest entry when full


def get(key: str) -> Tuple[Optional[Any], Optional[float]]:
    """Retrieve key from L1 memory cache, then fall back to L2 SQLite cache.
    Returns (value, age_in_seconds) or (None, None).
    """
    now = time.time()
    with _L1_LOCK:
        if key in _L1_CACHE:
            val, ts, ttl = _L1_CACHE[key]
            age = now - ts
            if age <= ttl:
                return val, age
            else:
                del _L1_CACHE[key]

    # Fallback to L2
    val, age, ttl = l2_cache_get(key)
    if val is not None:
        # Populate L1 with the REAL ttl (previously hardcoded 300s, which
        # could serve data far older than its configured lifetime)
        with _L1_LOCK:
            if len(_L1_CACHE) >= _L1_MAXSIZE and key not in _L1_CACHE:
                try:
                    _L1_CACHE.pop(next(iter(_L1_CACHE)))  # FIFO eviction
                except StopIteration:
                    pass
            _L1_CACHE[key] = (val, now - age, ttl)
        return val, age

    return None, None


def set(key: str, value: Any, ttl: float = 300.0):
    """Store key in both L1 memory cache and L2 SQLite cache."""
    now = time.time()
    with _L1_LOCK:
        _L1_CACHE[key] = (value, now, ttl)
    l2_cache_set(key, value, ttl=ttl)


def delete(key: str):
    """Remove key from L1 and L2 caches."""
    with _L1_LOCK:
        _L1_CACHE.pop(key, None)
    l2_cache_delete(key)


def clear_l1():
    """Clear memory tier cache."""
    with _L1_LOCK:
        _L1_CACHE.clear()
