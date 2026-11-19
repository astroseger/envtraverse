#!/usr/bin/env python3
"""Render one MP4 per Life Minecraft session from an experiment's saved JPEGs.

Usage: python3 make_videos_v1.py /path/to/experiment [--fps 30] [--overwrite]
Requires ffmpeg with libx264; Python uses only the standard library. Each saved
image becomes one video frame. Capture timestamps do not add pauses or duplicate
frames: recordings saved at 1 FPS play approximately 30 times faster at --fps 30.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def positive_fps(value: str) -> float:
    try:
        fps = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("FPS must be a positive finite number") from None
    if not math.isfinite(fps) or fps <= 0:
        raise argparse.ArgumentTypeError("FPS must be a positive finite number")
    return fps


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-") or "unknown"


def task_name(session: Path) -> str:
    metadata = session / "session.json"
    if not metadata.is_file():
        return "unknown"
    value = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {metadata}")
    task = value.get("task_id")
    return task if isinstance(task, str) and task.strip() else "unknown"


def recorded_frames(session: Path) -> list[Path]:
    """Use manifest order; older archives can fall back to numeric JPEG names."""
    directory = (session / "frames").resolve()
    manifest = session / "frames.jsonl"
    frames = []
    if manifest.is_file():
        seen = set()
        with manifest.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    filename = entry.get("file") if isinstance(entry, dict) else None
                    if not isinstance(filename, str) or not filename:
                        raise ValueError("expected a frame file path")
                    relative = Path(filename)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("frame path must stay inside this session's frames directory")
                    frame = (session / relative).resolve()
                    if not frame.is_relative_to(directory) or frame.suffix.lower() not in {".jpg", ".jpeg"}:
                        raise ValueError("frame path must refer to a JPEG inside this session's frames directory")
                    if not frame.is_file():
                        raise ValueError(f"missing recorded frame: {filename}")
                    if frame in seen:
                        raise ValueError(f"duplicate recorded frame: {filename}")
                    seen.add(frame)
                    frames.append(frame)
                except ValueError as error:
                    raise ValueError(f"{manifest}:{line_number}: {error}") from error
    elif directory.is_dir():
        frames = sorted((path for path in directory.iterdir()
                         if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}),
                        key=lambda path: (0, int(path.stem)) if path.stem.isdecimal() else (1, path.name))
    return frames


def render_video(frames: list[Path], output: Path, fps: float, ffmpeg: str, *, overwrite: bool) -> None:
    """Pipe non-contiguous JPEG sequences and publish only a finished video."""
    fd, name = tempfile.mkstemp(prefix="." + output.stem + ".", suffix=".mp4", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-xerror",
               "-progress", "pipe:1", "-nostats",
               "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "pipe:0",
               "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(temporary)]
    process = None
    try:
        # A file avoids stderr pipe deadlocks if an encoder reports many errors.
        with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as progress:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=progress, stderr=errors)
            try:
                try:
                    for frame in frames:
                        jpeg = frame.read_bytes()
                        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                            raise ValueError(f"Not a complete JPEG: {frame}")
                        process.stdin.write(jpeg)
                except BrokenPipeError:
                    pass  # Report ffmpeg's diagnostic below.
                finally:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                return_code = process.wait()
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
            if return_code:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4096))
                diagnostic = errors.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ffmpeg exited {return_code}: {diagnostic}")
            # The image parser can silently skip a malformed JPEG even with
            # -xerror. Require one encoded frame per recorded input image.
            progress.seek(0)
            counts = re.findall(rb"(?m)^frame=(\d+)[ \t]*$", progress.read())
            encoded = int(counts[-1]) if counts else None
            if encoded != len(frames):
                raise RuntimeError(f"ffmpeg encoded {encoded} frames; expected {len(frames)}. "
                                   "Recorded images may be damaged or unsupported.")
        if not temporary.stat().st_size:
            raise RuntimeError("ffmpeg produced an empty video")
        if overwrite:
            os.replace(temporary, output)
        else:
            # Publish exclusively: a concurrent run must not overwrite an output
            # created after our initial existence check.
            os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_root", type=Path, help="Experiment directory containing run_mc/")
    parser.add_argument("--fps", type=positive_fps, default=30.0, help="Output frames per second (default: 30)")
    parser.add_argument("--output-dir", type=Path, help="Video directory (default: RUN_ROOT/run_mc/videos)")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing videos after successful encoding")
    args = parser.parse_args(argv)
    root = args.run_root.expanduser().resolve()
    sessions = root / "run_mc/sessions"
    if not sessions.is_dir():
        parser.error(f"Minecraft session directory does not exist: {sessions}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        parser.error("ffmpeg is required; install ffmpeg with the libx264 encoder and retry")
    output_dir = (args.output_dir.expanduser().resolve() if args.output_dir else root / "run_mc/videos")
    rendered = skipped = empty = failed = 0
    try:
        for session in sorted(path for path in sessions.iterdir() if path.is_dir()):
            try:
                frames = recorded_frames(session)
                if not frames:
                    empty += 1
                    print(f"SKIP {session.name}: no recorded frames", flush=True)
                    continue
                output = output_dir / f"{safe_name(task_name(session))}_{safe_name(session.name)}.mp4"
                if output.exists() and not args.overwrite:
                    if not output.is_file():
                        raise ValueError(f"Output path exists and is not a file: {output}")
                    skipped += 1
                    print(f"SKIP existing video: {output} (use --overwrite to replace)", flush=True)
                    continue
                output_dir.mkdir(parents=True, exist_ok=True)
                print(f"RENDER {session.name}: {len(frames)} frames at {args.fps:g} FPS "
                      f"({len(frames) / args.fps:.2f}s) -> {output}", flush=True)
                render_video(frames, output, args.fps, ffmpeg, overwrite=args.overwrite)
                rendered += 1
                print(f"DONE {output}", flush=True)
            except (OSError, ValueError, RuntimeError) as error:
                failed += 1
                print(f"ERROR {session.name}: {error}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("Interrupted; completed videos have been kept.", file=sys.stderr)
        return 130
    print(f"Videos: {rendered} rendered, {skipped} already present, {empty} sessions without frames, {failed} failed.", flush=True)
    if not rendered and not skipped and not failed:
        print("No recorded frames found. Frame recording may have been disabled.", file=sys.stderr)
    return 1 if failed or not (rendered or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
