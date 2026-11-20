"""One continuously running game, independent of EnvTraverse action cadence."""
from __future__ import annotations

import logging
import hashlib
import json
from pathlib import Path
import threading
import time

from .actions import validate_action
from .catalog import CATALOG_SCHEMA_VERSION
from .description import environment_description
from .docker_backend import DockerBackend
from .evaluator import TaskEvaluator
from .protocol import RuntimeFault, observation
from .recording import FramePublisher, JsonlLog, atomic_json
from .resources import MEMORY_FRAME_PATH, WORKSPACE_FRAME_PATH, frame_volume, parse_resource_context

LOGGER = logging.getLogger(__name__)


class Session:
    def __init__(self, *, session_id: str, owner_token: str, task: dict, settings,
                 run_root: Path, directory: Path, accept_eula: bool = False,
                 backend_factory=DockerBackend, clock=time.monotonic, viewer_url: str | None = None,
                 resources: dict | None = None):
        self.session_id, self.owner_token = session_id, owner_token
        self.task, self.settings, self.directory = task, settings, directory
        self.clock, self.viewer_url = clock, viewer_url
        self.resources = parse_resource_context(resources, session_id=session_id)
        volume = frame_volume(self.resources)
        self.latest_frame_path = MEMORY_FRAME_PATH if volume is not None else WORKSPACE_FRAME_PATH
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.startup_done = threading.Event()
        self.closed_event = threading.Event()
        self.closing = self.closed = self.started = False
        self.close_requested = False
        self.cleanup_failed = False
        self.evaluator = None
        self.last_state = None
        self.result = None
        self.action_sequence = 0
        self.threads = []
        self.events = JsonlLog(directory / "events.jsonl")
        self.actions = JsonlLog(directory / "actions.jsonl")
        self.publisher = FramePublisher(run_root, session_id, directory, settings.record_fps,
                                        publish_to_workspace=volume is None)
        try:
            backend_options = {"frame_volume": volume} if volume is not None else {}
            self.backend = backend_factory(session_id, directory, settings, accept_eula=accept_eula, **backend_options)
        except BaseException:
            self.publisher.close()
            raise
        self.metadata = {"session_id": session_id, "task_id": task["id"], "task": task,
                         "catalog_schema_version": CATALOG_SCHEMA_VERSION,
                         "task_sha256": hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                         "settings": settings.to_dict(), "created_at": time.time(),
                         "viewer_url": viewer_url, "status": "starting",
                         "latest_frame_path": self.latest_frame_path, "resources": self.resources}
        atomic_json(directory / "session.json", self.metadata)

    def event(self, kind: str, **data):
        self.events.append({"type": kind, "time": time.time(), "session_id": self.session_id, "data": data})

    def _save_result(self, value: dict | None):
        if value is not None and self.result is None:
            self.result = {**value, "session_id": self.session_id, "finished_at": time.time()}
            state_time = None if self.last_state is None else self.last_state.get("time_ms", 0) / 1000
            atomic_json(self.directory / "verdict_state.json", {
                "recorded_at": time.time(), "state": self.last_state,
                "state_age_seconds": None if state_time is None else max(0, time.time() - state_time)})
            atomic_json(self.directory / "result.json", self.result)
            reason = value.get("reason")
            kind = ("timeout" if reason == "minecraft_task_timeout" else
                    "goal_achieved" if value.get("success") else "task_finished")
            self.event(kind, **value)
            # Timeout only latches the task result. It deliberately neither
            # pauses Minecraft nor releases keys, nor pushes a server update.

    def start(self):
        try:
            if self.close_requested or self.closing:
                raise RuntimeError("Session closed before startup")
            baseline, frame = self.backend.start()
            self.publisher.publish(*frame)
            with self.lock:
                if self.closing:
                    raise RuntimeError("Session closed during startup")
                self.evaluator = TaskEvaluator(self.task, baseline,
                                              self.settings.minecraft_task_timeout_seconds, clock=self.clock)
                self.last_state = baseline
                self.started = True
                self.metadata.update(status="running", ready_at=time.time(),
                                     minecraft_session_id=baseline.get("session_id"))
                atomic_json(self.directory / "session.json", self.metadata)
                atomic_json(self.directory / "initial_state.json", baseline)
                for event in getattr(self.backend, "startup_events", []):
                    self.events.append({"source": "minecraft", **event})
                self.event("task_started", task_id=self.task["id"],
                           minecraft_task_timeout_seconds=self.settings.minecraft_task_timeout_seconds)
            for name, target in (("frames", self._capture), ("evaluation", self._monitor)):
                thread = threading.Thread(target=target, name=f"mc-{name}-{self.session_id[:8]}", daemon=True)
                self.threads.append(thread)
                thread.start()
            return {"session_id": self.session_id,
                    "environment_description": environment_description(self.task, self.settings,
                                                                       latest_frame_path=self.latest_frame_path),
                    "observation": observation({"ready.json": '{"ready":true}\n'})}
        except BaseException:
            with self.lock:
                self._save_result({"task_id": self.task["id"], "status": "error", "success": False,
                                   "reason": "startup_failed", "elapsed_seconds": 0})
            raise
        finally:
            self.startup_done.set()

    def _capture(self):
        last_success = self.clock()
        while not self.stop_event.is_set():
            began = self.clock()
            try:
                frame = self.backend.frame()
                self.publisher.publish(*frame)
                last_success = self.clock()
            except Exception:
                if self.clock() - last_success >= 5:
                    with self.lock:
                        if not self.closing:
                            self._save_result(self.evaluator.invalidate("capture_unavailable"))
            self.stop_event.wait(max(0.001, 1 / self.settings.capture_fps - (self.clock() - began)))

    def _monitor(self):
        while not self.stop_event.is_set():
            self.poll()
            self.stop_event.wait(0.1)

    def poll(self):
        """Public for deterministic tests; production calls this independently."""
        with self.lock:
            if not self.started or self.closing:
                return
            # Check the deadline before consuming a later successful snapshot.
            self._save_result(self.evaluator.tick())
            try:
                state, events = self.backend.read_state()
                self.last_state = state
                for event in events:
                    self.events.append({"source": "minecraft", **event})
                fresh = (state.get("server_running") is True and not state.get("telemetry_error")
                         and abs(time.time() - state.get("time_ms", 0) / 1000) < 5)
                self._save_result(self.evaluator.observe(state, events=events, fresh=fresh))
                for event in self.backend.read_input_events():
                    self.actions.append({"source": "bridge", **event})
            except Exception:
                LOGGER.exception("Session telemetry read failed: %s", self.session_id)
                self._save_result(self.evaluator.invalidate("telemetry_unavailable"))

    def action(self, action: dict):
        # Validate the complete action before touching input.
        action = validate_action(action, width=self.settings.width, height=self.settings.height)
        with self.lock:
            if not self.started or self.closing:
                raise RuntimeFault("not_ready", "Minecraft session is not running")
            self.poll()
            if self.result is not None:
                try:
                    self.backend.release()
                    self.actions.append({"type": "release_all", "source": "runtime", "time": time.time(),
                                         "reason": "terminal_observed"})
                except Exception:
                    LOGGER.exception("Could not release terminal session controls: %s", self.session_id)
                return {"observation": observation({"action.json": json.dumps({
                    "accepted": False, "terminal": True, "status": self.result["status"],
                    "reason": self.result["reason"], "success": self.result["success"]}) + "\n"}, terminal=True)}
            self.action_sequence += 1
            action_id = self.action_sequence
            requested_at = time.time()
            try:
                acknowledgment = self.backend.action(action)
            except Exception as error:
                self.actions.append({"action_id": action_id, "action": action, "requested_at": requested_at,
                                     "accepted": False, "delivery": "unknown"})
                self._save_result(self.evaluator.invalidate("input_unavailable"))
                raise RuntimeFault("unavailable", "Minecraft input connection failed; do not retry blindly") from error
            self.actions.append({"action_id": action_id, "action": action, "requested_at": requested_at,
                                 "accepted_at": time.time(), "accepted": True, "bridge": acknowledgment})
            return {"observation": observation({"action.json": json.dumps({
                "accepted": True, "action_id": action_id}) + "\n"})}

    def close(self):
        with self.lock:
            if self.closing:
                return
            self.closing = True
            self.stop_event.set()
            cancelled = getattr(self.backend, "cancelled", None)
            if cancelled is not None:
                cancelled.set()
        # Startup may still be inside docker compose up; it must finish before
        # teardown so a late startup cannot recreate containers after down.
        self.startup_done.wait()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)
        with self.lock:
            if self.evaluator is not None:
                self._save_result(self.evaluator.tick())
                self._save_result(self.evaluator.cancel())
        cleanup_error = None
        try:
            self.backend.stop()
        except Exception as error:
            LOGGER.exception("Docker cleanup failed for %s", self.session_id)
            cleanup_error = type(error).__name__
        finally:
            self.publisher.close()
            with self.lock:
                self.closed = True
                self.cleanup_failed = cleanup_error is not None
                self.metadata.update(status="closed", closed_at=time.time(), cleanup_error=cleanup_error)
                atomic_json(self.directory / "session.json", self.metadata)
                self.event("session_closed", cleanup_error=cleanup_error)
            self.closed_event.set()
