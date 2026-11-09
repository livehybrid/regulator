"""One cache operation at a time per target.

The Evict and Purge buttons, a run's eviction before it starts, a run's
periodic eviction and the Warm cache button all list, evict or fetch the same
indexers' buckets; two at once double the load on a cluster under test,
confuse the confirmed counts, and a warm-up racing an eviction undoes itself.
Shared here because the routes and the runner both need it and must not
import each other.
"""

from __future__ import annotations

import asyncio
from typing import Dict

_locks: Dict[int, asyncio.Lock] = {}


def evict_lock(target_id: int) -> asyncio.Lock:
    return _locks.setdefault(int(target_id), asyncio.Lock())
