#!/usr/bin/env python3
"""Replay a maze from a JSON action file in an interactive terminal player."""
from __future__ import annotations

import argparse
from contextlib import closing
import curses
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

if __package__:
    from . import replay_single
else:
    import replay_single


@dataclass(frozen=True)
class ReplayFrame:
    index: int
    label: str
    action: str | None
    text: str
    status: str | None
    observe: str | None
    events: list[Any]
    inventory: Any
    done: bool | None
    replay_map: str


@dataclass(frozen=True)
class ReplayMetadata:
    json_path: Path
    name_key: str
    labyrinth_name: str
    action_count: int
    base_url: str


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _frame_from_step(step: replay_single.ReplayStep) -> ReplayFrame:
    response = step.response
    events = replay_single.response_field(response, "events")
    replay_map = response.get("map")
    return ReplayFrame(
        index=step.index, label=step.label, action=step.action,
        text=str(response.get("text", "")),
        status=_optional_str(replay_single.response_field(response, "status")),
        observe=_optional_str(replay_single.response_field(response, "observe")),
        events=events if isinstance(events, list) else [],
        inventory=response.get("inventory"),
        done=bool(response["done"]) if "done" in response else None,
        replay_map=replay_map.rstrip("\n") if isinstance(replay_map, str) else "",
    )


def collect_replay(args: argparse.Namespace) -> tuple[ReplayMetadata, list[ReplayFrame]]:
    saved = replay_single.load_saved_labyrinth(args.json_file, args.maze_name)
    metadata = ReplayMetadata(
        saved.json_path, saved.name_key, saved.labyrinth_name, len(saved.actions), f"http://{args.host}:{args.port}",
    )
    with closing(replay_single.replay_steps(
        saved, host=args.host, port=args.port, lang=args.lang, timeout=args.timeout,
    )) as steps:
        frames = [_frame_from_step(step) for step in steps]
    return metadata, frames


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "."


def _add_line(stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> int:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return y + 1
    try:
        stdscr.addstr(y, x, _truncate(text, width - x), attr)
    except curses.error:
        pass
    return y + 1


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


def _draw_map(stdscr: curses.window, y: int, x: int, replay_map: str, previous_map: str) -> int:
    lines = replay_map.splitlines()
    previous_lines = previous_map.splitlines()
    height, width = stdscr.getmaxyx()
    max_lines = max(0, height - y - 2)
    for row, line in enumerate(lines[:max_lines]):
        screen_y = y + row
        if screen_y >= height - 1:
            break
        previous_line = previous_lines[row] if row < len(previous_lines) else ""
        for col, ch in enumerate(line[:max(0, width - x)]):
            attr = _map_attr(ch)
            if col >= len(previous_line) or previous_line[col] != ch:
                attr |= curses.A_BOLD
            try:
                stdscr.addstr(screen_y, x + col, ch, attr)
            except curses.error:
                pass
    return y + min(len(lines), max_lines)


def _init_curses() -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    for pair, color in enumerate((curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                                  curses.COLOR_WHITE, curses.COLOR_MAGENTA), start=1):
        curses.init_pair(pair, color, -1)


def _wrap_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw(
    stdscr: curses.window, metadata: ReplayMetadata, frames: list[ReplayFrame],
    frame_index: int, playing: bool, finished_once: bool,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 14 or width < 40:
        _add_line(stdscr, 0, 0, "Terminal too small. Resize or press q.", curses.A_BOLD)
        stdscr.refresh()
        return
    frame = frames[frame_index]
    previous_map = frames[frame_index - 1].replay_map if frame_index > 0 else ""
    status = "playing" if playing else "review" if finished_once else "paused"
    y = _add_line(stdscr, 0, 0,
                  f"replay_single_terminal.py  {status}  frame {frame_index + 1}/{len(frames)}", curses.A_REVERSE)
    y = _add_line(stdscr, y, 0, f"{metadata.name_key}={metadata.labyrinth_name}", curses.A_BOLD)
    y = _add_line(stdscr, y, 0,
                  f"actions={metadata.action_count} source={metadata.json_path.name} server={metadata.base_url}")
    action = "start" if frame.action is None else f"{frame.label} seq={frame.index} action={frame.action}"
    y = _add_line(stdscr, y, 0, action, curses.color_pair(1) | curses.A_BOLD)

    state_bits = []
    for name in ("status", "observe", "done"):
        value = getattr(frame, name)
        if value is not None:
            state_bits.append(f"{name}={value}")
    y = _add_line(stdscr, y, 0, " ".join(state_bits))
    y = _add_line(stdscr, y, 0, f"inventory={replay_single.format_json_compact(frame.inventory)}")
    events_text = replay_single.format_json_compact(frame.events) if frame.events else "-"
    y = _add_line(stdscr, y, 0, f"events={events_text}", curses.color_pair(2) if frame.events else 0)
    text_lines = _wrap_text(f"text: {frame.text}", max(10, width))
    for index in range(2):
        line = text_lines[index] if index < len(text_lines) else ""
        if index == 1 and len(text_lines) > 2:
            line = _truncate(line, max(0, width - 4)) + " ..."
        y = _add_line(stdscr, y, 0, line)
    while y < 10:
        y = _add_line(stdscr, y, 0, "")
    _draw_map(stdscr, y, 0, frame.replay_map, previous_map)
    _add_line(stdscr, height - 1, 0,
              "left/right step  up start  down end  space play/pause  q quit", curses.A_REVERSE)
    stdscr.refresh()


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
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            playing = False
            stdscr.timeout(-1)
            if key == curses.KEY_LEFT:
                frame_index = max(0, frame_index - 1)
            elif key == curses.KEY_RIGHT:
                frame_index = min(len(frames) - 1, frame_index + 1)
            elif key == curses.KEY_UP:
                frame_index = 0
            else:
                frame_index = len(frames) - 1
        if playing and key == -1:
            if frame_index < len(frames) - 1:
                frame_index += 1
            else:
                playing = False
                finished_once = True
                stdscr.timeout(-1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    replay_single.add_replay_arguments(parser)
    parser.add_argument("--delay", type=replay_single.positive_seconds, default=0.15,
                        help="Autoplay delay between frames in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        print("Collecting replay frames...", file=sys.stderr)
        metadata, frames = collect_replay(args)
        curses.wrapper(run_player, metadata, frames, args.delay)
    except KeyboardInterrupt:
        return 130
    except (OSError, UnicodeError, ValueError, RuntimeError, curses.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
