"""Per-game Docker tmpfs volumes and a small, bounded deferred-removal queue."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from typing import Callable, Sequence


RESOURCE_ENVIRONMENT_VARIABLE = "ENVTRAVERSE_RESOURCES"
# Existing framework mounts and authentication directories must remain visible.
RESERVED_MOUNTS = ("/data", "/observations", "/client", "/home/user/run",
                   "/home/user/.codex", "/home/user/.local/share/opencode",
                   "/proc", "/sys", "/dev", "/app")
CLEANUP_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class MemDiskMount:
    mount: str
    size_bytes: int


def _size_bytes(value: object) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(?:i?b)?", str(value).strip(), re.I)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or match is None:
        raise RuntimeError("mem_disk_mounts size must be positive bytes or a size such as 10mb")
    amount = Decimal(match[1]) * 1024 ** ("kmgt".index(match[2].lower()) + 1 if match[2] else 0)
    if not 1 <= amount <= 2**63 - 1:
        raise RuntimeError("mem_disk_mounts size must be between 1 and 2**63 - 1 bytes")
    return int(amount)


def validate_mount_targets(mounts: Sequence[MemDiskMount], existing: Sequence[str]) -> None:
    targets = [PurePosixPath(target) for target in existing]
    for mount in mounts:
        target = PurePosixPath(mount.mount)
        for other in targets:
            if target == other or target in other.parents or other in target.parents:
                raise RuntimeError(f"mem_disk_mounts target {target} overlaps another mount: {other}")
        targets.append(target)


def parse_mem_disk_mounts(config: dict, path: Path, *, existing: Sequence[str] = ()) -> tuple[MemDiskMount, ...]:
    entries = config.get("mem_disk_mounts", [])
    if not isinstance(entries, list):
        raise RuntimeError(f"mem_disk_mounts in {path} must be a list of mount/size mappings")
    mounts = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"mount", "size"}:
            raise RuntimeError("Each mem_disk_mounts entry must contain exactly mount and size")
        target = entry["mount"]
        if (not isinstance(target, str) or not target.startswith("/") or any(character in target for character in ",:")
                or any(ord(character) < 32 or ord(character) == 127 for character in target)):
            raise RuntimeError("mem_disk_mounts mount must be an absolute directory without commas, colons, or control characters")
        target_path = PurePosixPath(target)
        if ".." in target_path.parts or str(target_path).strip("/") == "":
            raise RuntimeError("mem_disk_mounts mount must be an absolute directory other than /, without '..'")
        mounts.append(MemDiskMount("/" + str(target_path).lstrip("/"), _size_bytes(entry["size"])))
    validate_mount_targets(mounts, (*RESERVED_MOUNTS, *existing))
    return tuple(mounts)


def resource_context(mounts: Sequence[MemDiskMount], session_id: str) -> dict:
    if not mounts:
        return {}
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise ValueError("RAM volume session_id must be a hexadecimal UUID")
    return {"session_id": session_id, "mem_disk_mounts": [
        {"mount": mount.mount, "size_bytes": mount.size_bytes,
         "volume": f"envtraverse-mem-{session_id}-{index}"}
        for index, mount in enumerate(mounts)
    ]}


def volume_mount_args(resources: dict | None) -> list[str]:
    arguments = []
    for mount in (resources or {}).get("mem_disk_mounts", []):
        arguments.extend(["--mount", f"type=volume,src={mount['volume']},dst={mount['mount']},volume-nocopy"])
    return arguments


class MemVolumeManager:
    def __init__(self, mounts: Sequence[MemDiskMount], log: Callable[..., None]):
        self.mounts, self.log = mounts, log
        # Includes live volumes until their game ends. Failed removals stay here
        # for the final pass; this is deliberately not a separate daemon/lease.
        self.pending: dict[str, str] = {}
        self.games: dict[str, list[str]] = {}

    def create_game(self, session_id: str, directory: Path) -> dict:
        resources = resource_context(self.mounts, session_id)
        if not resources:
            return {}
        # Names remain available for manual cleanup after an interrupted run.
        (directory / "resources.json").write_text(json.dumps(resources, indent=2) + "\n", encoding="utf-8")
        names = self.games.setdefault(session_id, [])
        for mount in resources["mem_disk_mounts"]:
            name = mount["volume"]
            names.append(name)
            self.pending[name] = "volume creation has not completed"
            self.log("creating RAM volume %s at %s (%s bytes)", name, mount["mount"], mount["size_bytes"])
            result = subprocess.run([
                "docker", "volume", "create", "--driver", "local", "--opt", "type=tmpfs",
                "--opt", "device=tmpfs", "--opt",
                f"o=size={mount['size_bytes']},uid={os.getuid()},gid={os.getgid()},mode=1777",
                "--label", f"envtraverse.session={session_id}", name,
            ], capture_output=True, text=True, timeout=30, check=False)
            if result.returncode:
                raise RuntimeError(f"Could not create RAM volume {name}: {result.stderr.strip()}")
            self.pending[name] = "game still owns the volume"
        return resources

    def _remove(self, name: str, *, timeout: float = 5.0) -> bool:
        try:
            result = subprocess.run(["docker", "volume", "rm", name],
                                    capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.pending[name] = str(error)
            return False
        if result.returncode == 0 or "no such volume" in result.stderr.lower():
            self.pending.pop(name, None)
            return True
        self.pending[name] = result.stderr.strip() or result.stdout.strip() or f"Docker exited {result.returncode}"
        return False

    def release_game(self, session_id: str) -> None:
        for name in self.games.pop(session_id, []):
            if name in self.pending and not self._remove(name):
                self.log("RAM volume %s pending cleanup: %s", name, self.pending[name])

    def close(self) -> None:
        if not self.pending:
            return
        self.log("%s unremoved RAM volumes; removing them (up to %s seconds)",
                 len(self.pending), CLEANUP_TIMEOUT_SECONDS)
        deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
        while self.pending:
            for name in list(self.pending):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._remove(name, timeout=min(5.0, remaining))
            if not self.pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
        for name, error in self.pending.items():
            self.log("RAM volume cleanup failed: %s: %s", name, error)
        raise RuntimeError("RAM volume cleanup timed out; remove remaining volumes after their containers stop: "
                           + ", ".join(self.pending))
