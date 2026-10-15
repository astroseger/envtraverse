from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import curses
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.agimaze_single import replay_single as replay
from analysis_scripts.agimaze_single import replay_single_terminal as terminal


MAZE = "TUTORIAL/STAGE-01/0000.json"
SOLUTION = [{"id": action} for action in ("down", "left", "up", "left", "up", "right", "left", "down", "left")]


class TerminalReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "actions.json"
        self.path.write_text(json.dumps([{"environment_id": MAZE, "actions": [{"id": "up"}]}]))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def test_terminal_uses_the_same_loader_replay_and_cleanup(self) -> None:
        self.assertIs(terminal.replay_single, replay)
        self.enterContext(patch.object(replay, "start_server"))
        stop = self.enterContext(patch.object(replay, "stop_server"))
        post = self.enterContext(patch.object(replay, "http_post_json", side_effect=[
            {"id": "session", "map": "ST\n", "formal": {"status": "success"}},
            {"done": True, "map": "-T\n", "formal": {"events": ["exited"]}},
        ]))
        args = terminal.parse_args([str(self.path), MAZE])
        metadata, frames = terminal.collect_replay(args)
        self.assertEqual(metadata.json_path, self.path)
        self.assertEqual(metadata.name_key, "environment_id")
        self.assertEqual(metadata.labyrinth_name, MAZE)
        self.assertEqual(metadata.action_count, 1)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].replay_map, "ST")
        self.assertEqual(frames[0].status, "success")
        self.assertEqual(frames[1].label, "action_0000")
        self.assertEqual(frames[1].action, "up")
        self.assertEqual(frames[1].events, ["exited"])
        self.assertTrue(frames[1].done)
        self.assertEqual(post.call_count, 2)
        stop.assert_called_once()

    def test_conversion_failure_closes_shared_generator(self) -> None:
        closed = Mock()

        def steps(*args, **kwargs):
            try:
                yield replay.ReplayStep(0, None, {})
            finally:
                closed()

        with patch.object(replay, "replay_steps", steps), \
                patch.object(terminal, "_frame_from_step", side_effect=ValueError("cannot render")):
            with self.assertRaisesRegex(ValueError, "cannot render"):
                terminal.collect_replay(terminal.parse_args([str(self.path), MAZE]))
        closed.assert_called_once()

    def test_frame_fields_prefer_top_level_values_and_handle_missing_optional_data(self) -> None:
        frame = terminal._frame_from_step(replay.ReplayStep(0, None, {
            "status": "top", "events": [], "map": None,
            "formal": {"status": "nested", "events": ["ignored"], "observe": "empty"},
        }))
        self.assertEqual(frame.status, "top")
        self.assertEqual(frame.observe, "empty")
        self.assertEqual(frame.events, [])
        self.assertEqual(frame.replay_map, "")
        self.assertIsNone(frame.done)

    def test_player_keeps_step_jump_pause_and_quit_controls(self) -> None:
        metadata = terminal.ReplayMetadata(self.path, "name", MAZE, 2, "http://127.0.0.1:8000")
        frames = [terminal._frame_from_step(replay.ReplayStep(i, None if i == 0 else "up", {})) for i in range(3)]
        screen = Mock()
        screen.getch.side_effect = [curses.KEY_RIGHT, curses.KEY_RIGHT, curses.KEY_LEFT,
                                   curses.KEY_UP, curses.KEY_DOWN, ord(" "), -1, ord("q")]
        with patch.object(terminal, "_init_curses"), patch.object(terminal, "_draw") as draw:
            terminal.run_player(screen, metadata, frames, 0.15)
        self.assertEqual([item.args[3] for item in draw.call_args_list], [0, 1, 2, 1, 0, 2, 2, 2])
        self.assertEqual(draw.call_args.args[4:], (False, True))

    def test_draw_displays_new_metadata_without_results_or_indices(self) -> None:
        metadata = terminal.ReplayMetadata(self.path, "environment_id", MAZE, 0, "http://127.0.0.1:8000")
        screen = Mock()
        screen.getmaxyx.return_value = (30, 160)
        frames = [terminal._frame_from_step(replay.ReplayStep(0, None, {}))]
        with patch.object(curses, "color_pair", return_value=0):
            terminal._draw(screen, metadata, frames, 0, False, False)
        text = "\n".join(item.args[2] for item in screen.addstr.call_args_list)
        self.assertIn(f"environment_id={MAZE}", text)
        self.assertIn("source=actions.json", text)
        self.assertNotIn("results.json", text)
        self.assertNotIn("labyrinth_index", text)

    def test_main_collects_before_opening_player_and_handles_interrupts_and_terminal_errors(self) -> None:
        args = [str(self.path), MAZE]
        with patch.object(terminal, "collect_replay", return_value=("metadata", ["frame"])), \
                patch.object(curses, "wrapper") as wrapper:
            self.assertEqual(terminal.main(args), 0)
            wrapper.assert_called_once_with(terminal.run_player, "metadata", ["frame"], 0.15)
            wrapper.side_effect = curses.error("no terminal")
            self.assertEqual(terminal.main(args), 1)
            wrapper.side_effect = KeyboardInterrupt
            self.assertEqual(terminal.main(args), 130)
        self.assertIn("no terminal", self.stderr.getvalue())

    def test_both_clis_validate_shared_arguments_and_terminal_delay(self) -> None:
        for parser in (replay.parse_args, terminal.parse_args):
            for option, value in (("--port", "0"), ("--port", "65536"),
                                  ("--timeout", "0"), ("--timeout", "nan"), ("--timeout", "inf")):
                with self.subTest(parser=parser, option=option, value=value), self.assertRaises(SystemExit):
                    parser([str(self.path), MAZE, option, value])
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(delay=value), self.assertRaises(SystemExit):
                terminal.parse_args([str(self.path), MAZE, "--delay", value])

    def test_both_script_entrypoints_work_from_another_directory(self) -> None:
        for name in ("replay_single.py", "replay_single_terminal.py"):
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, "-B", "-S", str(ROOT / "analysis_scripts/agimaze_single" / name), "--help"],
                    cwd=self.root, text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("json_file maze_name", result.stdout)
                self.assertNotIn("labyrinth_index", result.stdout)


class LocalBackendReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The backend logs wins. Isolate those logs without modifying the vendored
        # server or any original run: only its script is copied, assets are shared.
        self.backend = self.root / "labyrinth"
        (self.backend / "scripts").mkdir(parents=True)
        shutil.copyfile(replay.LABYRINTH_DIR / "scripts/webui_server.py", self.backend / "scripts/webui_server.py")
        for name in ("src", "packs", "webui"):
            (self.backend / name).symlink_to(replay.LABYRINTH_DIR / name, target_is_directory=True)
        self.enterContext(patch.object(replay, "LABYRINTH_DIR", self.backend))
        self.path = self.root / "raw_actions.json"
        self.path.write_text(json.dumps([{"environment_id": MAZE, "actions": SOLUTION}]))
        with socket.create_server(("127.0.0.1", 0)) as listener:
            self.port = listener.getsockname()[1]

    def test_real_text_replay_reaches_exit_and_preserves_input(self) -> None:
        before = self.path.read_bytes()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = replay.main([str(self.path), MAZE, "--port", str(self.port)])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(stdout.getvalue().count("\n=== "), 10)
        self.assertIn("done: True", stdout.getvalue())
        self.assertIn('events: ["exited"]', stdout.getvalue())
        self.assertEqual(self.path.read_bytes(), before)
        with socket.create_server(("127.0.0.1", self.port)):
            pass  # The child has shut down and released its listening port.

    def test_real_terminal_collection_and_zero_action_record(self) -> None:
        args = terminal.parse_args([str(self.path), MAZE, "--port", str(self.port)])
        metadata, frames = terminal.collect_replay(args)
        self.assertEqual(metadata.action_count, 9)
        self.assertEqual(len(frames), 10)
        self.assertTrue(frames[-1].done)
        self.assertIn("exited", frames[-1].events)
        self.assertTrue(all(frame.replay_map for frame in frames))
        self.path.write_text(json.dumps([{"name": MAZE, "actions": []}]))
        metadata, frames = terminal.collect_replay(args)
        self.assertEqual(metadata.name_key, "name")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].label, "start")

    def test_real_terminal_ui_starts_and_quits_in_a_pseudoterminal(self) -> None:
        child_code = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from analysis_scripts.agimaze_single import replay_single_terminal as player; "
            "player.replay_single.LABYRINTH_DIR = Path(sys.argv[2]); "
            "raise SystemExit(player.main(sys.argv[3:]))"
        )
        master_fd, slave_fd = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 160, 0, 0))
            process = subprocess.Popen(
                [sys.executable, "-B", "-S", "-c", child_code, str(ROOT), str(self.backend),
                 str(self.path), MAZE, "--port", str(self.port), "--delay", "1"],
                cwd=self.root, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env={**os.environ, "TERM": "xterm-256color"}, start_new_session=True,
            )
            output = b""
            deadline = time.monotonic() + 15
            # A screen refresh can arrive in multiple PTY reads. Wait for both
            # fields asserted below instead of stopping after the first row.
            while (b"frame 1/10" not in output or b"environment_id=" not in output) and process.poll() is None and time.monotonic() < deadline:
                if select.select([master_fd], [], [], 0.1)[0]:
                    output += os.read(master_fd, 65536)
            self.assertIn(b"frame 1/10", output, output.decode(errors="replace"))
            self.assertIn(b"environment_id=", output)
            os.write(master_fd, b"q")
            self.assertEqual(process.wait(timeout=5), 0, output.decode(errors="replace"))
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            os.close(master_fd)
            os.close(slave_fd)


if __name__ == "__main__":
    unittest.main()
