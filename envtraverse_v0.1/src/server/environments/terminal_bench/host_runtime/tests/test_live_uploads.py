"""Actual client + HTTP server + worker + Harbor/Docker workspace upload tests."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from terminal_bench.catalog import BenchmarkCatalog, BUILTIN_SMOKE, SMOKE_GAME
from terminal_bench.runtime import RuntimeService


@unittest.skipUnless(
    os.environ.get("TB_RUN_DOCKER_SMOKE") == "1"
    and shutil.which("docker") and importlib.util.find_spec("harbor"),
    "Real upload tests require TB_RUN_DOCKER_SMOKE=1 and Docker/Harbor",
)
class LiveWorkspaceUploads(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tb_upload_")
        self.root = Path(self.temporary.name)
        self.project = Path(__file__).resolve().parents[6]
        self.python = os.environ.get("TB_SERVER_PYTHON", "python3")
        self.servers = []
        self.service = None
        process = await asyncio.create_subprocess_exec(
            self.python, "-c", "import flask, requests", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        output, _ = await process.communicate()
        self.assertEqual(process.returncode, 0, "HTTP test interpreter needs Flask/Requests: " + output.decode())

    async def asyncTearDown(self):
        for process in self.servers:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self.service is not None:
            await self.service.stop()
        self.temporary.cleanup()

    async def start_runtime(self, user_mode="root"):
        fixture = self.root / "fixture"
        shutil.copytree(BUILTIN_SMOKE, fixture)
        dockerfile = fixture / "environment/Dockerfile"
        dockerfile.write_text(dockerfile.read_text() + """
RUN useradd -m -u 12345 -U solver \\
    && chown solver:solver /app \\
    && mkdir /protected \\
    && printf 'original' > /protected/keep.txt \\
    && chmod 755 /protected && chmod 644 /protected/keep.txt
