"""Own one Docker bridge for the lifetime of a Minecraft host runtime."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import subprocess
import threading
from uuid import uuid4

from .recording import atomic_json


LOGGER = logging.getLogger(__name__)
OWNER_LABEL = "io.envtraverse.minecraft_realtime.runtime"


class RuntimeNetwork:
    def __init__(self, directory: Path):
        self.directory = directory
        self.owner_id = uuid4().hex
        self.name = "minecraft-realtime-runtime-" + self.owner_id
        self.network_id = None
        self.state = "not_created"
        self.cleanup_error = None
        self.creation_error = None
        self._attempted = False
        self._lock = threading.RLock()

    def metadata(self) -> dict:
        with self._lock:
            return {"name": self.name, "id": self.network_id, "owner_id": self.owner_id,
                    "state": self.state, "creation_error": self.creation_error,
                    "cleanup_error": self.cleanup_error,
                    "cleanup_required": self._attempted and self.state != "closed"}

    def _save(self) -> None:
        atomic_json(self.directory / "network.json", self.metadata())

    def _save_failure(self) -> None:
        # A full disk must not replace the original error or prevent cleanup of
        # the network ID already kept in memory.
        try:
            self._save()
        except OSError:
            LOGGER.exception("Cannot save Minecraft network cleanup metadata")

    @staticmethod
    def _run(*arguments: str):
        return subprocess.run(["docker", "network", *arguments], check=False,
                              capture_output=True, text=True, timeout=30)

    @staticmethod
    def _error(operation: str, result) -> RuntimeError:
        detail = (result.stderr or result.stdout or "no Docker diagnostic").strip()[:2000]
        return RuntimeError(f"Minecraft network {operation} failed (exit {result.returncode}): {detail}")

    @staticmethod
    def _missing(result, reference: str) -> bool:
        text = (result.stderr or "").lower()
        return ("no such network" in text
                or (f"network {reference.lower()}" in text and "not found" in text))

    def create(self) -> str:
        with self._lock:
            if self.state == "ready":
                return self.name
            if self.state == "closed":
                raise RuntimeError("Minecraft runtime network has already been closed")
            if self._attempted:
                raise RuntimeError("Minecraft network creation already failed; cleanup is required")
            # Persist ownership before Docker runs. If the CLI times out after
            # creating the network, close() can recover its ID by this exact name
            # and ownership label rather than leaving an untracked network.
            self._attempted = True
            self.state = "creating"
            try:
                self._save()
                result = self._run("create", "--driver", "bridge", "--label",
                                   f"{OWNER_LABEL}={self.owner_id}", self.name)
                if result.returncode:
                    raise self._error("creation", result)
                identifier = result.stdout.strip()
                if not re.fullmatch(r"[0-9a-f]{64}", identifier):
                    raise RuntimeError("Docker returned an invalid Minecraft network ID")
                self.network_id = identifier
                self.state = "ready"
                self._save()
                return self.name
            except BaseException as error:
                self.creation_error = str(error) or type(error).__name__
                self.state = "creation_failed"
                self._save_failure()
                raise

    def _owned_network(self) -> dict | None:
        reference = self.network_id or self.name
        result = self._run("inspect", reference)
        if result.returncode:
            if self._missing(result, reference):
                return None
            raise self._error("inspection", result)
        try:
            records = json.loads(result.stdout)
            if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
                raise ValueError("expected one network")
            network = records[0]
            identifier = network.get("Id", "")
            if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
                raise ValueError("invalid network ID")
        except (TypeError, ValueError) as error:
            raise RuntimeError("Docker returned invalid Minecraft network inspection data") from error
        if (network.get("Name") != self.name
                or (network.get("Labels") or {}).get(OWNER_LABEL) != self.owner_id
                or (self.network_id is not None and identifier != self.network_id)):
            raise RuntimeError(f"Refusing to remove network {reference}: runtime ownership does not match")
        self.network_id = identifier
        return network

    def close(self) -> None:
        with self._lock:
            if self.state == "closed":
                return
            try:
                if self._attempted and self._owned_network() is not None:
                    # Docker refuses removal while endpoints are attached. Do
                    # not disconnect them: a failed game cleanup must stay visible.
                    result = self._run("rm", self.network_id)
                    if result.returncode and not self._missing(result, self.network_id):
                        raise self._error("removal", result)
                self.cleanup_error = None
                self.state = "closed"
                self._save()
            except BaseException as error:
                self.cleanup_error = str(error) or type(error).__name__
                self.state = "cleanup_failed"
                self._save_failure()
                raise
