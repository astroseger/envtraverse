import asyncio
import hmac
import io
import json
import logging
import os
from pathlib import Path
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .input import BUTTONS, KEYS, X11Input
from .controls import InputController, InputEvents

WIDTH, HEIGHT = int(os.getenv('WIDTH', '960')), int(os.getenv('HEIGHT', '540'))
FPS = float(os.getenv('CAPTURE_FPS', '10'))
if not 1 <= FPS <= 60: raise ValueError('CAPTURE_FPS must be in [1,60]')
if not 320 <= WIDTH <= 3840 or not 240 <= HEIGHT <= 2160:
    raise ValueError('unsupported capture resolution')


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keys: list[str] = Field(default_factory=list, max_length=len(KEYS))
    buttons: list[str] = Field(default_factory=list, max_length=3)
    dx: int = Field(default=0, ge=-4096, le=4096)
    dy: int = Field(default=0, ge=-4096, le=4096)
    x: int | None = Field(default=None, ge=0, lt=WIDTH)
    y: int | None = Field(default=None, ge=0, lt=HEIGHT)
    wheel: int = Field(default=0, ge=-10, le=10)
    duration_ms: int = Field(default=250, ge=1, le=1000)

    @model_validator(mode='after')
    def valid_inputs(self):
        if set(self.keys) - KEYS.keys() or set(self.buttons) - BUTTONS.keys():
            raise ValueError('unsupported key or mouse button')
        if (self.x is None) != (self.y is None):
            raise ValueError('x and y must be supplied together')
        if self.x is not None and (self.dx or self.dy):
            raise ValueError('choose absolute or relative motion')
        return self


class Capture:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.frame = None
        self.sequence = 0
        self.time = 0.0
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        import mss
        from PIL import Image
        try:
            # Composite the real X11 pointer into the canonical observation.
            # GLFW supplies a transparent cursor while the camera is grabbed.
            with mss.mss(with_cursor=True) as screen:
                if not screen.with_cursor:
                    raise RuntimeError('Cursor capture requires the XFixes library')
                while not self.stop.is_set():
                    started = time.monotonic()
                    shot = screen.grab({'left': 0, 'top': 0, 'width': WIDTH, 'height': HEIGHT})
                    buffer = io.BytesIO()
                    Image.frombytes('RGB', shot.size, shot.rgb).save(buffer, format='JPEG', quality=85)
                    with self.lock:
                        self.frame = buffer.getvalue()
                        self.sequence += 1
                        self.time = time.time()
                    self.stop.wait(max(0, 1/FPS - (time.monotonic()-started)))
        except Exception as error:
            self.error = str(error)
            logging.exception('Capture stopped')

    def latest(self):
        with self.lock:
            return self.frame, self.sequence, self.time


capture = Capture()
lease = None


@asynccontextmanager
async def lifespan(app):
    global lease
    lease = InputController(X11Input(), width=WIDTH, height=HEIGHT,
                            events=InputEvents(os.getenv('INPUT_EVENTS_PATH')))
    capture.thread.start()
    async def watchdog():
        while True:
            lease.expire()
            await asyncio.sleep(0.02)
    task = asyncio.create_task(watchdog())
    try:
        yield
    finally:
        task.cancel()
        capture.stop.set()
        capture.thread.join(timeout=2)
        lease.release()
        lease.driver.close()


def authorize(authorization: str = Header(default='')):
    token = os.getenv('BRIDGE_TOKEN', '')
    if token and not hmac.compare_digest(authorization, 'Bearer ' + token):
        raise HTTPException(401, 'Invalid bridge token')


app = FastAPI(title='Minecraft pixels and controls', lifespan=lifespan,
              dependencies=[Depends(authorize)])


@app.get('/health')
def health():
    _, sequence, timestamp = capture.latest()
    try:
        client = json.loads(Path('/run/client/status.json').read_text())
    except (OSError, ValueError):
        client = {'state': 'unknown'}
    return {'capture_ready': sequence > 0 and time.time()-timestamp < 3,
            'width': WIDTH, 'height': HEIGHT, 'capture_fps': FPS,
            'frame_sequence': sequence, 'frame_time': timestamp,
            'capture_error': capture.error, 'client': client,
            'input_log_error': getattr(getattr(lease, 'events', None), 'error', None)}


def current_frame():
    frame, sequence, timestamp = capture.latest()
    if frame is None or time.time()-timestamp > 3:
        raise HTTPException(503, 'No fresh frame available')
    return frame, sequence, timestamp


@app.get('/frame.jpg')
def frame():
    data, sequence, timestamp = current_frame()
    return Response(data, media_type='image/jpeg', headers={
        'Cache-Control': 'no-store', 'X-Frame-Sequence': str(sequence), 'X-Frame-Time': str(timestamp)})


@app.get('/stream.mjpg')
def stream():
    async def frames():
        previous = -1
        while True:
            data, seq, _ = capture.latest()
            if data is not None and seq != previous:
                previous = seq
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + data + b'\r\n'
            await asyncio.sleep(1/FPS)
    return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame')


@app.post('/input')
def apply(action: Action):
    if lease is None: raise HTTPException(503, 'Input is not ready')
    lease.apply(action)
    return {'accepted': True, 'lease_ms': action.duration_ms, 'time': time.time()}


@app.post('/release')
def release():
    if lease: lease.release()
    return {'released': True}


@app.post('/action')
def explicit_action(action: dict):
    if lease is None:
        raise HTTPException(503, 'Input is not ready')
    try:
        return lease.execute(action)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
