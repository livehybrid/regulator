"""The contract suite: Regulator's assumptions about Splunk, checked against a real one.

Skipped unless REG_CONTRACT_SH_URL is set, so a plain `pytest` never needs a
cluster. harness/contract/up.sh brings up the one these defaults describe.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))

ENV = os.environ
SH_URL = ENV.get("REG_CONTRACT_SH_URL", "")
MEMBER_URLS = [u for u in ENV.get("REG_CONTRACT_MEMBER_URLS", "").split(",") if u]
CM_URL = ENV.get("REG_CONTRACT_CM_URL", "")
INDEXER_URLS = [u for u in ENV.get("REG_CONTRACT_INDEXER_URLS", "").split(",") if u]
HEC_URL = ENV.get("REG_CONTRACT_HEC_URL", "")
HEC_TOKEN = ENV.get("REG_CONTRACT_HEC_TOKEN", "")
USER = ENV.get("REG_CONTRACT_USER", "admin")
PASSWORD = ENV.get("REG_CONTRACT_PASSWORD", "")
INDEX = ENV.get("REG_CONTRACT_INDEX", "contract")

if not SH_URL:
    collect_ignore_glob = ["test_*.py"]


def run(coro):
    return asyncio.run(coro)


def client_for(url: str):
    from regulator_agent.config import TargetConfig
    from regulator_agent.splunk import SplunkClient

    return SplunkClient(TargetConfig(url=url, username=USER, password=PASSWORD, verify_tls=False))


async def with_client(url: str, fn):
    client = client_for(url)
    await client.start()
    try:
        return await fn(client)
    finally:
        await client.close()


async def oneshot(url: str, spl: str, earliest: float = 0.0, latest: Optional[float] = None, count: int = 0) -> List[Dict[str, Any]]:
    from regulator_agent.timepolicy import TimeWindow

    window = TimeWindow(earliest=earliest, latest=latest or time.time() + 3600)
    rows, _ = await with_client(url, lambda c: c.oneshot(spl, window, count=count))
    return rows


def _insecure() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def rest(url: str, path: str, method: str = "GET", data: Optional[Dict[str, str]] = None) -> Any:
    """A management REST call with basic auth, JSON out. Raises on HTTP errors."""
    import base64
    from urllib.parse import urlencode

    body = urlencode(data).encode() if data is not None else None
    sep = "&" if "?" in path else "?"
    request = urllib.request.Request(f"{url}{path}{sep}output_mode=json", data=body, method=method)
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, context=_insecure(), timeout=60) as response:
        return json.loads(response.read() or b"{}")


def hec_send(events: List[Dict[str, Any]]) -> None:
    request = urllib.request.Request(
        f"{HEC_URL}/services/collector/event",
        data="\n".join(json.dumps(e) for e in events).encode(),
        method="POST",
    )
    request.add_header("Authorization", f"Splunk {HEC_TOKEN}")
    with urllib.request.urlopen(request, context=_insecure(), timeout=60) as response:
        assert response.status == 200


def roll_hot_buckets(index: str) -> None:
    for url in INDEXER_URLS:
        rest(url, f"/services/data/indexes/{index}/roll-hot-buckets", method="POST", data={})


def wait_for(predicate, timeout_s: float = 120.0, every_s: float = 3.0, what: str = "condition"):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(every_s)
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what} (last: {last!r})")


@pytest.fixture(scope="session")
def corpus():
    """A fresh batch of events in the contract index and in main, rolled to warm.

    Every event carries this session's tag, a token searched for as a term
    (not as a field: "tag" is Splunk's own, and JSON is not extracted for an
    unconfigured sourcetype), so a test finds exactly this batch however many
    sessions have run against the cluster before.
    """
    tag = uuid.uuid4().hex[:12]
    now = time.time()
    events = []
    for n in range(6000):
        sourcetype = "contract:a" if n % 2 else "contract:b"
        events.append({"time": now - 3600 + n * 0.5, "index": INDEX, "sourcetype": sourcetype,
                       "host": "contract-gen", "event": {"n": n, "batch": tag, "pad": "y" * 300}})
    # The same sourcetype in main, so a search's index scoping can be told apart.
    for n in range(200):
        events.append({"time": now - 1800 + n, "index": "main", "sourcetype": "contract:a",
                       "host": "contract-gen", "event": {"n": n, "batch": tag}})
    for start in range(0, len(events), 1000):
        hec_send(events[start:start + 1000])

    def all_searchable():
        rows = run(oneshot(SH_URL, f'search index={INDEX} {tag} | stats count', earliest=now - 7200))
        return rows and int(rows[0].get("count") or 0) >= 6000

    wait_for(all_searchable, what="the batch to be searchable")
    roll_hot_buckets(INDEX)
    return {"tag": tag, "earliest": now - 3700, "latest": now + 60}
