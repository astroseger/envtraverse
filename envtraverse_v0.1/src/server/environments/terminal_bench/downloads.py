"""Export as the task user, then install regular files inside this workspace.

Docker downloads bypass task-user permissions. Only a regular archive created by
the task user is downloaded; its root-owned parent prevents an unprivileged task
from replacing it with a link. Archive entries are validated into private host
staging before touching the workspace. Destination traversal never follows links.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
import errno
import json
import logging
import os
from pathlib import Path, PurePosixPath
import posixpath
import shlex
import stat
import tarfile
import tempfile
import threading
from typing import Any
from uuid import uuid4

from .actions import validate_action
from .compat import ENVIRONMENT_OPERATION_TIMEOUT_SECONDS
from .protocol import RuntimeFault, observation
from .uploads import Snapshot

LOGGER = logging.getLogger(__name__)
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class TransferResult(Snapshot):
    """Committed workspace contents, independent of the staged source totals."""
    files_not_copied: int = 0
    error: str | None = None

    @property
    def status(self) -> str:
        return "partial" if self.error is not None else "success"


@dataclass
class InstallProgress:
    files: int = 0
    size: int = 0
    changed: bool = False
    error: str | None = None


def copy_failure(exc: OSError | RuntimeFault, path: str, operation: str) -> str:
    """Describe the workspace-relative target without exposing host/staging paths."""
    if isinstance(exc, RuntimeFault):
        reason = str(exc)
    elif isinstance(exc, PermissionError):
        reason = "permission denied"
    else:
        reason = {
            errno.ENOENT: "source or destination parent no longer exists",
            errno.ENOTDIR: "destination ancestor is not a directory or is a symbolic link",
            errno.ELOOP: "destination contains a symbolic link",
            errno.EISDIR: "destination is a directory",
            errno.ENOSPC: "not enough disk space",
            errno.EDQUOT: "disk quota exceeded",
            errno.EROFS: "destination filesystem is read-only",
            errno.ENAMETOOLONG: "path is too long",
            errno.EIO: "filesystem I/O error",
        }.get(exc.errno, "filesystem operation failed")
    return f"Could not {operation} {path}: {reason}."


# All variable data is passed as quoted positional arguments. GNU tar writes
# hard-linked regular files as independent contents, never as host hard links.
EXPORT_SCRIPT = '''set -eu
source=$1; archive=$2; excluded=$3
cursor=$source
while :; do
  [ ! -L "$cursor" ] || exit 44
  [ "$cursor" != / ] || break
  cursor=${cursor%/*}
  [ -n "$cursor" ] || cursor=/
done
if [ -d "$source" ]; then
  cd -- "$source"
  invalid=$(find . -path "$excluded" -prune -o ! -type f ! -type d -print -quit)
  [ -z "$invalid" ] || exit 44
  tar --hard-dereference --no-wildcards --anchored --exclude="$excluded" -cf "$archive" -- .
  printf directory
elif [ -f "$source" ] && [ -r "$source" ]; then
  parent=${source%/*}; name=${source##*/}
  [ -n "$parent" ] || parent=/
  tar --hard-dereference -cf "$archive" -C "$parent" -- "$name"
  printf file
else
  exit 44
fi
'''


def check_cancelled(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise InterruptedError("Workspace download cancelled")


def unpack_export(archive: Path, target: Path, *, directory: bool, root_name: str,
                  cancelled: threading.Event) -> tuple[Snapshot, dict[tuple[str, ...], int]]:
    """Read only regular files/directories; never extract archive links or owners."""
    modes: dict[tuple[str, ...], int] = {}
    directories: set[tuple[str, ...]] = set()
    size = files = 0
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeFault("invalid_action", "Sandbox export is not a regular archive")
        with tarfile.open(fileobj=stream, mode="r|") as exported:
            for member in exported:
                check_cancelled(cancelled)
                path = PurePosixPath(member.name)
                if not member.name or path.is_absolute() or ".." in path.parts:
                    raise RuntimeFault("invalid_action", "Sandbox export contains an unsafe path")
                parts = path.parts
                if not directory:
                    if parts != (root_name,):
                        raise RuntimeFault("invalid_action", "Sandbox file export contains unexpected entries")
                    parts = ()
                if parts in modes or (parts and parts[:-1] not in directories):
                    raise RuntimeFault("invalid_action", "Sandbox export contains duplicate or unordered paths")
                if not (member.isdir() or member.isreg()) or (not parts and member.isdir() != directory):
                    raise RuntimeFault("invalid_action", "Source must contain only regular files and directories")
                destination = target.joinpath(*parts)
                if member.isdir():
                    destination.mkdir(mode=0o700)
                    directories.add(parts)
                else:
                    source = exported.extractfile(member)
                    assert source is not None
                    with source, destination.open("xb") as output:
                        while True:
                            check_cancelled(cancelled)
                            block = source.read(65536)
                            if not block:
                                break
                            output.write(block)
                            size += len(block)
                    files += 1
                # Keep staging accessible; apply these modes only at installation.
                modes[parts] = member.mode & 0o777
    if () not in modes:
        raise RuntimeFault("invalid_action", "Sandbox export is empty or missing its root")
    return Snapshot(directory, size, files), modes


def install_export(source: Path, workspace: Path, relative: str,
                   modes: dict[tuple[str, ...], int], cancelled: threading.Event) -> InstallProgress:
    """Merge through directory descriptors; replace files without following links."""
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or ".." in parts:
        raise RuntimeFault("invalid_action", "Destination must stay inside your workspace")
    if source.is_file() and (not parts or relative.rsplit("/", 1)[-1] in {"", "."}):
        raise RuntimeFault("invalid_action", "For a file, path_local must specify a destination filename")
    progress = InstallProgress()
    current_path = str(PurePosixPath(relative))
    operation = "open destination"

    def location(key: tuple[str, ...], verb: str) -> None:
        nonlocal current_path, operation
        current_path = str(PurePosixPath(relative).joinpath(*key))
        operation = verb

    def copy_file(path: Path, parent: int, name: str, mode: int) -> None:
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise RuntimeFault("environment_error", "destination is not a regular file")
            if not os.access(name, os.W_OK, dir_fd=parent, follow_symlinks=False, effective_ids=True):
                raise PermissionError("Destination file is not writable")
        temporary = ".get-path-" + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        copied_size = 0
        try:
            with os.fdopen(fd, "wb") as output, path.open("rb") as input_file:
                while True:
                    check_cancelled(cancelled)
                    block = input_file.read(65536)
                    if not block:
                        break
                    output.write(block)
                    copied_size += len(block)
                output.flush()
                os.fchmod(output.fileno(), mode)
            check_cancelled(cancelled)
            # A fresh inode also prevents overwriting another path through an
            # existing destination hard link. Replacement is atomic per file.
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            progress.files += 1
            progress.size += copied_size
            progress.changed = True
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass

    def copy_directory(path: Path, fd: int, key: tuple[str, ...], *, keep_mode: bool = False) -> None:
        location(key, "copy directory")
        for child in sorted(path.iterdir()):
            copy_entry(child, fd, child.name, (*key, child.name))
        check_cancelled(cancelled)
        if not keep_mode:
            location(key, "set directory permissions for")
            previous_mode = stat.S_IMODE(os.fstat(fd).st_mode)
            os.fchmod(fd, modes[key])
            progress.changed |= previous_mode != modes[key]

    def copy_entry(path: Path, parent: int, name: str, key: tuple[str, ...]) -> None:
        check_cancelled(cancelled)
        location(key, "copy")
        if path.is_dir():
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
                progress.changed = True
            except FileExistsError:
                pass
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            try:
                copy_directory(path, fd, key)
            finally:
                os.close(fd)
        else:
            copy_file(path, parent, name, modes[key])

    try:
        with ExitStack() as stack:
            def opened(name: str, parent: int | None = None) -> int:
                check_cancelled(cancelled)
                fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                stack.callback(os.close, fd)
                return fd

            fd = opened("/")
            for name in workspace.absolute().parts[1:]:
                fd = opened(name, fd)
            for name in parts[:-1]:
                fd = opened(name, fd)
            if parts:
                copy_entry(source, fd, parts[-1], ())
            else:
                copy_directory(source, fd, (), keep_mode=True)
    except (OSError, RuntimeFault) as exc:
        if cancelled.is_set():
            raise
        message = copy_failure(exc, current_path, operation)
        if not progress.changed:
            raise RuntimeFault("environment_error", message) from exc
        progress.error = message
    return progress


def receive_export(archive: Path, workspace: Path, path_local: str, *, directory: bool,
                   root_name: str, cancelled: threading.Event) -> TransferResult:
    try:
        source = archive.parent / "payload"
        snapshot, modes = unpack_export(archive, source, directory=directory,
                                        root_name=root_name, cancelled=cancelled)
    except (OSError, tarfile.TarError) as exc:
        if cancelled.is_set():
            raise
        raise RuntimeFault("invalid_action", "Could not read the sandbox export; keep the source unchanged") from exc
    check_cancelled(cancelled)
    progress = install_export(source, workspace, path_local, modes, cancelled)
    return TransferResult(snapshot.directory, progress.size, progress.files,
                          files_not_copied=snapshot.files - progress.files, error=progress.error)


class WorkspaceDownloads:
    def __init__(self, environment: Any, workspace_dir: Path, host_staging_dir: Path):
        self.environment = environment
        self.workspace_dir = workspace_dir
        self.host_staging_dir = host_staging_dir
        self.user = environment.default_user

    async def get(self, path_sandbox: str, path_local: str) -> dict[str, Any]:
        validate_action({"id": "get_path", "path_local": path_local, "path_sandbox": path_sandbox},
                        environment_version="v1.2")
        result = await self.environment.exec("id -u && id -g", user=self.user, timeout_sec=30)
        identity = (result.stdout or "").split()
        if result.return_code != 0 or len(identity) != 2 or not all(v.isdecimal() for v in identity):
            raise RuntimeError("Could not determine the sandbox user")
        owner = ":".join(identity)
        result = await self.environment.exec("mktemp -d /tmp/.workspace-download-XXXXXXXXXX",
                                             user="root", timeout_sec=30)
        staging = (result.stdout or "").strip()
        if result.return_code != 0 or not staging.startswith("/tmp/.workspace-download-") or "/" in staging[5:]:
            raise RuntimeError("Could not prepare the sandbox download directory")
        try:
            archive_sandbox = staging + "/export.tar"
            # The task can write the archive's contents, but cannot replace the
            # file or its parent. Only these temporary paths are privileged.
            prepare = '''set -eu
: > "$1/export.tar"
chown -- "$2" "$1/export.tar"
chmod 600 -- "$1/export.tar"
chmod 711 -- "$1"
'''
            result = await self.environment.exec(shlex.join(["bash", "-c", prepare, "--", staging, owner]),
                                                 user="root", timeout_sec=30)
            if result.return_code != 0:
                raise RuntimeError("Could not prepare the sandbox export file")
            # Exclude our own temporary directory when exporting e.g. /tmp.
            excluded = "./" + posixpath.relpath(staging, posixpath.normpath(path_sandbox))
            result = await self.environment.exec(
                shlex.join(["bash", "-c", EXPORT_SCRIPT, "--", path_sandbox, archive_sandbox, excluded]),
                user=self.user, timeout_sec=int(ENVIRONMENT_OPERATION_TIMEOUT_SECONDS))
            kind = (result.stdout or "").strip()
            if result.return_code != 0 or kind not in {"file", "directory"}:
                LOGGER.warning("Sandbox export failed: %s", result.stderr or result.stdout)
                raise RuntimeFault("invalid_action", "Cannot export source; use readable regular files and "
                                   "directories without symbolic links, and keep them unchanged. GNU tar is required.")

            self.host_staging_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(prefix="workspace-download-", dir=self.host_staging_dir)
            try:
                archive = Path(temporary.name) / "export.tar"
                await self.environment.download_file(archive_sandbox, archive)
                cancelled = threading.Event()
                copying = asyncio.create_task(asyncio.to_thread(
                    receive_export, archive, self.workspace_dir, path_local, directory=kind == "directory",
                    root_name=PurePosixPath(path_sandbox).name, cancelled=cancelled))
                try:
                    transfer = await asyncio.shield(copying)
                except asyncio.CancelledError:
                    cancelled.set()
                    # Never remove staging while its worker thread is using it.
                    try:
                        await copying
                    except Exception:
                        pass
                    raise
            finally:
                try:
                    temporary.cleanup()
                except OSError:
                    # Cleanup must not hide a completed transfer or replace
                    # the original copy error/cancellation.
                    LOGGER.exception("Could not clean host download staging directory")
        finally:
            try:
                result = await self.environment.exec(shlex.join(["rm", "-rf", "--", staging]),
                                                     user="root", timeout_sec=10)
                if result.return_code != 0:
                    LOGGER.warning("Could not clean sandbox download staging directory")
            except Exception:
                LOGGER.exception("Could not clean sandbox download staging directory")

        receipt = {
            "status": transfer.status,
            "path_local": path_local, "path_sandbox": path_sandbox,
            "kind": "directory" if transfer.directory else "file",
            "files_copied": transfer.files, "bytes_copied": transfer.size,
        }
        if transfer.status == "partial":
            receipt.update(files_not_copied=transfer.files_not_copied, error=transfer.error)
        return observation({"transfer.json": json.dumps(receipt, indent=2)})
