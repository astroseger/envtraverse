"""Public task instructions and real-time action semantics."""
from __future__ import annotations

from typing import Any
from .actions import KEYS
from .settings import Settings


def environment_description(task: dict[str, Any], settings: Settings) -> str:
    respawn = ("This is hardcore: death fails the task and normal respawning is unavailable."
               if settings.hardcore else "Death does not fail this task. Use Minecraft's Respawn button to continue.")
    return f'''You are playing Minecraft Java Edition in real time, in survival mode.
Difficulty: {settings.difficulty}. Each task starts in a fresh world.

Task:
{task["instruction"]}

Read your current view from /home/user/run/agent_run/latest_frame.jpg (latest_frame.jpg
relative to your initial working directory). The image is atomically replaced at a
target of {settings.capture_fps:g} FPS. Reopen the file for a new image; the image includes
the game's actual cursor. Image coordinates are {settings.width} by {settings.height},
with (0,0) at the top left. Minecraft continues running while you read, think, and
between actions; reading a frame does not pause or advance a discrete game step.

minecraft_task_timeout: {settings.minecraft_task_timeout:g} seconds of wall-clock time,
starting when the world and first image are ready. It includes your thinking time.
When this deadline expires the task irreversibly fails. Minecraft and image updates
continue, and your next action returns terminal status without applying that action.
Success is detected automatically and likewise reported on your next action.
{respawn}
Leaving the server or restarting the game invalidates the attempt. Do not disconnect.

Send actions as JSON objects. Responses are small confirmation files; continue reading
latest_frame.jpg to observe results. Supported keys: {", ".join(sorted(KEYS))}.
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

Mouse movement does not release held keys. Actions acknowledge immediately instead of
waiting for a hold to finish. Only key_press and mouse_click release automatically;
explicit key_down and mouse_down holds have no short automatic timeout. Use release_all
when you need to stop. Use E to open/close inventory and absolute mouse input to use it.
The first relative movement after the game grabs the pointer may be ignored by Minecraft.
'''
