#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
from dataclasses import dataclass
import json
import sys
from pathlib import Path
from typing import Any

if __package__:
    from . import replay_single
else:
    import replay_single


@dataclass(frozen=True)
class ReplayFrame:
    index: int
    label: str
    action_index: int | None
    action: str | None
    seq: int | None
    text: str
    status: str | None
    observe: str | None
    events: list[Any]
    inventory: Any
    done: bool | None
    replay_map: str


@dataclass(frozen=True)
class ReplayMetadata:
    run_folder: Path
    actions_path: Path
    results_path: Path
    game_name: str
    labyrinth_index: int
    labyrinth_name: str
    action_count: int
    base_url: str


def _response_field(response: dict[str, Any], name: str) -> Any:
    value = response.get(name)
    if value is not None:
        return value
    formal = response.get("formal")
    if isinstance(formal, dict):
        return formal.get(name)
    return None


def _frame_from_response(
    *,
    index: int,
    label: str,
    response: dict[str, Any],
    action_index: int | None = None,
    action: str | None = None,
    seq: int | None = None,
) -> ReplayFrame:
    events = _response_field(response, "events")
    if not isinstance(events, list):
        events = []

    replay_map = response.get("map")
    if not isinstance(replay_map, str):
        replay_map = ""

    done = bool(response["done"]) if "done" in response else None

    return ReplayFrame(
        index=index,
        label=label,
        action_index=action_index,
        action=action,
        seq=seq,
        text=str(response.get("text", "")),
        status=_optional_str(_response_field(response, "status")),
        observe=_optional_str(_response_field(response, "observe")),
        events=events,
        inventory=response.get("inventory"),
        done=done,
        replay_map=replay_map.rstrip("\n"),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def collect_replay(args: argparse.Namespace) -> tuple[ReplayMetadata, list[ReplayFrame]]:
    run_folder = args.run_folder.expanduser().resolve()
    if not run_folder.is_dir():
        raise FileNotFoundError(f"Run folder does not exist: {run_folder}")
    if args.labyrinth_index < 0:
        raise ValueError("labyrinth_index must be non-negative")

    saved = replay_single.load_saved_labyrinth(run_folder, args.labyrinth_index)

    base_url = f"http://{args.host}:{args.port}"
    metadata = ReplayMetadata(
        run_folder=run_folder,
        actions_path=saved.actions_path,
        results_path=saved.results_path,
        game_name=saved.game_name,
        labyrinth_index=saved.labyrinth_index,
        labyrinth_name=saved.labyrinth_name,
        action_count=len(saved.actions),
        base_url=base_url,
    )

    process = None
    frames: list[ReplayFrame] = []
    try:
        process = replay_single.start_server(args.host, args.port, args.timeout)
        start = replay_single.http_post_json(
            f"{base_url}/api/start",
            {
                "path": saved.labyrinth_name,
                "lang": args.lang,
                "client": "api",
                "agent": "replay_single_terminal",
            },
        )
        if start.get("error"):
            raise RuntimeError(f"Failed to start labyrinth {saved.labyrinth_name!r}: {start}")
        session_id = start.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"Labyrinth start response did not include a session id: {start}")

        frames.append(_frame_from_response(index=0, label="start", response=start))
        seq = 1
        for action_index, action_payload in enumerate(saved.actions):
            move = replay_single.action_id(action_payload)
            out = replay_single.http_post_json(
                f"{base_url}/api/step",
                {"id": session_id, "action": move, "seq": seq},
            )
            if out.get("error"):
                raise RuntimeError(f"Replay failed at action_{action_index:04d}: {out}")

            frames.append(
                _frame_from_response(
                    index=len(frames),
                    label=f"action_{action_index:04d}",
                    response=out,
                    action_index=action_index,
                    action=move,
                    seq=seq,
                )
            )
            if out.get("done"):
                break
            seq += 1
    finally:
        if process is not None:
            replay_single.stop_server(process)

    return metadata, frames


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "."


def _add_line(stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> int:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return y + 1
    try:
        stdscr.addstr(y, x, _truncate(text, width - x), attr)
    except curses.error:
        pass
    return y + 1


def _draw_map(
    stdscr: curses.window,
    y: int,
    x: int,
    replay_map: str,
    previous_map: str,
) -> int:
    lines = replay_map.splitlines()
    previous_lines = previous_map.splitlines()
    height, width = stdscr.getmaxyx()
    max_lines = max(0, height - y - 2)

    for row, line in enumerate(lines[:max_lines]):
        screen_y = y + row
        if screen_y >= height - 1:
            break
        previous_line = previous_lines[row] if row < len(previous_lines) else ""
        for col, ch in enumerate(line[: max(0, width - x)]):
            attr = _map_attr(ch)
            if col >= len(previous_line) or previous_line[col] != ch:
                attr |= curses.A_BOLD
            try:
                stdscr.addstr(screen_y, x + col, ch, attr)
            except curses.error:
                pass
    return y + min(len(lines), max_lines)


def _map_attr(ch: str) -> int:
    if ch == "S":
        return curses.color_pair(1) | curses.A_BOLD
    if ch == "T":
        return curses.color_pair(2) | curses.A_BOLD
    if ch == "K":
        return curses.color_pair(3) | curses.A_BOLD
    if ch in "+-|":
        return curses.color_pair(4) | curses.A_DIM
    if ch.strip():
        return curses.color_pair(5)
    return curses.A_NORMAL


def _init_curses() -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.start_color()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_WHITE, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)


