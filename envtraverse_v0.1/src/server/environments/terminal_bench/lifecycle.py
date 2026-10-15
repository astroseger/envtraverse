"""Worker-local lifecycle and an advisory view of Harbor's solving clock.

Harbor, not this module, enforces the time limit. Clock readings are anchored at
entry to the bridge run() call, just inside Harbor's timed scope. No new timer or
budget override is introduced here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import time
from typing import Any, Callable


@dataclass
class SessionLifecycle:
    phase: str = "preparing"
    termination_reason: str | None = None
    verification_started: bool = False
    abort_requested: bool = False
    agent_timeout_seconds: float | None = None
    started_at: str | None = None
    deadline_at: str | None = None
    _started: float | None = None
    _ended: float | None = None
    _agent_task: asyncio.Task | None = field(default=None, repr=False)
    on_change: Callable[[], None] | None = field(default=None, repr=False)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def configure_timeout(self, seconds: float | None) -> None:
        if seconds is not None:
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError("Invalid resolved Harbor agent timeout")
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("Resolved Harbor agent timeout must be positive and finite")
            seconds = float(seconds)
        self.agent_timeout_seconds = seconds
        self._changed()

    def begin_solving(self, task: asyncio.Task | None = None) -> None:
        if self._started is not None:
            raise RuntimeError("The solving clock must not be restarted")
        self._started = time.monotonic()
        utc = datetime.now(timezone.utc)
        self.started_at = utc.isoformat()
        if self.agent_timeout_seconds is not None:
            self.deadline_at = (utc + timedelta(seconds=self.agent_timeout_seconds)).isoformat()
        self._agent_task = task
        self.phase = "solving"
        self._changed()

    @property
    def solving_ended(self) -> bool:
        return self.phase in {"finalizing", "verifying", "finished"}

    @property
    def deadline_elapsed(self) -> bool:
        return (self._started is not None and self.agent_timeout_seconds is not None
                and time.monotonic() >= self._started + self.agent_timeout_seconds)

    @property
    def preserve_finalization(self) -> bool:
        if self.abort_requested:
            return False
        # The cancellation may already have been delivered by wait_for, before
        # the bridge's finally clause has had its turn. Do not cancel its parent
        # runner as well: that would prevent Harbor from grading after timeout.
        cancelling = self._agent_task is not None and self._agent_task.cancelling() > 0
        return self.solving_ended or (self.phase == "solving" and
                                     (self.deadline_elapsed or cancelling))

    def request_abort(self) -> None:
        self.abort_requested = True
        self.termination_reason = "aborted"
        self.phase = "aborting"
        self._changed()

    def end_solving(self, reason: str | None = None) -> None:
        if self._started is not None and self._ended is None:
            self._ended = time.monotonic()
        if self.abort_requested:
            self.phase, self.termination_reason = "aborting", "aborted"
        else:
            if self.phase not in {"verifying", "finished"}:
                self.phase = "finalizing"
            if reason is not None and self.termination_reason != "agent_timeout":
                self.termination_reason = reason
        self._changed()

    def begin_verification(self, exception_type: str | None = None) -> None:
        self.end_solving()
        self.verification_started = True
        self.phase = "verifying"
        if exception_type == "AgentTimeoutError":
            self.termination_reason = "agent_timeout"
        self._changed()

    def finish(self, reason: str | None = None) -> None:
        if self._started is not None and self._ended is None:
            self._ended = time.monotonic()
        self.phase = "finished"
        if reason is not None:
            self.termination_reason = reason
        self._changed()

    def time_budget(self) -> dict[str, Any]:
        elapsed = (None if self._started is None else
                   max(0.0, (self._ended if self._ended is not None else time.monotonic()) - self._started))
        remaining = (None if elapsed is None or self.agent_timeout_seconds is None else
                     max(0.0, self.agent_timeout_seconds - elapsed))
        return {
            "phase": self.phase,
            "agent_timeout_seconds": self.agent_timeout_seconds,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "elapsed_seconds": None if elapsed is None else round(elapsed, 3),
            "remaining_seconds": None if remaining is None else round(remaining, 3),
            "termination_reason": self.termination_reason,
            "deadline_is_estimate": True,
        }

    def record(self) -> dict[str, Any]:
        return {**self.time_budget(), "verification_started": self.verification_started,
                "abort_requested": self.abort_requested,
                "preserve_finalization": self.preserve_finalization}
