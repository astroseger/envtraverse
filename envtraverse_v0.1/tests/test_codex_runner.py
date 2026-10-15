from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

AGENT_SRC = Path(__file__).resolve().parents[1] / "src" / "agents" / "agent_v1" / "agent"
sys.path.insert(0, str(AGENT_SRC))

import codex_runner


class CodexRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log_file = self.directory / "agent.log"
        self.thread_started = Mock()
        self.runner = codex_runner.CodexRunner(
            work_dir=self.directory / "workspace", log_file=self.log_file,
            model="test-model", reasoning_effort="medium",
            on_thread_started=self.thread_started,
        )
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.error_output = self.enterContext(redirect_stderr(io.StringIO()))
        self.popen = self.enterContext(patch.object(codex_runner.subprocess, "Popen"))

    def process(self, output: str, return_code=0, stderr=""):
        process = Mock(stdin=Mock(), stdout=io.StringIO(output), stderr=io.StringIO(stderr))
        process.wait.return_value = return_code
        return process

    def test_fresh_send_streams_all_output_and_returns_no_event_list(self) -> None:
        output = (
            'diagnostic text\n\n[]\n'
            '{"type":"thread.started","thread_id":"thread-one"}\n'
            '{"type":"item.completed","item":{"text":"Result"}}\n'
            'another diagnostic\n'
        )
        process = self.process(output)
        self.popen.return_value = process
        self.assertIsNone(self.runner.send("Initial prompt"))
        self.assertEqual(self.output.getvalue(), output)
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), output)
        self.assertEqual(self.runner.current_thread_id, "thread-one")
        self.thread_started.assert_called_once_with("thread-one")
        process.stdin.write.assert_called_once_with("Initial prompt")
        process.stdin.close.assert_called_once()
        command = self.popen.call_args.args[0]
        self.assertNotIn("resume", command)
        self.assertEqual(command[command.index("--cd") + 1], str(self.runner.work_dir))
        self.assertIn("--json", command)

    def test_events_after_the_thread_id_are_logged_without_decoding(self) -> None:
        output = '{"thread_id":"thread-one"}\n' + '{"type":"item.completed"}\n' * 1000
        self.popen.return_value = self.process(output)
        with patch.object(codex_runner.json, "loads", wraps=json.loads) as decode:
            self.assertIsNone(self.runner.send("Prompt"))
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), output)
        self.thread_started.assert_called_once_with("thread-one")

    def test_resume_uses_the_existing_id_and_does_not_decode_events(self) -> None:
        self.runner.resume_session("thread-existing")
        output = '{"thread_id":"thread-other"}\n{"type":"turn.completed"}\n'
        self.popen.return_value = self.process(output)
        with patch.object(codex_runner.json, "loads", side_effect=AssertionError("No decoding needed")):
            self.assertIsNone(self.runner.send("Continue"))
        self.assertEqual(self.runner.current_thread_id, "thread-existing")
        self.thread_started.assert_not_called()
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), output)
        command = self.popen.call_args.args[0]
        self.assertEqual(command[command.index("resume") + 1], "thread-existing")

    def test_subsequent_send_resumes_and_appends_to_the_log(self) -> None:
        initial = '{"thread_id":"thread-one"}\n'
        continuation = '{"type":"turn.completed"}\n'
        self.popen.side_effect = [self.process(initial), self.process(continuation)]
        self.assertIsNone(self.runner.send("Start"))
        self.assertIsNone(self.runner.send("Continue"))
        self.thread_started.assert_called_once_with("thread-one")
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), initial + continuation)
        command = self.popen.call_args_list[1].args[0]
        self.assertEqual(command[command.index("resume") + 1], "thread-one")

    def test_missing_thread_id_still_fails_after_logging_output(self) -> None:
        output = 'not JSON\n{"type":"turn.completed"}\n'
        self.popen.return_value = self.process(output)
        with self.assertRaisesRegex(RuntimeError, "did not report a thread_id"):
            self.runner.send("Start")
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), output)
        self.thread_started.assert_not_called()

    def test_nonzero_exit_still_raises_and_preserves_logs_and_thread_id(self) -> None:
        output = '{"thread_id":"thread-one"}\n{"type":"error","message":"Failed"}\n'
        self.popen.return_value = self.process(output, return_code=17, stderr="Transport error\n")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.runner.send("Start")
        self.assertEqual(raised.exception.returncode, 17)
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), output)
        self.assertEqual(self.runner.stderr_log_file.read_text(encoding="utf-8"), "Transport error\n")
        self.assertEqual(self.error_output.getvalue(), "Transport error\n")
        self.thread_started.assert_called_once_with("thread-one")

    def test_stderr_is_appended_separately_and_not_decoded_as_events(self) -> None:
        stderr = '{"type":"thread.started","thread_id":"wrong-thread"}\n'
        first = '{"type":"thread.started","thread_id":"thread-one"}\n'
        second = '{"type":"turn.completed"}\n'
        self.popen.side_effect = [
            self.process(first, stderr=stderr),
            self.process(second, stderr="Another diagnostic without a final newline"),
        ]
        self.runner.send("Start")
        self.runner.send("Continue")
        expected = stderr + "Another diagnostic without a final newline"
        self.assertEqual(self.runner.stderr_log_file.name, "codex_stderr.log")
        self.assertEqual(self.runner.stderr_log_file.read_text(encoding="utf-8"), expected)
        self.assertEqual(self.error_output.getvalue(), expected)
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), first + second)
        self.thread_started.assert_called_once_with("thread-one")

    def test_stderr_only_does_not_update_agent_log_modification_time(self) -> None:
        self.log_file.write_text("existing events\n", encoding="utf-8")
        os.utime(self.log_file, (100, 100))
        original_mtime = self.log_file.stat().st_mtime_ns
        self.runner.resume_session("existing-thread")
        self.popen.return_value = self.process("", stderr="Still retrying\n")
        self.runner.send("Continue")
        self.assertEqual(self.log_file.stat().st_mtime_ns, original_mtime)
        self.assertEqual(self.runner.stderr_log_file.read_text(encoding="utf-8"), "Still retrying\n")

    def test_new_session_after_a_previous_thread_does_not_resume(self) -> None:
        self.runner.resume_session("old-thread")
        self.runner.new_session()
        self.popen.return_value = self.process('{"type":"thread.started","thread_id":"new-thread"}\n')
        self.runner.send("Initial prompt from status")
        self.assertNotIn("resume", self.popen.call_args.args[0])
        self.thread_started.assert_called_once_with("new-thread")

    def test_failed_thread_persistence_stops_codex_and_preserves_stderr(self) -> None:
        process = self.process('{"thread_id":"thread-one"}\n', stderr="Diagnostic\n")
        self.popen.return_value = process
        self.thread_started.side_effect = OSError("Cannot save thread ID")
        with self.assertRaisesRegex(OSError, "Cannot save thread ID"):
            self.runner.send("Start")
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertEqual(self.runner.stderr_log_file.read_text(encoding="utf-8"), "Diagnostic\n")

    def test_nested_thread_id_is_still_captured_before_later_events(self) -> None:
        prefix = '{"event":{"data":[{"thread_id":"nested-thread"}]}}\n'
        self.popen.return_value = self.process(prefix + '{"type":"turn.completed"}\n')

        def capture(thread_id):
            self.assertEqual(thread_id, "nested-thread")
            self.assertEqual(self.log_file.read_text(encoding="utf-8"), prefix)

        self.thread_started.side_effect = capture
        self.assertIsNone(self.runner.send("Start"))
        self.thread_started.assert_called_once_with("nested-thread")


class CodexRunnerProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runner = codex_runner.CodexRunner(self.directory, self.directory / "agent.log")
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.error_output = self.enterContext(redirect_stderr(io.StringIO()))

    def test_real_process_drains_large_stderr_before_reading_a_large_prompt(self) -> None:
        # This child is a local Python stand-in, not a real Codex/API invocation.
        # An alarm turns a pipe deadlock into a bounded test failure.
        command = [sys.executable, "-c", (
            "import signal, sys; signal.alarm(8); "
            "sys.stderr.write('diagnostic line\\n' * 20000); sys.stderr.flush(); "
            "assert len(sys.stdin.read()) == 300000; "
            "print('{\"type\":\"thread.started\",\"thread_id\":\"local-thread\"}'); "
            "sys.stderr.buffer.write(b'final diagnostic: \\xff')"
        )]
        self.runner._run_command_once(command, "x" * 300000)
        expected = "diagnostic line\n" * 20000 + "final diagnostic: \ufffd"
        self.assertEqual(self.runner.stderr_log_file.read_text(encoding="utf-8"), expected)
        self.assertEqual(self.error_output.getvalue(), expected)
        self.assertEqual(self.runner.current_thread_id, "local-thread")
        self.assertEqual(self.runner.log_file.read_text(encoding="utf-8"), self.output.getvalue())
        self.assertNotIn("diagnostic", self.output.getvalue())

    def test_stderr_is_flushed_while_stdout_is_silent(self) -> None:
        command = [sys.executable, "-c", (
            "import pathlib, signal, sys, time\n"
            "signal.alarm(8)\n"
            "sys.stderr.write('live diagnostic\\n')\n"
            "sys.stderr.flush()\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "while 'live diagnostic' not in path.read_text():\n"
            "    time.sleep(0.01)\n"
            "sys.stdin.read()\n"
            "print('{\"type\":\"thread.started\",\"thread_id\":\"local-thread\"}')\n"
        ), str(self.runner.stderr_log_file)]
        self.runner._run_command_once(command, "Prompt")
        self.assertEqual(self.runner.stderr_log_file.read_text(encoding="utf-8"), "live diagnostic\n")
        self.assertEqual(self.runner.current_thread_id, "local-thread")


if __name__ == "__main__":
    unittest.main()
