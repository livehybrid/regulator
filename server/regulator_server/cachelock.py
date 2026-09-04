"""One eviction at a time per target.

The Evict and Purge buttons, a run's eviction before it starts and a run's
periodic eviction all list and evict the same indexers; two at once double
the listing load on a cluster under test and confuse the confirmed counts.
Shared here because the routes and the runner both need it and must not
import each other.
"""

from __future__ import annotations

import asyncio
from typing import Dict

_locks: Dict[int, asyncio.Lock] = {}


def evict_lock(target_id: int) -> asyncio.Lock:
    return _locks.setdefault(int(target_id), asyncio.Lock())
