"""Public task instructions and real-time action semantics."""
from __future__ import annotations

from typing import Any
from .actions import KEYS
from .settings import Settings
from .resources import WORKSPACE_FRAME_PATH


def environment_description(task: dict[str, Any], settings: Settings, *,
                            latest_frame_path: str = WORKSPACE_FRAME_PATH) -> str:
    respawn = ("Hardcore mode: death counts as task failure; respawning is unavailable."
               if settings.hardcore else "Death does not fail the task. You can respawn and continue.")
    return f'''You are playing Minecraft Java Edition in real time, in survival mode.
Difficulty: {settings.difficulty}.

Task:
{task["instruction"]}

Your current game view is available at {latest_frame_path}. The file is
updated with each captured frame, approximately {settings.capture_fps:g}
times per second. Reopen the image to see the latest frame.

Image coordinates are {settings.width} by {settings.height},
with (0,0) at the top left.

Complete the task within {settings.minecraft_task_timeout_seconds:g} seconds.
Do not leave the server or restart the game. Either action counts as task failure.
{respawn}

Send actions as JSON objects. Responses are small confirmation files; continue reading
{latest_frame_path} to observe results. Supported keys: {", ".join(sorted(KEYS))}.
Mouse buttons: left, middle, right.

- {{"id":"key_down","key":"w"}}: hold W until key_up, a timed press on W,
  release_all, or session termination. It remains held while you think.
- {{"id":"key_up","key":"w"}}: release W; other held controls are unchanged.
- {{"id":"key_press","key":"w","duration_ms":250}}: press W now and release it
  automatically after 250 milliseconds. duration_ms is 1..10000, default 250.
  This replaces any previous hold/timer on W. A later key_down/up overrides its timer.
- {{"id":"mouse_move","dx":12,"dy":-5}}: immediately move relatively, for looking
  around. Each delta is -4096..4096; an omitted relative axis defaults to zero.
- {{"id":"mouse_move","x":400,"y":250}}: immediately move to this image coordinate,
  for inventory and menus. Supply both x/y; do not mix with dx/dy.
- {{"id":"mouse_down","button":"left"}}: hold left mouse, for example to mine.
- {{"id":"mouse_up","button":"left"}}: release left mouse.
- {{"id":"mouse_click","button":"left","x":400,"y":250}}: move to the coordinate,
  press, and release after a brief click. Omit both x/y to click at the current position.
  A click replaces any existing hold on that button. Other controls are unchanged.
- {{"id":"mouse_scroll","amount":1}}: scroll up one notch; negative scrolls down,
  allowed range -10..10.
- {{"id":"release_all"}}: release all held keys and mouse buttons.

Actions return immediately, without waiting for timed presses to finish.
Mouse movement does not release held keys or buttons.
'''
