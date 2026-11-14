"""Read-only-at-rest client storage; callers serialize writes with the client lock."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


READ_ONLY_DIRECTORY_MODE = 0o555
READ_ONLY_FILE_MODE = 0o444
CLIENT_CONFIG_FILENAME = "client_config.json"
DEFAULT_HTTP_READ_TIMEOUT_SECONDS = 65 * 60.0


def positive_timeout_seconds(value: object, name: str) -> float:
    message = f"{name} must be a finite, positive number of seconds"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        seconds = float(value)
    except OverflowError:
        raise ValueError(message) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(message)
    return seconds


@dataclass(frozen=True)
class ClientConfig:
    server_url: str
    observations_path: Path
    http_read_timeout_seconds: float = DEFAULT_HTTP_READ_TIMEOUT_SECONDS

    @classmethod
    def from_dict(cls, value: object) -> ClientConfig:
        required = {"server_url", "observations_path"}
        if (not isinstance(value, dict) or not required <= value.keys()
                or value.keys() - required - {"http_read_timeout_seconds"}):
            raise ValueError("expected server_url and observations_path, with optional http_read_timeout_seconds")
        server_url = value["server_url"]
        if not isinstance(server_url, str) or not server_url or any(char.isspace() for char in server_url):
            raise ValueError("server_url must be an absolute HTTP(S) URL without whitespace")
        try:
            parsed = urlsplit(server_url)
            valid_url = (
                parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and (parsed.port is None or parsed.port > 0)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("server_url must be an absolute HTTP(S) URL without credentials, query, or fragment")
        observations_path = value["observations_path"]
        if (
            not isinstance(observations_path, str) or "\0" in observations_path
            or not Path(observations_path).is_absolute()
        ):
            raise ValueError("observations_path must be an absolute filesystem path")
        http_read_timeout_seconds = positive_timeout_seconds(
            value.get("http_read_timeout_seconds", DEFAULT_HTTP_READ_TIMEOUT_SECONDS),
            "http_read_timeout_seconds",
        )
        # Do not resolve container paths against the launcher's host filesystem.
        return cls(server_url=server_url.rstrip("/"), observations_path=Path(observations_path),
                   http_read_timeout_seconds=http_read_timeout_seconds)

    def to_dict(self) -> dict[str, str | float]:
        return {"server_url": self.server_url, "observations_path": str(self.observations_path),
                "http_read_timeout_seconds": self.http_read_timeout_seconds}


def read_client_config(client_dir: Path) -> ClientConfig:
    path = client_dir / CLIENT_CONFIG_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"Client configuration is missing: {path}. Prepare it with the launcher first.") from None
    except OSError as exc:
        raise RuntimeError(f"Cannot read client configuration {path}: {exc.strerror}") from None
    except ValueError:
        raise RuntimeError(f"Invalid client configuration {path}: expected UTF-8 JSON") from None
    try:
        return ClientConfig.from_dict(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid client configuration {path}: {exc}") from None


def require_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Expected an existing storage directory: {path}")


@contextmanager
def writable_directory(path: Path):
    """Unlock only the immediate parent of a local write, never its contents."""
    require_directory(path)
    try:
        path.chmod(0o755)
        yield
    finally:
        path.chmod(READ_ONLY_DIRECTORY_MODE)


def write_readonly_file(path: Path, content: bytes) -> None:
    """Atomically replace a file without making the old file writable."""
    with writable_directory(path.parent):
        fd, temporary_name = tempfile.mkstemp(prefix=".client-write-", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                # chmod does not revoke our already-open writable descriptor.
                # Even the unpublished temporary file is read-only to new opens.
                os.fchmod(stream.fileno(), READ_ONLY_FILE_MODE)
                stream.write(content)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def ensure_readonly_directory(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        with writable_directory(path.parent):
            path.mkdir(mode=READ_ONLY_DIRECTORY_MODE)
    require_directory(path)
    path.chmod(READ_ONLY_DIRECTORY_MODE)


def seal_storage(client_dir: Path, observations_dir: Path) -> None:
    """Also close directory write windows left by a forcibly killed client."""
    for directory in (client_dir, observations_dir):
        require_directory(directory)
        directory.chmod(READ_ONLY_DIRECTORY_MODE)
    actions_dir = observations_dir / "actions"
    if actions_dir.exists() or actions_dir.is_symlink():
        require_directory(actions_dir)
        actions_dir.chmod(READ_ONLY_DIRECTORY_MODE)
    for path in (
        client_dir / ".client.lock",
        client_dir / ".client_session.json",
        client_dir / CLIENT_CONFIG_FILENAME,
        observations_dir / "environment_description.md",
    ):
        if path.is_symlink():
            raise RuntimeError(f"Storage file must not be a symbolic link: {path}")
        if path.exists():
            path.chmod(READ_ONLY_FILE_MODE)


def initialize_client_storage(
    client_dir: Path,
    observations_dir: Path,
    *,
    server_url: str,
    observations_path: str | Path | None = None,
    http_read_timeout_seconds: float = DEFAULT_HTTP_READ_TIMEOUT_SECONDS,
) -> None:
    """Protect a fresh per-game copy before launching its container."""
    configuration = ClientConfig.from_dict({
        "server_url": server_url,
        "observations_path": str(observations_dir.resolve() if observations_path is None else observations_path),
        "http_read_timeout_seconds": http_read_timeout_seconds,
    })
    require_directory(client_dir)
    require_directory(observations_dir)
    config_path = client_dir / CLIENT_CONFIG_FILENAME
    if config_path.exists() or config_path.is_symlink():
        raise FileExistsError(f"Client configuration already exists: {config_path}")
    if any(observations_dir.iterdir()) or (client_dir / ".client_session.json").exists():
        raise FileExistsError("Client storage must be fresh before initialization")
    # This inode is permanent: never replace/truncate it when taking the lock.
    lock_fd = os.open(client_dir / ".client.lock", os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o444)
    os.close(lock_fd)
    write_readonly_file(config_path, (json.dumps(configuration.to_dict(), indent=2) + "\n").encode("utf-8"))
    for root, directories, files in os.walk(client_dir, topdown=False):
        for name in directories + files:
            path = Path(root) / name
            if path.is_symlink():
                raise RuntimeError(f"Client storage must not contain symbolic links: {path}")
            path.chmod(READ_ONLY_DIRECTORY_MODE if path.is_dir() else READ_ONLY_FILE_MODE)
    seal_storage(client_dir, observations_dir)
