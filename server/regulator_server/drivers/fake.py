"""An in-memory driver, and the one that spawns real workers as subprocesses.

Two jobs. In bookkeeping mode it records what was asked and reports it as
running, which is enough for the lifecycle tests. In spawn mode it launches
the real ``regulator_agent`` as local subprocesses in managed mode, pointed at
a control plane on a real socket, which is how the fleet protocol is proven
end to end without a swarm: the same code that runs in a container, the same
claim, the same heartbeats, the same final report.
"""

from __future__ import annotations

import itertools
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Set

from .base import DriverError, DriverRef, DriverStatus, WorkerGroup

log = logging.getLogger("regulator.driver.fake")

_ids = itertools.count(1)
_LOG_CAP = 4000


class _Group:
    def __init__(self, group: WorkerGroup) -> None:
        self.group = group
        self.desired = group.workers
        self.stopped = False
        self.destroyed = False
        self.procs: List[subprocess.Popen] = []
        self.log_lines: List[str] = []


class FakeDriver:
    kind = "fake"

    def __init__(
        self,
        spawn: bool = False,
        python: Optional[str] = None,
        cwd: Optional[str] = None,
        env_overrides: Optional[Dict[str, str]] = None,
        capture_logs: bool = True,
    ) -> None:
        self._spawn = spawn
        self._python = python or sys.executable
        self._cwd = cwd
        self._env_overrides = dict(env_overrides or {})
        self._capture = capture_logs
        self._groups: Dict[str, _Group] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ test aids

    def desired_for(self, ref: DriverRef) -> int:
        with self._lock:
            state = self._groups.get(ref.id)
            return state.desired if state and not state.destroyed else 0

    def is_destroyed(self, ref: DriverRef) -> bool:
        with self._lock:
            state = self._groups.get(ref.id)
            return state is None or state.destroyed

    def created_groups(self) -> List[WorkerGroup]:
        with self._lock:
            return [state.group for state in self._groups.values()]

    # -------------------------------------------------------------- driver

    def create(self, group: WorkerGroup) -> DriverRef:
        if group.workers < 1:
            raise DriverError("workers must be at least 1")
        group_id = f"fake-run-{group.run_id}-{group.group}-{next(_ids)}"
        state = _Group(group)
        with self._lock:
            self._groups[group_id] = state
        log.info("fake driver created %s desired=%d image=%s", group_id, group.workers, group.image)
        if self._spawn:
            self._spawn_workers(state)
        return DriverRef(kind=self.kind, id=group_id, group=group.group, raw={"run_id": group.run_id})

    def _spawn_workers(self, state: _Group) -> None:
        for index in range(state.group.workers):
            env = dict(os.environ)
            env.update(state.group.env)
            env.update(state.group.secrets)  # a subprocess has no secret store
            env["REG_HINT_SLOT"] = str(index + state.group.options.get("slot_base", 0))
            env["REG_HOLDER"] = f"{state.group.group}-{index}"
            env["REG_WORKER_ENGINE"] = state.group.group
            env.update(self._env_overrides)
            proc = subprocess.Popen(
                [self._python, "-m", "regulator_agent"],
                env=env,
                cwd=self._cwd,
                stdout=subprocess.PIPE if self._capture else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self._capture else subprocess.DEVNULL,
                text=True,
            )
            state.procs.append(proc)
            if self._capture and proc.stdout is not None:
                threading.Thread(
                    target=self._pump, args=(state, proc), name=f"fake-worker-log-{index}", daemon=True
                ).start()

    def _pump(self, state: _Group, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            with self._lock:
                state.log_lines.append(line.rstrip("\n"))
                if len(state.log_lines) > _LOG_CAP:
                    del state.log_lines[: len(state.log_lines) - _LOG_CAP]

    def stop(self, ref: DriverRef, grace_s: int) -> None:
        with self._lock:
            state = self._groups.get(ref.id)
            if state is None:
                return
            state.stopped = True
            procs = list(state.procs)
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass

    def destroy(self, ref: DriverRef) -> None:
        with self._lock:
            state = self._groups.get(ref.id)
            if state is None:
                return
            state.destroyed = True
            state.desired = 0
            procs = list(state.procs)
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

    def status(self, ref: DriverRef) -> DriverStatus:
        with self._lock:
            state = self._groups.get(ref.id)
            if state is None or state.destroyed:
                return DriverStatus(desired=0, running=0)
            if self._spawn:
                running = sum(1 for proc in state.procs if proc.poll() is None)
            else:
                running = 0 if state.stopped else state.desired
            return DriverStatus(desired=state.desired, running=running)

    def logs(self, ref: DriverRef, tail: int) -> str:
        with self._lock:
            state = self._groups.get(ref.id)
            if state is None:
                return ""
            lines = state.log_lines[-tail:] if tail > 0 else state.log_lines
            return "\n".join(lines)

    def list_run_ids(self) -> Set[int]:
        with self._lock:
            return {state.group.run_id for state in self._groups.values() if not state.destroyed}

    def wait(self, ref: DriverRef, timeout_s: float) -> None:
        """Test aid: wait for spawned workers to exit."""
        with self._lock:
            state = self._groups.get(ref.id)
            procs = list(state.procs) if state else []
        for proc in procs:
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
