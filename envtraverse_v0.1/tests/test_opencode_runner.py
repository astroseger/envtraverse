from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/agents/opencode_agent_v1_08_wstop3/agent/opencode_runner.py"
SPEC = importlib.util.spec_from_file_location("opencode_runner_under_test", SOURCE)
runner_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner_module)


def event(kind: str, session="ses_test", **fields) -> str:
    return json.dumps({"type": kind, "sessionID": session, **fields}) + "\n"


class OpenCodeRunnerTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workdir = self.directory / "workspace with spaces"
        self.workdir.mkdir()
        self.log = self.directory / "agent.log"
        self.callback = Mock()
        self.runner = runner_module.OpenCodeRunner(
            self.workdir, self.log, model="gpt-5.6-sol", reasoning_effort="max",
            on_session_started=self.callback,
        )
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.popen = self.enterContext(patch.object(runner_module.subprocess, "Popen"))
        self.preflight = self.enterContext(patch.object(runner_module.subprocess, "run"))
        self.preflight.return_value = subprocess.CompletedProcess([], 0)
        self.killpg = self.enterContext(patch("os.killpg"))

    @staticmethod
    def process(output: str, status=0, stderr=""):
        process = Mock(stdin=Mock(), stdout=io.StringIO(output), stderr=io.StringIO(stderr), pid=123456)
        process.wait.return_value = status
        process.poll.return_value = None
        return process

    def test_new_session_passes_stdin_and_correct_working_directory(self):
        output = event("step_start") + event("text", part={"text": "done"})
        process = self.process(output)
        self.popen.return_value = process
        self.assertIsNone(self.runner.send("Initial prompt\nwith details"))
        command = self.popen.call_args.args[0]
        self.assertEqual(command[:2], ["opencode", "run"])
        for flag in ("--standalone", "--auto", "--format"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--format") + 1], "json")
        self.assertEqual(command[command.index("--model") + 1], "openai/gpt-5.6-sol#max")
        self.assertNotIn("--session", command)
        self.assertEqual(Path(self.popen.call_args.kwargs["cwd"]), self.workdir)
        self.assertEqual(self.popen.call_args.kwargs["env"]["PWD"], str(self.workdir))
        process.stdin.write.assert_called_once_with("Initial prompt\nwith details")
        process.stdin.close.assert_called_once()
        self.assertEqual(self.runner.current_session_id, "ses_test")
        self.callback.assert_called_once_with("ses_test")
        self.preflight.assert_not_called()
        self.assertEqual(self.log.read_text(), output)
        self.assertEqual((self.directory / "opencode_events.jsonl").read_text(), output)

    def test_native_defaults_and_explicit_cli_options(self):
        default = runner_module.OpenCodeRunner(self.workdir, self.log)
        self.popen.return_value = self.process(event("step_start"))
        default.send("Prompt")
        command = self.popen.call_args.args[0]
        self.assertNotIn("--model", command)
        self.assertNotIn("--agent", command)
        self.assertNotIn("--thinking", command)
        configured = runner_module.OpenCodeRunner(
            self.workdir, self.log, model="openai/gpt-6.1-sol#medium",
            thinking=True, opencode_agent="build",
        )
        self.popen.return_value = self.process(event("step_start", "ses_other"))
        configured.send("Prompt")
        command = self.popen.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "openai/gpt-6.1-sol#medium")
        self.assertIn("--thinking", command)
        self.assertEqual(command[command.index("--agent") + 1], "build")

    def test_recovery_verifies_session_existence_before_resume(self):
        self.runner.resume_session(" ses_existing ")
        self.popen.return_value = self.process(event("text", "ses_existing", part={"text": "continued"}))
        self.runner.send("Continue")
        lookup = self.preflight.call_args.args[0]
        self.assertIn("session.get", lookup)
        self.assertIn("sessionID=ses_existing", lookup)
        self.assertIn("--standalone", lookup)
        self.assertEqual(self.preflight.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertTrue(self.preflight.call_args.kwargs["check"])
        self.assertEqual(self.preflight.call_args.kwargs["env"]["PWD"], str(self.workdir))
        command = self.popen.call_args.args[0]
        self.assertEqual(command[command.index("--session") + 1], "ses_existing")
        self.callback.assert_not_called()
        self.assertEqual(self.runner.current_session_id, "ses_existing")

    def test_missing_recovery_session_never_starts_a_new_conversation(self):
        self.runner.resume_session("ses_missing")
        self.preflight.side_effect = subprocess.CalledProcessError(1, ["opencode", "api", "session.get"])
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            self.runner.send("Continue")
        self.popen.assert_not_called()
        self.assertEqual(self.runner.current_session_id, "ses_missing")

    def test_all_stdout_refreshes_progress_log(self):
        self.runner.resume_session("ses_test")
        self.log.write_text("previous progress\n")
        os.utime(self.log, (100, 100))
        original_mtime = self.log.stat().st_mtime_ns
        output = (event("retry", attempt=1) + event("background_wait", jobs=[])
                  + event("unknown_event") + "diagnostic text\n\n[]\n")
        self.popen.return_value = self.process(output, stderr="Transport diagnostic\n")
        self.runner.send("Continue")
        self.assertEqual(self.log.read_text(), "previous progress\n" + output)
        self.assertGreater(self.log.stat().st_mtime_ns, original_mtime)
        self.assertEqual((self.directory / "opencode_events.jsonl").read_text(), output)
        self.assertEqual((self.directory / "opencode_stderr.log").read_text(), "Transport diagnostic\n")

    def test_stderr_alone_does_not_refresh_progress_log(self):
        self.runner.resume_session("ses_test")
        self.log.write_text("previous progress\n")
        os.utime(self.log, (100, 100))
        original_mtime = self.log.stat().st_mtime_ns
        self.popen.return_value = self.process("", stderr="Transport diagnostic\n")
        self.runner.send("Continue")
        self.assertEqual(self.log.read_text(), "previous progress\n")
        self.assertEqual(self.log.stat().st_mtime_ns, original_mtime)
        self.assertEqual((self.directory / "opencode_events.jsonl").read_text(), "")
        self.assertEqual((self.directory / "opencode_stderr.log").read_text(), "Transport diagnostic\n")

    def test_json_error_is_failure_even_when_cli_exit_is_zero(self):
        output = event("error", error={"message": "model unavailable"})
        self.popen.return_value = self.process(output)
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            self.runner.send("Prompt")
        self.assertEqual(self.log.read_text(), output)

    def test_nonzero_exit_preserves_session_id_and_diagnostics(self):
        self.popen.return_value = self.process(event("step_start"), status=17, stderr="failure details\n")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.runner.send("Prompt")
        self.assertEqual(raised.exception.returncode, 17)
        self.callback.assert_called_once_with("ses_test")
        self.assertEqual((self.directory / "opencode_stderr.log").read_text(), "failure details\n")

    def test_missing_session_id_fails(self):
        self.popen.return_value = self.process('{"type":"text","part":{"text":"unattributed"}}\n')
        with self.assertRaises(RuntimeError):
            self.runner.send("Prompt")
        self.callback.assert_not_called()

    def test_new_session_discards_saved_session_and_preflight(self):
        self.runner.resume_session("ses_old")
        self.runner.new_session()
        self.popen.return_value = self.process(event("step_start", "ses_new"))
        self.runner.send("Start again")
        self.preflight.assert_not_called()
        self.assertNotIn("--session", self.popen.call_args.args[0])
        self.callback.assert_called_once_with("ses_new")

    def test_failed_session_id_persistence_stops_child(self):
        self.callback.side_effect = OSError("Cannot save session ID")
        process = self.process(event("step_start"))
        self.popen.return_value = process
        with self.assertRaisesRegex(OSError, "Cannot save session ID"):
            self.runner.send("Prompt")
        self.assertTrue(self.killpg.called or process.kill.called or process.terminate.called)
        self.assertTrue(process.wait.called)


class OpenCodeRunnerPipeTest(unittest.TestCase):
    def test_large_stderr_and_prompt_do_not_deadlock(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            executable = directory / "opencode"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, signal, sys\n"
                "signal.alarm(8)\n"
                "sys.stderr.write('diagnostic line\\n' * 20000); sys.stderr.flush()\n"
                "assert len(sys.stdin.read()) == 300000\n"
                "print(json.dumps({'type':'step_start','sessionID':'ses_pipe'}))\n"
                "sys.stderr.buffer.write(b'final diagnostic: \\xff')\n"
            )
            executable.chmod(0o755)
            runner = runner_module.OpenCodeRunner(directory, directory / "agent.log")
            with patch.dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}"), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                runner.send("x" * 300000)
            self.assertEqual(runner.current_session_id, "ses_pipe")
            self.assertEqual((directory / "opencode_stderr.log").read_text(),
                             "diagnostic line\n" * 20000 + "final diagnostic: \ufffd")


if __name__ == "__main__":
    unittest.main()
