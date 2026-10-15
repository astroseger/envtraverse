"""Harbor custom agent whose actions come from the external environment manager.

No model, prompt optimization or solving logic. Actions execute inside run(),
so Harbor's user/network/environment scopes remain in effect.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from harbor.agents.base import BaseAgent
from . import __version__
from .actions import validate_action
from .bridge import BRIDGES
from .protocol import RuntimeFault
from .terminal import TmuxTerminals
from .uploads import WorkspaceUploads

LOGGER = logging.getLogger(__name__)


class RemoteTerminalAgent(BaseAgent):
    def __init__(self, *, bridge_key: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.state = BRIDGES[bridge_key]
        self.state.logs_dir = self.logs_dir
        self.terminals: TmuxTerminals | None = None
        self.uploads: WorkspaceUploads | None = None

    @staticmethod
    def name() -> str:
        return "unified-terminal-bridge"

    def version(self) -> str:
        return __version__

    async def setup(self, environment: Any) -> None:
        self.terminals = TmuxTerminals(environment, self.state.settings)
        self.uploads = WorkspaceUploads(environment, self.state.workspace_dir,
                                        self.state.upload_dir)
        await self.terminals.setup()
        self.state.initial_observation = await self.terminals.new()

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        if self.terminals is None or self.uploads is None:
            raise RuntimeError("Agent setup was not called")
        self.state.lifecycle.begin_solving(asyncio.current_task())
        self.state.instruction = instruction
        self.state.ready.set()
        context.metadata = {"external_agent": True, "bridge_version": __version__}
        current_reply: asyncio.Future | None = None
        termination_reason = "agent_stopped"
        try:
            while True:
                action, current_reply = await self.state.queue.get()
                try:
                    validate_action(action)
                    if action["id"] == "finish":
                        termination_reason = self.state.finish_reason
                        current_reply.set_result({"submitted": True})
                        current_reply = None
                        # Harbor performs artifact collection and verification after
                        # returning. Do not kill terminals/servers before grading.
                        return
                    if action["id"] == "send_path":
                        result = await self.uploads.send(action["path_local"], action["path_sandbox"])
                    else:
                        result = await self.terminals.perform(action)
                except RuntimeFault as exc:
                    if not current_reply.done():
                        current_reply.set_exception(exc)
                except Exception:
                    LOGGER.exception("Sandbox terminal operation failed")
                    if not current_reply.done():
                        current_reply.set_exception(RuntimeFault(
                            "environment_error", "Sandbox operation failed; see runtime logs. "
                            "Input may have been delivered; do not blindly repeat it."))
                else:
                    if not current_reply.done():
                        current_reply.set_result(result)
                current_reply = None
        finally:
            # Publish the phase BEFORE releasing replies or returning to Harbor.
            # Timeout cancellation is not a user-requested abort. Harbor will
            # record its exact cause and may then verify the existing solution.
            self.state.lifecycle.end_solving(termination_reason)
            # A Harbor timeout/cancellation must not leave an RPC waiting forever.
            if current_reply is not None and not current_reply.done():
                current_reply.cancel()
            while not self.state.queue.empty():
                _, reply = self.state.queue.get_nowait()
                if not reply.done():
                    reply.cancel()
