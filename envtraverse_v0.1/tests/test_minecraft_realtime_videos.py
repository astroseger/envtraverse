"""Render tiny recordings through the standalone CLI and inspect actual MP4s."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "analysis_scripts/minecraft_realtime/make_videos_v1.py"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required")
SESSION = "a" * 32
WIDTH, HEIGHT = 64, 48


@pytest.fixture(scope="module")
def jpeg_colors():
    images = []
    for rgb in ((240, 10, 10), (10, 240, 10), (10, 10, 240)):
        result = subprocess.run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-i", "pipe:0", "-frames:v", "1", "-c:v", "mjpeg",
            "-threads", "1", "-f", "image2pipe", "pipe:1",
        ], input=bytes(rgb) * WIDTH * HEIGHT, capture_output=True, check=True, timeout=10)
        images.append(result.stdout)
    return images


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "run root with spaces"
    (directory / "run_mc/sessions").mkdir(parents=True)
    return directory


def session(root, images, *, session_id=SESSION, task="kill_zombie", manifest=True,
            sequences=(10, 40, 70), names=None):
    directory = root / "run_mc/sessions" / session_id
    frames = directory / "frames"
    frames.mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps({"session_id": session_id, "task_id": task,
                                                       "settings": {"record_fps": 1}}))
    records = []
    for index, (sequence, jpeg) in enumerate(zip(sequences, images)):
        name = names[index] if names else f"{sequence:012d}.jpg"
        (frames / name).write_bytes(jpeg)
        records.append({"sequence": sequence, "file": "frames/" + name,
                        "captured_at": 1700000000 + index, "published_at": 1700000000 + index + .01})
    if manifest:
        (directory / "frames.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    return directory


def run(root, *arguments):
    return subprocess.run([sys.executable, str(SCRIPT), str(root), *map(str, arguments)],
                          capture_output=True, text=True, timeout=30, cwd=root.parent)


def video(root, task="kill_zombie", session_id=SESSION):
    return root / "run_mc/videos" / f"{task}_{session_id}.mp4"


def probe(path):
    result = subprocess.run([
        FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=codec_name,pix_fmt,nb_frames,r_frame_rate,duration", "-of", "json", str(path),
    ], capture_output=True, text=True, check=True, timeout=10)
    return json.loads(result.stdout)["streams"][0]


def assert_video(path, frames, fps):
    stream = probe(path)
    assert stream["codec_name"] == "h264"
    assert stream["pix_fmt"] == "yuv420p"
    assert int(stream["nb_frames"]) == frames
    numerator, denominator = map(int, stream["r_frame_rate"].split("/"))
    assert numerator / denominator == pytest.approx(fps)
    assert float(stream["duration"]) == pytest.approx(frames / fps, abs=0.002)


def test_default_30_fps_preserves_one_frame_per_record_despite_sequence_gaps(root, jpeg_colors):
    session(root, jpeg_colors)
    result = run(root)
    assert result.returncode == 0, result.stderr
    assert_video(video(root), 3, 30)
    assert len(list((root / "run_mc/videos").glob("*.mp4"))) == 1


@pytest.mark.parametrize("fps", [1, 12.5])
def test_configurable_fps_and_output_directory(root, jpeg_colors, fps):
    session(root, jpeg_colors, task="kill_ghast")
    output = root.parent / "custom video output"
    result = run(root, "--fps", fps, "--output-dir", output)
    assert result.returncode == 0, result.stderr
    assert_video(output / f"kill_ghast_{SESSION}.mp4", 3, fps)


@pytest.mark.parametrize("manifest,expected_order", [(False, [0, 1, 2]), (True, [2, 0, 1])])
def test_frame_order_uses_manifest_or_numeric_fallback(root, jpeg_colors, manifest, expected_order):
    session(root, [jpeg_colors[2], jpeg_colors[0], jpeg_colors[1]], manifest=manifest,
            sequences=(1, 2, 3), names=("10.jpg", "1.jpg", "2.jpg"))
    result = run(root)
    assert result.returncode == 0, result.stderr
    path = video(root)
    assert_video(path, 3, 30)
    decoded = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "pipe:1"], capture_output=True, check=True, timeout=10).stdout
    frame_size = WIDTH * HEIGHT * 3
    assert len(decoded) == 3 * frame_size
    dominant_colors = []
    for offset in range(0, len(decoded), frame_size):
        frame = decoded[offset:offset + frame_size]
        channels = [sum(frame[channel::3]) for channel in range(3)]
        dominant_colors.append(channels.index(max(channels)))
    assert dominant_colors == expected_order


def test_existing_output_skipped_unless_overwrite_requested(root, jpeg_colors):
    session(root, jpeg_colors)
    path = video(root)
    path.parent.mkdir()
    path.write_bytes(b"previous output")
    result = run(root)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == b"previous output"
    result = run(root, "--overwrite", "--fps", 5)
    assert result.returncode == 0, result.stderr
    assert_video(path, 3, 5)


def test_failed_overwrite_preserves_previous_video(root, jpeg_colors):
    # Keep JPEG markers so decoder errors, not just filename/header checks, are exercised.
    session(root, [jpeg_colors[0], b"\xff\xd8invalid JPEG payload\xff\xd9", jpeg_colors[2]])
    path = video(root)
    path.parent.mkdir()
    path.write_bytes(b"previous output")
    result = run(root, "--overwrite")
    assert result.returncode != 0
    assert path.read_bytes() == b"previous output"
    assert list(path.parent.iterdir()) == [path]


def test_missing_manifest_frame_fails_session_but_other_session_renders(root, jpeg_colors):
    missing = session(root, jpeg_colors)
    (missing / "frames/000000000040.jpg").unlink()
    other_id = "b" * 32
    session(root, jpeg_colors, session_id=other_id, task="kill_spider")
    result = run(root)
    assert result.returncode != 0
    assert not video(root).exists()
    assert_video(video(root, "kill_spider", other_id), 3, 30)


def test_no_recorded_frames_returns_failure(root):
    session(root, [])
    result = run(root)
    assert result.returncode != 0
    assert not list(root.rglob("*.mp4"))


def test_empty_session_is_skipped_when_another_session_has_frames(root, jpeg_colors):
    session(root, [])
    other_id = "b" * 32
    session(root, jpeg_colors, session_id=other_id, task="kill_spider")
    result = run(root)
    assert result.returncode == 0, result.stderr
    assert not video(root).exists()
    assert_video(video(root, "kill_spider", other_id), 3, 30)


def test_manifest_cannot_read_outside_own_frames_directory(root, jpeg_colors):
    directory = session(root, [])
    (directory / "outside.jpg").write_bytes(jpeg_colors[0])
    (directory / "frames.jsonl").write_text(json.dumps({"sequence": 1, "file": "outside.jpg"}) + "\n")
    result = run(root)
    assert result.returncode != 0
    assert not video(root).exists()


@pytest.mark.parametrize("fps", ["0", "-1", "nan", "inf", "not-a-number"])
def test_invalid_fps_rejected(root, fps):
    result = run(root, "--fps", fps)
    assert result.returncode != 0
    assert "fps" in result.stderr.lower()
    assert not list(root.rglob("*.mp4"))
