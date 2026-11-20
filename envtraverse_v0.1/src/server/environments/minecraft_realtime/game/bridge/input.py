"""Bounded input leases; no game-state queries or autonomous gameplay."""
import threading
import time

KEYS = {**{k: k for k in 'wasdeqf123456789'}, 'space': 'space',
        'shift': 'Shift_L', 'ctrl': 'Control_L', 'tab': 'Tab', 'escape': 'Escape'}
BUTTONS = {'left': 1, 'middle': 2, 'right': 3}


class X11Input:
    def __init__(self):
        from Xlib import X, XK, display
        from Xlib.ext import xtest
        self.display = display.Display()
        self.X, self.XK, self.xtest = X, XK, xtest

    def key(self, key, down):
        code = self.display.keysym_to_keycode(self.XK.string_to_keysym(KEYS[key]))
        self.xtest.fake_input(self.display, self.X.KeyPress if down else self.X.KeyRelease, code)

    def button(self, button, down):
        self.xtest.fake_input(self.display, self.X.ButtonPress if down else self.X.ButtonRelease, button)

    def motion(self, x, y, relative):
        self.xtest.fake_input(self.display, self.X.MotionNotify, detail=int(relative), x=x, y=y)

    def flush(self):
        self.display.sync()

    def close(self):
        self.display.close()


class InputLease:
    def __init__(self, driver, clock=time.monotonic):
        self.driver, self.clock = driver, clock
        self.keys, self.buttons = set(), set()
        self.deadline = 0.0
        self.lock = threading.Lock()

    def _replace(self, keys, buttons):
        for key in sorted(self.keys - keys): self.driver.key(key, False)
        for button in sorted(self.buttons - buttons): self.driver.button(BUTTONS[button], False)
        for key in sorted(keys - self.keys): self.driver.key(key, True)
        for button in sorted(buttons - self.buttons): self.driver.button(BUTTONS[button], True)
        self.keys, self.buttons = keys, buttons

    def apply(self, action):
        with self.lock:
            if action.x is not None:
                self.driver.motion(action.x, action.y, False)
            elif action.dx or action.dy:
                self.driver.motion(action.dx, action.dy, True)
            self._replace(set(action.keys), set(action.buttons))
            for _ in range(abs(action.wheel)):
                button = 4 if action.wheel > 0 else 5
                self.driver.button(button, True)
                self.driver.button(button, False)
            self.driver.flush()
            self.deadline = self.clock() + action.duration_ms / 1000

    def release(self):
        with self.lock:
            self._replace(set(), set())
            self.driver.flush()
            self.deadline = 0

    def expire(self):
        with self.lock:
            if self.deadline and self.clock() >= self.deadline:
                self._replace(set(), set())
                self.driver.flush()
                self.deadline = 0
