"""Own one native Harbor Trial from lazy preparation through verification."""
from __future__ import annotations

import asyncio
import importlib.metadata
import hashlib
import json
import logging
import math
from pathlib import Path
import tomllib
from typing import Any

from .bridge import BRIDGES, BridgeState
from .description import environment_description
from .protocol import RuntimeFault, terminal_observation
from .settings import TerminalSettings
from .records import atomic_json, now

LOGGER = logging.getLogger(__name__)
SUPPORTED_HARBOR_VERSION = "0.23.0"


def check_harbor() -> str:
    try:
        version = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("Install the HOST dependencies: pip install '.[host]'") from exc
    if version != SUPPORTED_HARBOR_VERSION:
        raise RuntimeError(
            f"This release targets harbor=={SUPPORTED_HARBOR_VERSION}; found {version}. "
            "Use a dedicated host virtual environment."
        )
    from harbor.trial.trial import Trial
    if not callable(getattr(Trial, "create", None)):
        raise RuntimeError("Installed Harbor lacks the expected Trial.create API")
    return version


def summarize_result(raw: dict[str, Any], session_id: str, task_id: str, *,
                     termination_reason: str | None = None,
                     verification_started: bool = False) -> dict[str, Any]:
    """Separate agent termination from grading, retaining Harbor's reward.

    AgentTimeoutError is special: native Harbor still runs verification after
    it. Other exceptions must not be silently reclassified as successful runs.
    """
    verifier = raw.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    error = raw.get("exception_info")
    exception_type = error.get("exception_type") if isinstance(error, dict) else None
    valid_rewards = (isinstance(rewards, dict) and bool(rewards) and all(
        isinstance(key, str) and type(value) in (int, float) and math.isfinite(value)
        for key, value in rewards.items()))
    timeout = exception_type == "AgentTimeoutError"
    allowed_outcome = not error or timeout
    status = "finished" if valid_rewards and allowed_outcome else "error"
    if timeout:
        termination_reason = "agent_timeout"
    elif termination_reason not in {"finish", "agent_timeout", "aborted", "agent_stopped"}:
        termination_reason = "error" if error else "finish"
    if valid_rewards and exception_type != "VerifierTimeoutError":
        verification_status = "finished"
    elif verification_started or raw.get("verifier") or exception_type == "VerifierTimeoutError":
        verification_status = "error"
    else:
        verification_status = "not_run"
    if status == "finished" and timeout:
        note = "Agent time budget expired; verification completed. Reward is the verifier's result, not an automatic zero."
    elif status == "finished":
        note = "Verification completed. Finished does not imply the task was solved."
    else:
        note = "Evaluation did not complete cleanly; inspect verification_status, exception_type and private host logs."
    return {
        "session_id": session_id, "game_id": task_id, "status": status,
        "termination_reason": termination_reason, "verification_status": verification_status,
        "rewards": rewards if valid_rewards else None,
        "exception_type": exception_type, "note": note,
    }


