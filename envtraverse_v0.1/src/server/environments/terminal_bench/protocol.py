"""Small length-prefixed JSON protocol over an AF_UNIX stream.

One request/response per connection. No pickle and no automatic retries.
"""
from __future__ import annotations

import base64
import json
import socket
import struct
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 3
MAX_REQUEST_BYTES = 1 * 1024 * 1024
# A 32 MiB file uses about 43 MiB after base64 encoding; leave room for metadata.
MAX_RESPONSE_BYTES = 48 * 1024 * 1024


class RuntimeFault(Exception):
    """A public, sanitized runtime error safe to return to an agent."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def loads(data: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number: {value}")
    value = json.loads(data, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def pack(value: Any, limit: int) -> bytes:
    data = dumps(value)
    if len(data) > limit:
        raise ValueError(f"Message exceeds {limit}-byte size limit")
    return struct.pack("!I", len(data)) + data


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise ConnectionError("Runtime closed the connection before replying")
        chunks.extend(chunk)
    return bytes(chunks)


class RuntimeClient:
    def __init__(self, socket_path: str | Path, timeout: float | None = None):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def call(self, method: str, **parameters: Any) -> dict[str, Any]:
        request = {"version": PROTOCOL_VERSION, "method": method, **parameters}
        try:
            frame = pack(request, MAX_REQUEST_BYTES)
        except (TypeError, ValueError) as exc:
            # Nothing has been sent: invalid input is not a connection failure.
            raise RuntimeFault("bad_request", str(exc)) from exc
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            sock.sendall(frame)
            size = struct.unpack("!I", _recv_exact(sock, 4))[0]
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("Runtime response exceeds size limit")
            response = loads(_recv_exact(sock, size))
        if response.get("version") != PROTOCOL_VERSION:
            raise ValueError("Unsupported runtime protocol version")
        if response.get("ok") is not True:
            error = response.get("error", {})
            raise RuntimeFault(str(error.get("code", "runtime_error")),
                               str(error.get("message", "Runtime operation failed")))
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("Malformed runtime result")
        return result


def valid_filename(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and not any(c in name for c in "/\\\0")


def observation(files: dict[str, str | bytes], terminal: bool = False) -> dict[str, Any]:
    encoded = {}
    for name, value in files.items():
        if not valid_filename(name):
            raise ValueError("Observation names must be flat filenames")
        if isinstance(value, str):
            encoded[name] = {"text": value}
        elif isinstance(value, bytes):
            encoded[name] = {"base64": base64.b64encode(value).decode("ascii")}
        else:
            raise TypeError("Observation content must be text or bytes")
    return {"files": encoded, "terminal": terminal}


def terminal_observation() -> dict[str, Any]:
    return observation({"message.txt": "The solving phase has ended. No further actions are needed.\n"}, terminal=True)


def decode_observation(raw: dict[str, Any]) -> tuple[dict[str, str | bytes], bool]:
    if not isinstance(raw, dict) or type(raw.get("terminal")) is not bool:
        raise ValueError("Malformed observation")
    if not isinstance(raw.get("files"), dict):
        raise ValueError("Malformed observation files")
    files: dict[str, str | bytes] = {}
    for name, value in raw["files"].items():
        if not isinstance(name, str) or not valid_filename(name) or not isinstance(value, dict):
            raise ValueError("Malformed observation artifact")
        if set(value) == {"text"} and isinstance(value["text"], str):
            files[name] = value["text"]
        elif set(value) == {"base64"} and isinstance(value["base64"], str):
            files[name] = base64.b64decode(value["base64"], validate=True)
        else:
            raise ValueError("Malformed observation content")
    return files, raw["terminal"]
