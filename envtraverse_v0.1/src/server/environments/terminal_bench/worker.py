"""Private per-session subprocess. Never listens on a public endpoint."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import importlib
import json
import logging
from pathlib import Path
import socket
import struct
from typing import Any

from .protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, RuntimeFault, loads, pack
from .records import error_result
from .settings import TerminalSettings

LOGGER = logging.getLogger(__name__)


async def run_worker(fd: int, spec: dict[str, Any]) -> None:
    transport = socket.socket(fileno=fd)
    transport.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=transport)
    factory_path = spec.pop("worker_factory")
    if factory_path == "terminal_bench.harbor_runner:HarborSession":
        from .harbor_runner import check_harbor
        check_harbor()
    module_name, symbol = factory_path.split(":", 1)
    factory = getattr(importlib.import_module(module_name), symbol)
    spec["task_path"] = Path(spec["task_path"])
    spec["output_dir"] = Path(spec["output_dir"])
    spec["workspace_dir"] = Path(spec["workspace_dir"])
    spec["settings"] = TerminalSettings(**spec["settings"])
    session = factory(**spec)
    sending = asyncio.Lock()
    done = asyncio.Event()
    tasks: set[asyncio.Task] = set()

    async def execute(request: dict[str, Any]) -> None:
        method = request.get("method")
        try:
            if method == "start":
                result = await session.start()
            elif method == "action":
                result = await session.move(request["action"])
            elif method == "close":
                await session.close(reason=request.get("reason", "agent_stopped"))
                result = {"closed": True}
            else:
                raise RuntimeFault("bad_request", "Unknown worker method")
            response = {"request_id": request.get("request_id"), "ok": True, "result": result}
        except RuntimeFault as exc:
            response = {"request_id": request.get("request_id"), "ok": False,
                        "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            LOGGER.exception("Session operation failed")
            if method == "start":
                error_result(spec["output_dir"] / spec["session_id"],
                             session_id=spec["session_id"], game_id=spec["task_id"],
                             benchmark=spec["benchmark"], task_name=spec["task_name"],
                             exception_type=type(exc).__name__, note="Worker initialization failed; inspect runtime.log")
            response = {"request_id": request.get("request_id"), "ok": False,
                        "error": {"code": "environment_error", "message": "Worker operation failed; inspect session logs"}}
        try:
            try:
                data = pack(response, MAX_RESPONSE_BYTES)
            except ValueError:
                data = pack({"request_id": request.get("request_id"), "ok": False,
                             "error": {"code": "environment_error", "message": "Worker response exceeds size limit"}},
                            MAX_RESPONSE_BYTES)
            async with sending:
                writer.write(data)
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            done.set()
        finally:
            if method == "close":
                done.set()

    async def receive() -> None:
        try:
            while True:
                length = struct.unpack("!I", await reader.readexactly(4))[0]
                if length > MAX_REQUEST_BYTES:
                    raise ValueError("Worker request too large")
                request = loads(await reader.readexactly(length))
                task = asyncio.create_task(execute(request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            done.set()

    receiver = asyncio.create_task(receive())
    try:
        await done.wait()
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        # Also cleans up when the supervisor disappears. Hard-killed workers cannot promise cleanup.
        await session.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with suppress(ConnectionError, BrokenPipeError):
            await writer.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    spec = json.loads(args.spec.read_text())
    try:
        asyncio.run(run_worker(args.fd, dict(spec)))
    except BaseException as exc:
        LOGGER.exception("Worker failed")
        error_result(Path(spec["output_dir"]) / spec["session_id"],
                     session_id=spec["session_id"], game_id=spec["task_id"],
                     benchmark=spec["benchmark"], task_name=spec["task_name"],
                     exception_type=type(exc).__name__, note="Session worker failed; inspect runtime.log",
                     cleanup_required=True)
        raise


if __name__ == "__main__":
    main()
