"""The ``abort_if`` guards that watch the TARGET, not the generator.

``AbortIf`` has always said, in its own docstring, that "every one of these is
checked continuously during a run". Two of them were not checked at all:
``sut_cpu_pct`` and ``skipped_searches_delta`` were declared, parsed out of
scenario YAML, and then read by nothing. A scenario could ask for a CPU ceiling
on a production search head, lint clean, and run with no such ceiling. That is
worse than the feature being absent, because the operator believed there was a
safety net.

This module is the missing half. It polls the target's own telemetry during a
run and stops the run when a ceiling is crossed, which is what the other two
guards already do from locally-measured numbers.

Design notes, all of them about not making things worse
-------------------------------------------------------
**A breach must be sustained.** One scraped sample is not evidence: CPU on a
search head spikes for all sorts of reasons that are not this load test, and a
guard that kills a customer's run on a single reading would be a worse bug than
the one it fixes. Two consecutive readings over the line are required.

**A poll failure never aborts.** The account running the test may have no read
access to ``_introspection`` or ``_internal``; a customer's RBAC is their
business. An unevaluable guard goes into the result as unevaluated, and the run
continues on the guards that do work.

**...but an unevaluable guard is never silent.** The whole point of this change
is that a requested guard should not be a lie. :meth:`TargetGuard.status`
reports which guards were live, which never produced a reading and why, and the
run summary carries it. One probe is taken at the start, so the operator learns
within seconds rather than at the end.

**The poll is cheap and bounded.** One search per interval (30 s by default)
against an index the test is not measuring. A load test issuing thousands of
searches a minute is not perturbed by two a minute, and the cost is stated
rather than hidden.

**Nothing here raises into the run loop.** Every path is caught; the task's job
is to call ``request_stop`` and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .scenario import AbortIf
from .splunk import SplunkClient
from .timepolicy import TimeWindow

log = logging.getLogger("regulator.targetguard")

#: How often the target's telemetry is sampled. Two searches a minute against
#: an internal index, which is noise next to any real load test.
DEFAULT_POLL_S = 30.0

#: Consecutive readings over the ceiling before the run is stopped.
BREACH_SAMPLES = 2

#: How far back each poll looks. Wider than the poll interval so a late
#: ``_introspection`` write is not read as "no data".
LOOKBACK_S = 180.0

_CPU_SPL = (
    "search index=_introspection sourcetype=splunk_resource_usage component=Hostwide "
    "| stats max('data.cpu_user_pct') as cpu by host "
    "| sort 0 - cpu | head 1"
)

# Scheduler skips are the target telling you it has run out of capacity, which
# is exactly the damage a load test should stop before doing. Counted as a
# delta from the run's own baseline so a cluster that was already skipping
# before the test began is not blamed on the test.
_SKIPPED_SPL = (
    "search index=_internal sourcetype=scheduler status=skipped "
    "| stats count as skipped"
)


@dataclass
class GuardState:
    """One guard's live condition, for the result."""

    name: str
    limit: float
    live: bool = False
    readings: int = 0
    latest: Optional[float] = None
    peak: Optional[float] = None
    breaches: int = 0
    unavailable_reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "guard": self.name,
            "limit": self.limit,
            "live": self.live,
            "readings": self.readings,
            "latest": self.latest,
            "peak": self.peak,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class TargetGuard:
    """Polls the target and stops the run when a declared ceiling is crossed."""

    client: Optional[SplunkClient]
    guards: AbortIf
    request_stop: Callable[[str], None]
    poll_s: float = DEFAULT_POLL_S
    now: Callable[[], float] = time.time
    _states: Dict[str, GuardState] = field(default_factory=dict)
    _baseline_skipped: Optional[float] = None
    _task: Optional[asyncio.Task] = None
    _stopped: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def wanted(self) -> bool:
        """Whether the scenario asked for either target-side guard."""
        return (
            self.guards.sut_cpu_pct is not None
            or self.guards.skipped_searches_delta is not None
        )

    def __post_init__(self) -> None:
        if self.guards.sut_cpu_pct is not None:
            self._states["sut_cpu_pct"] = GuardState(
                name="sut_cpu_pct", limit=float(self.guards.sut_cpu_pct)
            )
        if self.guards.skipped_searches_delta is not None:
            self._states["skipped_searches_delta"] = GuardState(
                name="skipped_searches_delta",
                limit=float(self.guards.skipped_searches_delta),
            )

    async def start(self) -> None:
        """Take one reading, then poll in the background.

        The first reading is taken inline on purpose: an operator who asked for
        a CPU ceiling should learn in the first seconds that the account cannot
        read ``_introspection``, not discover it in the summary of a run they
        believed was guarded.
        """
        if not self.wanted or self._task is not None:
            return
        if self.client is None:
            # The browser engine drives Chromium and has no splunkd client to
            # poll from. Saying that is far more use than the AttributeError
            # the generic handler would have recorded.
            for state in self._states.values():
                state.unavailable_reason = (
                    "this engine has no splunkd client to poll the target with "
                    "(a browser scenario); run an api-engine scenario for a "
                    "target-side ceiling"
                )
                log.warning("target guard %s cannot be evaluated: %s",
                            state.name, state.unavailable_reason)
            return
        await self._poll_once(first=True)
        for state in self._states.values():
            if state.live:
                log.info(
                    "target guard %s is live (limit %.4g, first reading %.4g)",
                    state.name, state.limit, state.latest if state.latest is not None else -1,
                )
            else:
                log.warning(
                    "target guard %s was requested but cannot be evaluated: %s. The run "
                    "continues WITHOUT it.", state.name, state.unavailable_reason,
                )
        self._task = asyncio.create_task(self._loop(), name="target-guard")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown is best effort
            pass

    def status(self) -> Dict[str, Any]:
        """What each requested guard actually did, for the run summary.

        ``unevaluated`` is the field that stops this being a lie: a guard named
        here was asked for and never produced a reading, so the run was not
        protected by it whatever the result says.
        """
        if not self._states:
            return {}
        return {
            "guards": [state.as_dict() for state in self._states.values()],
            "unevaluated": sorted(
                name for name, state in self._states.items() if not state.live
            ),
        }

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self.poll_s)
            except asyncio.CancelledError:
                return
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - a guard never breaks the run
                log.warning("target guard poll failed: %s", exc)

    async def _poll_once(self, first: bool = False) -> None:
        if "sut_cpu_pct" in self._states:
            await self._poll_cpu(self._states["sut_cpu_pct"])
        if "skipped_searches_delta" in self._states:
            await self._poll_skipped(self._states["skipped_searches_delta"], first=first)

    async def _value(self, spl: str, column: str) -> Optional[float]:
        """One number from a oneshot search, or None with the reason recorded."""
        now = self.now()
        window = TimeWindow(earliest=now - LOOKBACK_S, latest=now)
        rows, _ = await self.client.oneshot(spl, window, count=1)
        if not rows:
            return None
        raw = rows[0].get(column)
        if raw in (None, ""):
            return None
        return float(raw)

    async def _poll_cpu(self, state: GuardState) -> None:
        try:
            value = await self._value(_CPU_SPL, "cpu")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_unavailable(state, f"{type(exc).__name__}: {exc}")
            return
        if value is None:
            self._mark_unavailable(
                state,
                "no splunk_resource_usage events in _introspection for the last "
                f"{LOOKBACK_S / 60:.0f} minutes (introspection disabled, or this "
                "account cannot read that index)",
            )
            return
        self._record(state, value)
        if value > state.limit:
            self._breach(
                state,
                f"the target's peak CPU reached {value:.0f}% against a "
                f"{state.limit:.0f}% ceiling",
            )
        else:
            state.breaches = 0

    async def _poll_skipped(self, state: GuardState, first: bool = False) -> None:
        try:
            total = await self._value(_SKIPPED_SPL, "skipped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mark_unavailable(state, f"{type(exc).__name__}: {exc}")
            return
        if total is None:
            # No skipped searches at all is a legitimate zero, not an error,
            # once the baseline exists. Before that it means the search worked
            # and the cluster is simply healthy.
            total = 0.0
        if first or self._baseline_skipped is None:
            self._baseline_skipped = total
            self._record(state, 0.0)
            return
        delta = max(0.0, total - self._baseline_skipped)
        self._record(state, delta)
        if delta > state.limit:
            self._breach(
                state,
                f"the target skipped {delta:.0f} scheduled searches during the run "
                f"against a ceiling of {state.limit:.0f}",
            )
        else:
            state.breaches = 0

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def _record(self, state: GuardState, value: float) -> None:
        state.live = True
        state.unavailable_reason = None
        state.readings += 1
        state.latest = value
        state.peak = value if state.peak is None else max(state.peak, value)

    def _mark_unavailable(self, state: GuardState, reason: str) -> None:
        # Only the FIRST reason is kept, and only while the guard has never
        # produced a reading: a guard that worked and then hit one transient
        # error is still a live guard.
        if state.live:
            log.warning("target guard %s: a poll failed (%s); the guard stays live",
                        state.name, reason)
            return
        if state.unavailable_reason is None:
            state.unavailable_reason = reason

    def _breach(self, state: GuardState, message: str) -> None:
        state.breaches += 1
        if state.breaches < BREACH_SAMPLES:
            # One sample is not evidence. A search head spikes for reasons that
            # have nothing to do with this test, and killing a customer's run
            # on a single reading would be worse than the gap this fixes.
            log.warning("%s (reading %d of %d before the run is stopped)",
                        message, state.breaches, BREACH_SAMPLES)
            return
        log.error("aborting: %s", message)
        self.request_stop(message)


def guard_notes(status: Dict[str, Any]) -> List[str]:
    """Findings for the report: which requested guards did not run.

    Reported as a finding rather than a log line because the person reading a
    run afterwards is the one who needs to know the ceiling they asked for was
    never in force.
    """
    notes: List[str] = []
    for name in status.get("unevaluated") or []:
        reason = next(
            (g.get("unavailable_reason") for g in status.get("guards") or []
             if g.get("guard") == name),
            None,
        )
        notes.append(
            f"abort_if.{name} was declared but never evaluated"
            + (f": {reason}" if reason else "")
            + ". The run was NOT protected by that ceiling"
        )
    return notes


__all__ = ["BREACH_SAMPLES", "DEFAULT_POLL_S", "GuardState", "TargetGuard", "guard_notes"]