def _draw(
    stdscr: curses.window,
    metadata: ReplayMetadata,
    frames: list[ReplayFrame],
    frame_index: int,
    playing: bool,
    finished_once: bool,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 14 or width < 40:
        _add_line(stdscr, 0, 0, "Terminal too small. Resize or press q.", curses.A_BOLD)
        stdscr.refresh()
        return

    frame = frames[frame_index]
    previous_map = frames[frame_index - 1].replay_map if frame_index > 0 else ""

    status = "playing" if playing else "paused"
    if finished_once and not playing:
        status = "review"

    y = 0
    y = _add_line(
        stdscr,
        y,
        0,
        f"replay_single_terminal.py  {status}  frame {frame_index + 1}/{len(frames)}",
        curses.A_REVERSE,
    )
    y = _add_line(
        stdscr,
        y,
        0,
        (
            f"game={metadata.game_name} labyrinth_index={metadata.labyrinth_index} "
            f"path={metadata.labyrinth_name}"
        ),
        curses.A_BOLD,
    )
    y = _add_line(
        stdscr,
        y,
        0,
        (
            f"actions={metadata.action_count} "
            f"sources={metadata.actions_path.name},{metadata.results_path.name} "
            f"server={metadata.base_url}"
        ),
    )

    action = "start"
    if frame.action is not None:
        action = f"{frame.label} seq={frame.seq} action={frame.action}"
    y = _add_line(stdscr, y, 0, action, curses.color_pair(1) | curses.A_BOLD)

    state_bits = []
    if frame.status is not None:
        state_bits.append(f"status={frame.status}")
    if frame.observe is not None:
        state_bits.append(f"observe={frame.observe}")
    if frame.done is not None:
        state_bits.append(f"done={frame.done}")
    y = _add_line(stdscr, y, 0, " ".join(state_bits))
    y = _add_line(stdscr, y, 0, f"inventory={_json_compact(frame.inventory)}")
    events_text = _json_compact(frame.events) if frame.events else "-"
    events_attr = curses.color_pair(2) if frame.events else 0
    y = _add_line(stdscr, y, 0, f"events={events_text}", events_attr)

    text_lines = _wrap_text(f"text: {frame.text}", max(10, width))
    for text_line_index in range(2):
        line = text_lines[text_line_index] if text_line_index < len(text_lines) else ""
        if text_line_index == 1 and len(text_lines) > 2:
            suffix = " ..."
            line = _truncate(line, max(0, width - len(suffix))) + suffix
        y = _add_line(stdscr, y, 0, line)

    while y < 10:
        y = _add_line(stdscr, y, 0, "")
    y = _draw_map(stdscr, y, 0, frame.replay_map, previous_map)

    footer = "left/right step  up start  down end  space play/pause  q quit"
    _add_line(stdscr, height - 1, 0, footer, curses.A_REVERSE)
    stdscr.refresh()


def _wrap_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def run_player(stdscr: curses.window, metadata: ReplayMetadata, frames: list[ReplayFrame], delay: float) -> None:
    _init_curses()
    stdscr.keypad(True)
    stdscr.timeout(max(1, int(delay * 1000)))

    frame_index = 0
    playing = True
    finished_once = False

    while True:
        _draw(stdscr, metadata, frames, frame_index, playing, finished_once)
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            return
        if key == ord(" "):
            playing = not playing
            stdscr.timeout(max(1, int(delay * 1000)) if playing else -1)
        elif key == curses.KEY_LEFT:
            playing = False
            stdscr.timeout(-1)
            frame_index = max(0, frame_index - 1)
        elif key == curses.KEY_RIGHT:
            playing = False
            stdscr.timeout(-1)
            frame_index = min(len(frames) - 1, frame_index + 1)
        elif key == curses.KEY_UP:
            playing = False
            stdscr.timeout(-1)
            frame_index = 0
        elif key == curses.KEY_DOWN:
            playing = False
            stdscr.timeout(-1)
            frame_index = len(frames) - 1

        if playing and key == -1:
            if frame_index < len(frames) - 1:
                frame_index += 1
            else:
                playing = False
                finished_once = True
                stdscr.timeout(-1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one labyrinth selected by labyrinth_index from results.json "
            "using actions.json, and view it in an interactive terminal player. "
            "The replay is collected first, then played from start to end; after that "
            "use arrow keys to move through time."
        )
    )
    parser.add_argument(
        "run_folder",
        type=Path,
        help="Run folder containing actions.json and results.json",
    )
    parser.add_argument(
        "labyrinth_index",
        type=int,
        help="Zero-based labyrinth index, matching the saved labyrinth_index field.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--timeout", type=float, default=10.0, help="Server startup timeout in seconds")
    parser.add_argument("--delay", type=float, default=0.15, help="Autoplay delay between frames in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.delay <= 0:
        print("--delay must be positive", file=sys.stderr)
        return 1

    try:
        print("Collecting replay frames...", file=sys.stderr)
        metadata, frames = collect_replay(args)
        if not frames:
            print("Replay produced no frames", file=sys.stderr)
            return 1
        curses.wrapper(run_player, metadata, frames, args.delay)
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, ValueError, IndexError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
