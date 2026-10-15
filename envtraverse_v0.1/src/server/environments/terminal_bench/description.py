"""Action reference, resolved solving budget, and Harbor's public instruction."""
from __future__ import annotations

from typing import Any


def environment_description(instruction: str, max_file_bytes: int, *,
                            time_budget: dict[str, Any] | None = None) -> str:
    budget_text = ""
    if time_budget is not None:
        seconds = time_budget.get("agent_timeout_seconds")
        limit = "unlimited" if seconds is None else f"{seconds:g} seconds (wall-clock)"
        budget_text = f"\nSolving time budget: {limit}.\n"
    return f'''Solve the task below in its sandbox using the actions below.
The task sandbox has a separate filesystem from your local workspace. All terminals
opened through these actions share the main container's filesystem and have
independent shells. The initial terminal is t1. New terminals start in the
container's initial working directory, not inside another terminal's current
directory or SSH session.
{budget_text}
Actions are JSON objects:
- {{"id":"terminal_send","terminal_id":"t1","keys":[{{"text":"pwd"}},{{"key":"Enter"}}]}}
  Send keyboard input to the selected interactive terminal. This action
  does not wait for command completion; use terminal_read to inspect output.
  Inputs are sent in order. Each item must contain exactly one of text or key.
  Text is sent literally; no Enter is appended automatically.
  Named keys: Enter, Escape, Tab, BTab, BSpace, Space, Up, Down, Left, Right,
  Home, End, PageUp, PageDown, Delete, Insert, F1-F12, C-a through C-z,
  and M-a through M-z.
  {{"key":"C-c"}} sends Ctrl+C; {{"text":"C-c"}} types three characters.
- {{"id":"terminal_read","terminal_id":"t1"}}
  Read a snapshot of the selected terminal.
  view="screen" (default) returns the entire visible screen and ignores
  the lines parameter.
  view="history" returns the most recent terminal lines from retained
  scrollback and the current screen combined. The lines parameter sets
  the maximum number of lines returned (1..2000, default 200).
  Repeated reads may repeat output.
  Use wait_ms to delay capture, giving running commands more time to produce
  output (0..2000 milliseconds, default 0). This is a fixed delay; reading
  does not wait for a command to finish.
- {{"id":"terminal_new"}}
  Open another independent terminal and return its ID.
- {{"id":"get_file","path":"/absolute/path/in/main/container"}}
  Retrieve a readable regular file, at most {max_file_bytes} bytes. This reads
  from the main container, even while a terminal is connected elsewhere by SSH.
- {{"id":"send_path","path_local":"solution","path_sandbox":"/app/solution"}}
  Copy a regular file or directory from your local workspace into the main
  container. path_local is relative to your workspace (/home/user/run/agent_run/).
  Absolute source paths, '..', symbolic links and special files are rejected.
  For a file, path_sandbox is the absolute destination filename.
  For a directory, its contents are merged into the absolute destination
  directory path_sandbox, creating it if needed.
  The destination's parent must already exist. Matching files are overwritten.
  File permissions, including executable bits, are preserved.
- {{"id":"finish"}}
  Use this action when you believe you have completed the task. It ends the
  solving phase and runs the task verifier. No further solving actions are accepted.

A quiet screen does not mean a process has finished. Processes keep running
between actions. History is bounded and is not a lossless execution log.

Task instruction:
--- BEGIN TASK ---
{instruction}
--- END TASK ---
'''