class HarborSession:
    def __init__(self, *, session_id: str, task_id: str, task_path: Path,
                 output_dir: Path, settings: TerminalSettings, workspace_dir: Path,
                 environment_type: str = "docker", environment_kwargs: dict[str, Any] | None = None,
                 benchmark: str | None = None, task_name: str | None = None,
                 build_timeout_seconds: float = 3600):
        self.session_id = session_id
        self.task_id = task_id
        self.identity = {"benchmark": benchmark, "task_name": task_name}
        self.task_path = task_path
        self.output_dir = output_dir
        self.settings = settings
        self.environment_type = environment_type
        self.environment_kwargs = environment_kwargs or {}
        self.build_timeout_seconds = build_timeout_seconds
        self.state = BridgeState(settings, workspace_dir, output_dir / session_id / "uploads")
        self.runner: asyncio.Task | None = None
        self.final: dict[str, Any] | None = None
        self.closed = False
        self.submitted = False
        self.close_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.audit_dir = output_dir / session_id
        self.audit_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.state.lifecycle.on_change = self._publish_lifecycle
        self._publish_lifecycle()

    def _publish_lifecycle(self) -> None:
        # Private host record, atomically replaced. The supervisor can inspect it
        # even while this worker is blocked; no extra model-facing action exists.
        atomic_json(self.audit_dir / "lifecycle.json", {
            "session_id": self.session_id, **self.state.lifecycle.record(), "updated_at": now(),
        })

    async def _agent_ended(self, event: Any) -> None:
        # Also covers failure before the bridge's run() body was entered.
        self.state.lifecycle.end_solving()

    async def _verification_started(self, event: Any) -> None:
        error = getattr(event.result, "exception_info", None)
        self.state.lifecycle.begin_verification(getattr(error, "exception_type", None))

    def _log(self, event: str, **data: Any) -> None:
        with (self.audit_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **data}, ensure_ascii=True, allow_nan=False) + "\n")

    def _save_final(self) -> None:
        if self.final is not None:
            self.final.update(self.identity)
            self.final.setdefault("game_id", self.task_id)
            self.final.pop("task_id", None)
            self.final.setdefault("termination_reason", "aborted" if self.final.get("status") == "aborted" else "error")
            self.final.setdefault("verification_status", "error" if self.state.lifecycle.verification_started else "not_run")
            # Timing remains private for audit and possible future observation support.
            self.final["time_budget"] = self.state.lifecycle.time_budget()
            self.final.setdefault("finished_at", now())
            self.final.setdefault("cleanup_required", False)
            atomic_json(self.audit_dir / "result.json", self.final)

    async def _run(self) -> None:
        try:
            # Audit exact local task content without adding it to agent observations.
            hashes = {}
            for file in sorted(self.task_path.rglob("*")):
                if file.is_file() and not file.is_symlink():
                    digest = hashlib.sha256()
                    with file.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    hashes[str(file.relative_to(self.task_path))] = digest.hexdigest()
            atomic_json(self.audit_dir / "task-files.json", {"sha256": hashes})
            config = tomllib.loads((self.task_path / "task.toml").read_text(encoding="utf-8"))
            if config.get("steps"):
                raise RuntimeError("This first adapter supports single-stage tasks, not multi-step Harbor tasks")
            if config.get("environment", {}).get("os", "linux") not in {"linux", "Linux"}:
                raise RuntimeError("This adapter requires a Linux task environment")
            from harbor.models.trial.config import TrialConfig
            from harbor.models.task.config import TaskConfig as NativeTaskConfig
            from harbor.trial.trial import Trial

            # No oracle, tests or task-solving agent is selected here.
            cfg = TrialConfig(
                task={"path": self.task_path},
                trial_name=self.session_id,
                trials_dir=self.audit_dir / "trial",
                # Give infrastructure setup at least an hour. Solving and verifier
                # budgets retain the task's native values and multipliers.
                environment_build_timeout_multiplier=max(
                    1.0, self.build_timeout_seconds / NativeTaskConfig.model_validate(config).environment.build_timeout_sec,
                ),
                agent={
                    "import_path": f"{__package__}.harbor_agent:RemoteTerminalAgent",
                    "kwargs": {"bridge_key": self.session_id},
                },
                environment={"type": self.environment_type, "delete": True,
                             "kwargs": self.environment_kwargs},
                verifier={"disable": False},
            )
            BRIDGES[self.session_id] = self.state
            trial = await Trial.create(cfg)
            # The pinned Harbor release computes caps/multipliers here; do not
            # reimplement its defaults or start a second competing deadline.
            self.state.lifecycle.configure_timeout(trial._agent_timeout_sec)
            from harbor.trial.hooks import TrialEvent
            trial.add_hook(TrialEvent.AGENT_END, self._agent_ended)
            trial.add_hook(TrialEvent.VERIFICATION_START, self._verification_started)
            native_result = await trial.run()
            self.final = summarize_result(native_result.model_dump(mode="json"),
                                          self.session_id, self.task_id,
                                          termination_reason=self.state.lifecycle.termination_reason,
                                          verification_started=self.state.lifecycle.verification_started)
        except asyncio.CancelledError:
            # Trial.run's finally path performs container cleanup before this point.
            self.final = {"session_id": self.session_id, "task_id": self.task_id,
                          "status": "aborted", "rewards": None,
                          "note": "Session closed without completing finish."}
        except Exception as exc:
            LOGGER.exception("Trial %s failed", self.session_id)
            self.final = {"session_id": self.session_id, "task_id": self.task_id,
                          "status": "error", "rewards": None,
                          "exception_type": type(exc).__name__,
                          "note": "See the host runtime log; no successful evaluation is claimed."}
        finally:
            BRIDGES.pop(self.session_id, None)
            if self.final is not None:
                self.state.lifecycle.finish(self.final.get("termination_reason") or
                                            ("aborted" if self.final.get("status") == "aborted" else "error"))
            self.state.ready.set()
            self._save_final()

    async def start(self) -> dict[str, Any]:
        async with self.lock:
            if self.closed:
                raise RuntimeFault("start_failed", "Session was closed")
            if self.runner is None:
                self.runner = asyncio.create_task(self._run(), name=f"harbor-{self.session_id}")
        await self.state.ready.wait()
        if self.state.instruction is None or self.state.initial_observation is None:
            if self.runner is not None and not self.runner.cancelled():
                await asyncio.shield(self.runner)
            raise RuntimeFault("start_failed", "Task preparation failed; see the host runtime logs")
        if self.runner is not None and self.runner.done():
            raise RuntimeFault("start_failed", "Trial ended before startup was delivered")
        result = {
            "session_id": self.session_id,
            "environment_description": environment_description(
                self.state.instruction, self.settings.max_file_bytes,
                time_budget=self.state.lifecycle.time_budget()),
            "observation": self.state.initial_observation,
        }
        self._log("started", task_id=self.task_id, **result)
        return result

    async def move(self, action: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            if self.closed:
                raise RuntimeFault("terminal", "Session has been closed")
            if self.runner is None or self.state.instruction is None:
                raise RuntimeFault("environment_error", "Session has not finished preparing")
            if self.runner.done():
                # Surface asynchronous native timeout/failure as a terminal observation.
                return {"observation": terminal_observation()}
            if self.state.lifecycle.solving_ended:
                # Native timeout ends the game immediately; grading is host-owned.
                return {"observation": terminal_observation()}
            self._log("action", action=action)
            if action["id"] == "finish":
                self.submitted = True
            reply = asyncio.get_running_loop().create_future()
            await self.state.queue.put((action, reply))
            # Race the reply against trial completion, including Harbor's own timeout.
            await asyncio.wait((reply, self.runner), return_when=asyncio.FIRST_COMPLETED)
            if reply.cancelled() or not reply.done():
                if not reply.done():
                    reply.cancel()
                result = terminal_observation()
            else:
                try:
                    result = reply.result()
                except RuntimeFault as exc:
                    self._log("rejected", code=exc.code, message=str(exc))
                    raise
                if action["id"] == "finish":
                    # The bridge accepted submission. Grading continues in _run.
                    result = terminal_observation()
            self._log("observation", observation=result)
            return {"observation": result}

    async def _close_impl(self) -> None:
        runner = self.runner
        if (runner is not None and not runner.done() and not self.submitted
                and not self.state.lifecycle.preserve_finalization):
            if self.state.instruction is not None:
                # A prepared task must be graded even if the external agent exits
                # without finish. End the bridge normally so Harbor runs its verifier.
                self.submitted = True
                reply = asyncio.get_running_loop().create_future()
                reply.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                await self.state.queue.put(({"id": "finish"}, reply))
            else:
                # No usable task was delivered; only preparation can be cancelled.
                self.state.lifecycle.request_abort()
                runner.cancel()
        try:
            if runner is not None:
                await asyncio.shield(runner)
        except asyncio.CancelledError:
            # Cancellation before _run's first instruction bypasses its finally.
            if runner is None or not runner.cancelled():
                raise
        finally:
            if self.final is None:
                self.final = {"session_id": self.session_id, "task_id": self.task_id,
                              "status": "aborted", "rewards": None,
                              "note": "Session closed before activation."}
                self.state.lifecycle.finish("aborted")
                self._save_final()
            self.state.ready.set()

    async def close(self, *, reason: str = "agent_stopped") -> None:
        # Do not wait for self.lock: start/move can be waiting for the runner.
        # All close callers await the same cleanup, including runtime shutdown.
        if self.close_task is None:
            self.state.finish_reason = reason
            self.closed = True
            self.close_task = asyncio.create_task(self._close_impl())
        await asyncio.shield(self.close_task)
