"""The abort guards that watch the target.

``AbortIf``'s docstring has always claimed "every one of these is checked
continuously during a run". Two of them were not checked at all: ``sut_cpu_pct``
and ``skipped_searches_delta`` were declared, parsed, and read by nothing, so a
scenario could ask for a CPU ceiling on a production search head and run with
no such ceiling. That is worse than the feature being absent, because the
operator believed there was a safety net.

These tests pin the three properties that matter now it exists: a real breach
stops the run, a single noisy sample does not, and a guard that cannot be
evaluated says so instead of passing quietly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))

from regulator_agent.scenario import AbortIf  # noqa: E402
from regulator_agent.targetguard import (  # noqa: E402
    BREACH_SAMPLES,
    TargetGuard,
    guard_notes,
)


class FakeClient:
    """A splunkd client that answers the guard's two searches from a script.

    Each entry is the value the next poll should see, or an Exception to raise,
    or None for "the search worked and returned no rows".
    """

    def __init__(self, cpu: Optional[List[Any]] = None, skipped: Optional[List[Any]] = None):
        self._cpu = list(cpu or [])
        self._skipped = list(skipped or [])
        self._last: Dict[str, Any] = {}
        self.calls = 0

    async def oneshot(self, spl: str, window, count: int = 100) -> Tuple[List[Dict[str, Any]], int]:
        self.calls += 1
        is_cpu = "splunk_resource_usage" in spl
        queue = self._cpu if is_cpu else self._skipped
        column = "cpu" if is_cpu else "skipped"
        if queue:
            value = queue.pop(0)
            self._last[column] = value
        else:
            # A real target keeps reporting its current state rather than going
            # quiet, and the difference matters: a None here would reset the
            # breach counter and hide a sustained breach.
            value = self._last.get(column)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return [], 0
        return [{column: str(value)}], 0


def _guard(**kw) -> Tuple[TargetGuard, List[str]]:
    stops: List[str] = []
    guard = TargetGuard(request_stop=stops.append, **kw)
    return guard, stops


# --------------------------------------------------------------------------- #
# Is the guard even wanted
# --------------------------------------------------------------------------- #

def test_a_scenario_that_asks_for_neither_polls_nothing():
    client = FakeClient(cpu=[99])
    guard, stops = _guard(client=client, guards=AbortIf(error_rate_pct=5.0))
    assert guard.wanted is False
    asyncio.run(guard.start())
    assert client.calls == 0
    assert guard.status() == {}
    assert stops == []


# --------------------------------------------------------------------------- #
# CPU ceiling
# --------------------------------------------------------------------------- #

def test_a_sustained_cpu_breach_stops_the_run():
    client = FakeClient(cpu=[95, 96])
    guard, stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))

    async def drive():
        await guard.start()          # reading 1: over, but one sample is not evidence
        await guard._poll_once()     # reading 2: over again -> stop
        await guard.stop()

    asyncio.run(drive())
    assert len(stops) == 1
    # The message carries the reading that actually tripped it, which is the
    # second one, plus the ceiling it crossed.
    assert "96" in stops[0] and "85" in stops[0]
    assert guard.status()["unevaluated"] == []


def test_one_spike_does_not_kill_a_customers_run():
    """A search head spikes for reasons that are not this load test.

    Killing a run on a single reading would be a worse bug than the gap this
    guard fixes, so a breach has to be sustained.
    """
    assert BREACH_SAMPLES >= 2
    client = FakeClient(cpu=[99, 40, 42])
    guard, stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))

    async def drive():
        await guard.start()
        await guard._poll_once()
        await guard._poll_once()
        await guard.stop()

    asyncio.run(drive())
    assert stops == []
    status = guard.status()["guards"][0]
    assert status["live"] is True
    assert status["peak"] == 99.0
    assert status["readings"] == 3


def test_cpu_below_the_ceiling_never_stops_the_run():
    client = FakeClient(cpu=[50, 60, 70])
    guard, stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))

    async def drive():
        await guard.start()
        await guard._poll_once()
        await guard._poll_once()
        await guard.stop()

    asyncio.run(drive())
    assert stops == []


# --------------------------------------------------------------------------- #
# Skipped searches
# --------------------------------------------------------------------------- #

def test_skipped_searches_are_counted_from_a_baseline():
    """A cluster already skipping before the test must not be blamed on it."""
    client = FakeClient(skipped=[500, 505, 560])
    guard, stops = _guard(client=client, guards=AbortIf(skipped_searches_delta=50))

    async def drive():
        await guard.start()          # baseline 500, delta 0
        await guard._poll_once()     # 505 -> delta 5, under
        await guard._poll_once()     # 560 -> delta 60, over (first breach)
        await guard._poll_once()     # still over -> stop
        await guard.stop()

    asyncio.run(drive())
    assert len(stops) == 1
    assert "skipped" in stops[0]


def test_no_skipped_searches_is_a_zero_not_an_error():
    client = FakeClient(skipped=[None, None])
    guard, stops = _guard(client=client, guards=AbortIf(skipped_searches_delta=10))

    async def drive():
        await guard.start()
        await guard._poll_once()
        await guard.stop()

    asyncio.run(drive())
    assert stops == []
    assert guard.status()["guards"][0]["live"] is True


# --------------------------------------------------------------------------- #
# The honesty requirement
# --------------------------------------------------------------------------- #

def test_a_guard_that_cannot_be_evaluated_says_so_and_does_not_abort():
    """The customer's RBAC is their business; silence is not acceptable.

    The account may have no read access to _introspection. The run continues on
    the guards that do work, and the result records that this ceiling was never
    in force.
    """
    client = FakeClient(cpu=[PermissionError("no read access to _introspection")])
    guard, stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))
    asyncio.run(guard.start())

    assert stops == []
    status = guard.status()
    assert status["unevaluated"] == ["sut_cpu_pct"]
    assert "_introspection" in status["guards"][0]["unavailable_reason"]


def test_an_empty_introspection_index_is_reported_as_unevaluable():
    client = FakeClient(cpu=[None])
    guard, stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))
    asyncio.run(guard.start())
    reason = guard.status()["guards"][0]["unavailable_reason"]
    assert "introspection disabled" in reason or "cannot read" in reason


def test_an_engine_with_no_splunkd_client_explains_itself():
    """The browser engine has no client to poll from."""
    guard, stops = _guard(client=None, guards=AbortIf(sut_cpu_pct=85.0))
    asyncio.run(guard.start())
    assert stops == []
    assert "browser scenario" in guard.status()["guards"][0]["unavailable_reason"]


def test_a_transient_failure_after_a_good_reading_keeps_the_guard_live():
    """One flaky poll does not demote a guard that has been working."""
    client = FakeClient(cpu=[50, TimeoutError("read timeout"), 55])
    guard, stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))

    async def drive():
        await guard.start()
        await guard._poll_once()
        await guard._poll_once()
        await guard.stop()

    asyncio.run(drive())
    status = guard.status()
    assert status["unevaluated"] == []
    assert status["guards"][0]["live"] is True


def test_the_report_says_which_ceiling_was_never_in_force():
    """The person reading the run afterwards is the one who needs to know."""
    client = FakeClient(cpu=[PermissionError("denied")])
    guard, _stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))
    asyncio.run(guard.start())

    notes = guard_notes(guard.status())
    assert len(notes) == 1
    assert "abort_if.sut_cpu_pct was declared but never evaluated" in notes[0]
    assert "NOT protected" in notes[0]


def test_a_working_guard_produces_no_report_note():
    client = FakeClient(cpu=[50])
    guard, _stops = _guard(client=client, guards=AbortIf(sut_cpu_pct=85.0))
    asyncio.run(guard.start())
    assert guard_notes(guard.status()) == []


# --------------------------------------------------------------------------- #
# It must never break the run
# --------------------------------------------------------------------------- #

def test_the_background_loop_survives_a_failing_poll():
    client = FakeClient(cpu=[50, RuntimeError("boom"), 51])
    guard, stops = _guard(
        client=client, guards=AbortIf(sut_cpu_pct=85.0), poll_s=0.01
    )

    async def drive():
        await guard.start()
        await asyncio.sleep(0.08)   # let the loop take several polls
        await guard.stop()

    asyncio.run(drive())
    assert stops == []
    # Still live, and still polling after the exception.
    assert guard.status()["guards"][0]["readings"] >= 2


def test_stop_is_safe_when_nothing_was_ever_started():
    guard, _stops = _guard(client=FakeClient(), guards=AbortIf())
    asyncio.run(guard.stop())
    asyncio.run(guard.stop())
