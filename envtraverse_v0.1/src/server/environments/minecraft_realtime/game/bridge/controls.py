"""Stateful controls with independent timers for deliberate short presses."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time

from .actions import validate_action
from .input import BUTTONS


class InputEvents:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.error = None

    def __call__(self, event):
        if self.path is None:
            return
        try:
            # The private bind directory is provisioned by the host runtime.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(json.dumps(event, allow_nan=False) + '\n')
        except OSError as exc:
            self.error = str(exc)
            logging.exception('Could not record applied input')


class InputController:
    def __init__(self, driver, *, width=960, height=540, clock=time.monotonic, events=None):
        self.driver, self.width, self.height, self.clock = driver, width, height, clock
        self.events = events or (lambda event: None)
        self.lock = threading.Lock()
        self.keys, self.buttons = set(), set()
        self.deadlines = {}
        self.sequence = 0

    def _record(self, kind, **data):
        self.sequence += 1
        event = {'seq': self.sequence, 'time': time.time(), 'monotonic': self.clock(),
                 'type': kind, 'data': data}
        self.events(event)
        return self.sequence

    def _key(self, key, down):
        if (key in self.keys) != down:
            self.driver.key(key, down)
            (self.keys.add if down else self.keys.discard)(key)

    def _button(self, button, down):
        if (button in self.buttons) != down:
            self.driver.button(BUTTONS[button], down)
            (self.buttons.add if down else self.buttons.discard)(button)

    def _release(self):
        for key in sorted(self.keys): self._key(key, False)
        for button in sorted(self.buttons): self._button(button, False)
        self.deadlines.clear()

    def _motion(self, action):
        if 'x' in action:
            self.driver.motion(action['x'], action['y'], False)
        elif action.get('dx', 0) or action.get('dy', 0):
            self.driver.motion(action.get('dx', 0), action.get('dy', 0), True)

    def execute(self, raw):
        action = validate_action(raw, width=self.width, height=self.height)
        kind = action['id']
        with self.lock:
            if kind in ('key_down', 'key_up', 'key_press'):
                key = action['key']
                self.deadlines.pop(('key', key), None)
                if kind == 'key_press':
                    self._key(key, False)
                self._key(key, kind != 'key_up')
                if kind == 'key_press':
                    self.deadlines['key', key] = self.clock() + action['duration_ms'] / 1000
            elif kind in ('mouse_down', 'mouse_up', 'mouse_click'):
                button = action['button']
                self.deadlines.pop(('button', button), None)
                if kind == 'mouse_click':
                    # End any existing drag before positioning the new click.
                    self._button(button, False)
                    self._motion(action)
                self._button(button, kind != 'mouse_up')
                if kind == 'mouse_click':
                    self.deadlines['button', button] = self.clock() + 0.05
            elif kind == 'mouse_move':
                self._motion(action)
            elif kind == 'mouse_scroll':
                for _ in range(abs(action['amount'])):
                    button = 4 if action['amount'] > 0 else 5
                    self.driver.button(button, True)
                    self.driver.button(button, False)
            elif kind == 'release_all':
                self._release()
            self.driver.flush()
            seq = self._record('action_applied', action=action,
                               held_keys=sorted(self.keys), held_buttons=sorted(self.buttons))
            return {'accepted': True, 'action_id': seq, 'time': time.time()}

    def apply(self, action):
        """Legacy full-state input for the human viewer; shares the same state."""
        with self.lock:
            if action.x is not None:
                self.driver.motion(action.x, action.y, False)
            elif action.dx or action.dy:
                self.driver.motion(action.dx, action.dy, True)
            desired_keys, desired_buttons = set(action.keys), set(action.buttons)
            for key in sorted(self.keys | desired_keys): self._key(key, key in desired_keys)
            for button in sorted(self.buttons | desired_buttons): self._button(button, button in desired_buttons)
            deadline = self.clock() + action.duration_ms / 1000
            self.deadlines = {('key', key): deadline for key in self.keys}
            self.deadlines.update({('button', button): deadline for button in self.buttons})
            for _ in range(abs(action.wheel)):
                button = 4 if action.wheel > 0 else 5
                self.driver.button(button, True)
                self.driver.button(button, False)
            self.driver.flush()
            self._record('legacy_input_applied', action=action.model_dump(),
                         held_keys=sorted(self.keys), held_buttons=sorted(self.buttons))

    def release(self):
        with self.lock:
            self._release()
            self.driver.flush()
            self._record('release_all')

    def expire(self):
        with self.lock:
            expired = [(kind, name) for (kind, name), deadline in self.deadlines.items()
                       if self.clock() >= deadline]
            if not expired:
                return
            for kind, name in expired:
                (self._key if kind == 'key' else self._button)(name, False)
                del self.deadlines[kind, name]
            self.driver.flush()
            self._record('automatic_release', controls=[{'kind': k, 'name': n} for k, n in expired],
                         held_keys=sorted(self.keys), held_buttons=sorted(self.buttons))
