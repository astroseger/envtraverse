"""Copy this session's shared workspace into the sandbox using Harbor.

Only the worker reads host files. Source traversal uses directory descriptors and
O_NOFOLLOW, including at the workspace root; links cannot redirect a copy into
another session or the host's private files. A local snapshot also keeps
Harbor from reopening agent-controlled source paths after validation.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import tempfile
import threading
from typing import Any

from .actions import validate_action
from .compat import ENVIRONMENT_OPERATION_TIMEOUT_SECONDS
from .protocol import RuntimeFault, observation

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    directory: bool
    size: int
    files: int


def snapshot_workspace(workspace: Path, relative: str, target: Path,
                       cancelled: threading.Event) -> Snapshot:
    """Validate and copy regular files/directories without following source links."""
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or ".." in parts:
        raise RuntimeFault("invalid_action", "Source must stay inside your workspace")
    total = 0
    files = 0

    def check_cancelled() -> None:
        if cancelled.is_set():
            raise InterruptedError("Workspace copy cancelled")

    def copy_entry(fd: int, destination: Path) -> bool:
        nonlocal total, files
        check_cancelled()
        before = os.fstat(fd)
        directory = stat.S_ISDIR(before.st_mode)
        if directory:
            destination.mkdir(mode=0o700)
            for name in sorted(os.listdir(fd)):
                check_cancelled()
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    copy_entry(child, destination / name)
                finally:
                    os.close(child)
        elif stat.S_ISREG(before.st_mode):
            files += 1
            with destination.open("xb") as output:
                while True:
                    check_cancelled()
                    block = os.read(fd, 65536)
                    if not block:
                        break
                    total += len(block)
                    output.write(block)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeFault("invalid_action", "Source changed while copying; wait for its writer to finish")
        else:
            raise RuntimeFault("invalid_action", "Source must contain only regular files and directories")
        # Preserve ordinary permissions, including executable bits, without copying
        # host ownership or special permission bits into a task.
        destination.chmod(stat.S_IMODE(before.st_mode) & 0o777)
        return directory

    try:
        with ExitStack() as stack:
            def opened(name: str, flags: int, parent: int | None = None) -> int:
                fd = os.open(name, flags, dir_fd=parent)
                stack.callback(os.close, fd)
                return fd

            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = opened("/", directory_flags)
            for name in workspace.absolute().parts[1:]:
                check_cancelled()
                fd = opened(name, directory_flags, fd)
            for index, name in enumerate(parts):
                flags = directory_flags if index < len(parts) - 1 else (
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                fd = opened(name, flags, fd)
            is_directory = copy_entry(fd, target)
        return Snapshot(is_directory, total, files)
    except OSError as exc:
        if cancelled.is_set():
            raise
        raise RuntimeFault("invalid_action", "Source is missing, unreadable, or contains a symbolic link; "
                           "use regular files and directories inside your workspace") from exc


class WorkspaceUploads:
    def __init__(self, environment: Any, workspace_dir: Path, host_staging_dir: Path):
        self.environment = environment
        self.workspace_dir = workspace_dir
        self.host_staging_dir = host_staging_dir
        self.user = environment.default_user
        self.owner: str | None = None

    async def _identity(self) -> str:
        if self.owner is None:
            # None means the container's configured USER, not necessarily root.
            result = await self.environment.exec("id -u && id -g", user=self.user, timeout_sec=30)
            values = (result.stdout or "").split()
            if result.return_code != 0 or len(values) != 2 or not all(v.isdecimal() for v in values):
                raise RuntimeError("Could not determine the sandbox user")
            self.owner = ":".join(values)
        return self.owner

    async def send(self, path_local: str, path_sandbox: str) -> dict[str, Any]:
        validate_action({"id": "send_path", "path_local": path_local, "path_sandbox": path_sandbox})
        self.host_staging_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="workspace-upload-", dir=self.host_staging_dir) as temporary:
            source = Path(temporary) / "payload"
            cancelled = threading.Event()
            copying = asyncio.create_task(asyncio.to_thread(
                snapshot_workspace, self.workspace_dir, path_local, source, cancelled))
            try:
                snapshot = await asyncio.shield(copying)
            except asyncio.CancelledError:
                cancelled.set()
                # The thread must finish before TemporaryDirectory removes its output.
                try:
                    await copying
                except Exception:
                    pass
                raise
            if not snapshot.directory and path_sandbox.endswith("/"):
                raise RuntimeFault("invalid_action", "For a file, path_sandbox must specify a destination filename")

            owner = await self._identity()
            result = await self.environment.exec("mktemp -d /tmp/.workspace-upload-XXXXXXXXXX",
                                                 user="root", timeout_sec=30)
            staging = (result.stdout or "").strip()
            if result.return_code != 0 or not staging.startswith("/tmp/.workspace-upload-") or "/" in staging[5:]:
                raise RuntimeError("Could not prepare the sandbox upload directory")
            try:
                staged = staging + "/payload"
                if snapshot.directory:
                    await self.environment.upload_dir(source, staged)
                else:
                    await self.environment.upload_file(source, staged)
                result = await self.environment.exec(
                    shlex.join(["chown", "-hR", owner, "--", staging]), user="root", timeout_sec=30)
                if result.return_code != 0:
                    raise RuntimeError("Could not assign uploaded files to the sandbox user")
                # -T gives both kinds an exact destination (no extra basename).
                # Copy as the task user: host/Docker privileges must not make a
                # protected destination writable. Only staging is chowned as root.
                result = await self.environment.exec(
                    shlex.join(["cp", "-R", "-p", "-T", "--", staged, path_sandbox]),
                    user=self.user, timeout_sec=int(ENVIRONMENT_OPERATION_TIMEOUT_SECONDS))
                if result.return_code != 0:
                    LOGGER.warning("Sandbox upload copy failed: %s", result.stdout or result.stderr)
                    raise RuntimeFault("environment_error", "Copy failed; check destination permissions and "
                                       "that its parent directory exists. Some files may have been copied.")
            finally:
                try:
                    result = await self.environment.exec(shlex.join(["rm", "-rf", "--", staging]),
                                                         user="root", timeout_sec=10)
                    if result.return_code != 0:
                        LOGGER.warning("Could not clean sandbox upload staging directory")
                except Exception:
                    LOGGER.exception("Could not clean sandbox upload staging directory")

        return observation({"transfer.json": json.dumps({
            "path_local": path_local, "path_sandbox": path_sandbox,
            "kind": "directory" if snapshot.directory else "file",
            "bytes": snapshot.size, "files": snapshot.files,
        }, indent=2)})
