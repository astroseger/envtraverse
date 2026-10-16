"""ASCII/pseudographics rendering for quick visual inspection."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .generator import Board, Dir, PlayerState


def cell_glyph(features: List[object]) -> str:
    # precedence for display only
    # terrain first
    for f in features:
        if getattr(f, "name", None) == "river":
            kind = getattr(f, "kind", "body")
            flow = getattr(f, "flow", None)
            if kind == "mouth":
                return "m"
            # show direction arrow for debug
            if flow is not None:
                nm = getattr(flow, "name", "")
                return {"UP": "^", "DOWN": "v", "LEFT": "<", "RIGHT": ">"}.get(nm, "~")
            return "~"

    names = {getattr(f, "name", type(f).__name__) for f in features}

    # pits: show index within cycle; vary glyph family by cycle_id for readability.
    # This is for debugging: you can see where a pit will send you next.
    # pits: use ASCII digits for consistent monospace rendering (terminal + web).
    for f in features:
        if getattr(f, "name", None) == "pit":
            idx = int(getattr(f, "idx_in_cycle", 0))
            return str((idx % 9) + 1)

    if "treasure" in names:
        return "T"
    if "exit_key" in names:
        return "E"
    if "key" in names:
        return "K"
    if "grenade_pack" in names:
        return "G"
    if "boat" in names:
        return "B"
    return " "


def _exit_open_char(d: Dir) -> str:
    return {Dir.UP: " ", Dir.DOWN: " ", Dir.LEFT: " ", Dir.RIGHT: " "}[d]


def render(board: Board, start: PlayerState | None = None) -> str:
    """Render with walls BETWEEN cells.

    We use a classic ASCII maze layout:
    - corners are '+'
    - horizontal walls are '---'
    - vertical walls are '|'

    Renders monolith border (with one removed edge as exit) + internal walls.
    """

    n, m = board.n, board.m
    (er, ec), edir = board.exit_edge

    def has_wall_between(cell: tuple[int,int], d: Dir) -> bool:
        """Is there a wall on the edge from cell in direction d?

        Includes monolith border (except exit edge) and internal walls.
        """
        r, c = cell
        nc = (r + d.value[0], c + d.value[1])

        # monolith border
        if not (0 <= nc[0] < n and 0 <= nc[1] < m):
            if (r, c) == (er, ec) and d == edir:
                return False
            return True

        # internal wall
        return board.walls.is_blocked((r, c), nc)

    lines: List[str] = []

    # top border row
    top = ["+"]
    for c in range(m):
        top.append(("---" if has_wall_between((0, c), Dir.UP) else "   ") + "+")
    lines.append("".join(top))

    for r in range(n):
        # content row with vertical walls
        row = []
        for c in range(m):
            if c == 0:
                row.append("|" if has_wall_between((r, c), Dir.LEFT) else " ")

            pos = (r, c)
            ch = cell_glyph(board.features.get(pos, []))
            if start and pos == start.pos:
                ch = "S"
            row.append(f" {ch} ")

            row.append("|" if has_wall_between((r, c), Dir.RIGHT) else " ")
        lines.append("".join(row))

        # separator below row
        sep = ["+"]
        for c in range(m):
            sep.append(("---" if has_wall_between((r, c), Dir.DOWN) else "   ") + "+")
        lines.append("".join(sep))

    return "\n".join(lines)


def render_blank(board: Board, start_pos: tuple[int, int]) -> str:
    """Render a public/hidden map: only the outer frame + the start marker.

    Design choice: in non-debug mode we avoid drawing solid-looking walls.
    We draw the same grid geometry, but replace wall segments with dots.

    - No internal walls are shown.
    - No features are shown.
    - Exit edge is not shown.
    """

    n, m = board.n, board.m
    lines: list[str] = []

    # top border row (single dot segment)
    top = ["+"]
    for _c in range(m):
        top.append(" . " + "+")
    lines.append("".join(top))

    for r in range(n):
        # content row with dotted borders
        row = []
        for c in range(m):
            if c == 0:
                row.append(".")

            ch = "S" if (r, c) == tuple(start_pos) else " "
            row.append(f" {ch} ")

            row.append(".")
        lines.append("".join(row))

        # separator below row (single dot segment)
        sep = ["+"]
        for _c in range(m):
            sep.append(" . " + "+")
        lines.append("".join(sep))

    return "\n".join(lines)