""" + ("USER solver\n" if user_mode == "image" else ""))
        config = fixture / "task.toml"
        text = config.read_text().replace("timeout_sec = 120.0", "timeout_sec = 240.0")
        text = text.replace("[verifier]\n", '[verifier]\nuser = "root"\n')
        if user_mode == "task":
            text = text.replace("[agent]\n", '[agent]\nuser = "solver"\n')
        config.write_text(text)
        self.service = RuntimeService(
            socket_path=self.root / "runtime.sock", output_dir=self.root / "results",
            runs_dir=self.root / "run", catalog=BenchmarkCatalog(smoke_task=fixture))
        await self.service.start()

    async def start_client(self):
        alias = uuid4().hex
        game = self.root / "run" / alias
        workspace = game / "run/agent_run"
        workspace.mkdir(parents=True)
        server_log = game / "server.log"
        with (game / "server-process.log").open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                self.python, str(self.project / "src/server/server.py"),
                "--environment", "terminal_bench", "--game-id", SMOKE_GAME,
                "--game-alias", alias, "--port", "0", "--parameters",
                json.dumps({"socket_path": str(self.root / "runtime.sock"), "session_id": alias}),
                env=dict(os.environ, ENV_SERVER_LOG_PATH=str(server_log), PYTHONDONTWRITEBYTECODE="1"),
                stdout=output, stderr=asyncio.subprocess.STDOUT)
        self.servers.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.assertIsNone(process.returncode, (game / "server-process.log").read_text())
            text = server_log.read_text() if server_log.exists() else ""
            match = re.search(r"starting server .* port=(\d+)", text)
            if match:
                port = int(match.group(1))
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("HTTP server did not become ready: " + (game / "server-process.log").read_text())
        client_source = self.project / "src/agents/agent_v1/agent/client"
        client_dir = game / "client"
        shutil.copytree(client_source, client_dir, ignore=shutil.ignore_patterns("__pycache__"))
        observations = game / "observations"
        observations.mkdir()
        spec = importlib.util.spec_from_file_location("_upload_client_storage", client_source / "client_storage.py")
        storage = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = storage
        try:
            spec.loader.exec_module(storage)
            storage.initialize_client_storage(client_dir, observations, server_url=f"http://127.0.0.1:{port}")
        finally:
            sys.modules.pop(spec.name, None)
        client = {"alias": alias, "workspace": workspace, "dir": client_dir, "observations": observations}
        await self.cli(client, "start", alias)
        return client

    async def cli(self, client, *args, succeeds=True):
        process = await asyncio.create_subprocess_exec(
            self.python, str(client["dir"] / "client.py"), *args,
            # Deliberately not the workspace: path_local is workspace-relative.
            cwd=self.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 240)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if succeeds:
            self.assertEqual(process.returncode, 0, output.decode())
        else:
            self.assertNotEqual(process.returncode, 0, output.decode())
        return output.decode()

    async def move(self, client, action, succeeds=True):
        await self.cli(client, "move", json.dumps(action), succeeds=succeeds)
        state = json.loads((client["dir"] / ".client_session.json").read_text())
        prefix = f"step_{state['step_index']:04d}_"
        return {p.name[len(prefix):]: p.read_bytes()
                for p in client["observations"].glob(prefix + "*")}

    async def send_path(self, client, source, destination, succeeds=True):
        return await self.move(client, {"id": "send_path", "path_local": source,
                                       "path_sandbox": destination}, succeeds=succeeds)

    async def terminal_command(self, client, command):
        marker = uuid4().hex
        command += f"; printf '\\nUPLOAD_%s_%s\\n' {marker} $?"
        await self.move(client, {"id": "terminal_send", "terminal_id": "t1",
                                "keys": [{"text": command}, {"key": "Enter"}]})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            files = await self.move(client, {"id": "terminal_read", "terminal_id": "t1", "wait_ms": 100})
            screen = files["screen.txt"].decode()
            match = re.search("UPLOAD_" + marker + r"_(\d+)", screen)
            if match:
                self.assertEqual(match.group(1), "0", screen)
                return screen
        self.fail("Terminal command did not complete: " + screen)

    async def finish(self, client):
        await self.move(client, {"id": "finish"})
        await asyncio.wait_for(self.service.completions[client["alias"]], 120)
        result = json.loads((self.root / "results" / client["alias"] / "result.json").read_text())
        self.assertEqual(result["verification_status"], "finished", result)
        self.assertEqual(result["rewards"], {"reward": 1.0}, result)

    async def exercise_uploads(self, user_mode):
        await self.start_runtime(user_mode)
        client = await self.start_client()
        workspace = client["workspace"]
        uid = 0 if user_mode == "root" else 12345
        name = "a 'quote' $(not-a-command) Ω.bin"
        data = bytes(range(256)) * 8
        (workspace / name).write_bytes(data)
        (workspace / name).chmod(0o600)
        destination = "/app/" + name
        files = await self.send_path(client, name, destination)
        self.assertEqual(json.loads(files["transfer.json"])["bytes"], len(data))
        received = await self.move(client, {"id": "get_file", "path": destination})
        self.assertEqual(received["file.bin"], data)

        tree = workspace / "solution"
        (tree / "nested").mkdir(parents=True)
        (tree / "empty").mkdir()
        (tree / "empty").chmod(0o700)
        (tree / "nested/data.bin").write_bytes(data)
        (tree / "nested/data.bin").chmod(0o640)
        (tree / "run.sh").write_bytes(b"#!/bin/sh\nprintf executed\\n\n")
        (tree / "run.sh").chmod(0o750)
        tree.chmod(0o750)
        await self.terminal_command(client, "mkdir -p /app/copied; printf untouched > /app/copied/keep")
        await self.send_path(client, "solution", "/app/copied")
        # Overwriting an existing file and merging a second directory copy.
        (tree / "nested/data.bin").write_bytes(b"replacement\0\xff")
        await self.send_path(client, "solution", "/app/copied")
        (workspace / "empty-file").touch()
        await self.send_path(client, "empty-file", "/app/empty-file")

        expected_large_hash = None
        if user_mode == "root":
            large = workspace / "large.bin"
            with large.open("wb") as stream:
                for _ in range(33):
                    stream.write(bytes(range(256)) * 4096)
            expected_large_hash = hashlib.sha256(large.read_bytes()).hexdigest()
            files = await self.send_path(client, "large.bin", "/app/large.bin")
            self.assertEqual(json.loads(files["transfer.json"])["bytes"], 33 * 1024 * 1024)
            # Downloads retain their separate size limit.
            await self.move(client, {"id": "get_file", "path": "/app/large.bin"}, succeeds=False)
            large_directory = workspace / "large-directory"
            large_directory.mkdir()
            large.rename(large_directory / "large.bin")
            files = await self.send_path(client, "large-directory", "/app/large-directory")
            self.assertEqual(json.loads(files["transfer.json"])["bytes"], 33 * 1024 * 1024)

        (workspace / "link").symlink_to(self.root / "fixture/task.toml")
        await self.send_path(client, "link", "/app/rejected", succeeds=False)
        await self.send_path(client, "../outside", "/app/rejected", succeeds=False)
        await self.send_path(client, str(workspace / name), "/app/rejected", succeeds=False)
        await self.send_path(client, "missing", "/app/rejected", succeeds=False)
        if uid:
            await self.send_path(client, name, "/protected/keep.txt", succeeds=False)
            await self.send_path(client, "solution", "/protected/tree", succeeds=False)

        checker = workspace / "check.py"
        checker.write_text("import hashlib, os, stat\nfrom pathlib import Path\n" +
            f"assert os.getuid() == {uid}, os.getuid()\n" +
            f"p = Path({destination!r})\n" +
            f"assert p.stat().st_uid == {uid} and p.stat().st_gid == {uid}\n" +
            "assert stat.S_IMODE(p.stat().st_mode) == 0o600\n" +
            "assert Path('/app/empty-file').read_bytes() == b''\n" +
            "assert Path('/app/copied/nested/data.bin').read_bytes() == b'replacement\\0\\xff'\n" +
            "assert Path('/app/copied/keep').read_text() == 'untouched'\n" +
            "assert Path('/app/copied/empty').is_dir()\n" +
            "for name, mode in [('', 0o750), ('run.sh', 0o750), ('nested/data.bin', 0o640), ('empty', 0o700)]:\n" +
            "    p = Path('/app/copied') / name\n" +
            f"    assert p.stat().st_uid == {uid}, (p, p.stat())\n" +
            f"    assert p.stat().st_gid == {uid}, (p, p.stat())\n" +
            "    assert stat.S_IMODE(p.stat().st_mode) == mode, (p, p.stat())\n" +
            "assert not list(Path('/tmp').glob('.workspace-upload-*'))\n" +
            "assert not Path('/app/rejected').exists()\n" +
            ("assert Path('/protected/keep.txt').read_text() == 'original'\n"
             "assert Path('/protected/keep.txt').stat().st_uid == 0\n"
             "assert stat.S_IMODE(Path('/protected/keep.txt').stat().st_mode) == 0o644\n"
             "assert not Path('/protected/tree').exists()\n" if uid else "") +
            (f"for name in ('/app/large.bin', '/app/large-directory/large.bin'):\n"
             f"    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == {expected_large_hash!r}\n"
             if expected_large_hash else ""))
        await self.send_path(client, "check.py", "/app/check.py")
        await self.terminal_command(client, "python3 /app/check.py && /app/copied/run.sh")
        (workspace / "answer").write_bytes(b"hello from terminal bench\n")
        await self.send_path(client, "answer", "/app/answer.txt")
        await self.finish(client)
        actions = list((client["observations"] / "actions").glob("*.txt"))
        self.assertTrue(actions)
        self.assertTrue(all(path.stat().st_size < 4096 for path in actions))
        spec = json.loads((self.root / "results" / client["alias"] / "worker-spec.json").read_text())
        self.assertEqual(Path(spec["workspace_dir"]), workspace)
        staging = self.root / "results" / client["alias"] / "uploads"
        self.assertTrue(staging.is_dir())
        self.assertEqual(list(staging.iterdir()), [])

    async def test_root_files_and_directories_above_32_mib(self):
        await self.exercise_uploads("root")

    async def test_task_configured_user_owns_uploads_and_cannot_write_protected_paths(self):
        await self.exercise_uploads("task")

    async def test_dockerfile_user_owns_uploads_without_task_user_setting(self):
        await self.exercise_uploads("image")

    async def test_two_sessions_resolve_the_same_relative_path_independently(self):
        await self.start_runtime()
        first, second = await asyncio.gather(self.start_client(), self.start_client())
        for client, content in ((first, b"first"), (second, b"second")):
            (client["workspace"] / "same").write_bytes(content)
        await asyncio.gather(*(self.send_path(c, "same", "/app/same") for c in (first, second)))
        for client, content, other in ((first, b"first", second), (second, b"second", first)):
            result = await self.move(client, {"id": "get_file", "path": "/app/same"})
            self.assertEqual(result["file.bin"], content)
            (client["workspace"] / "other").symlink_to(other["workspace"], target_is_directory=True)
            await self.send_path(client, "other/same", "/app/other", succeeds=False)
            (client["workspace"] / "answer").write_bytes(b"hello from terminal bench\n")
            await self.send_path(client, "answer", "/app/answer.txt")
        await asyncio.gather(self.finish(first), self.finish(second))
