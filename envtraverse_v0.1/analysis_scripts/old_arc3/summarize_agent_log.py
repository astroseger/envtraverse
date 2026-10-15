from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ATTEMPT_DIR_RE = re.compile(r"^level_(\d+)_attempt_(\d+)$")
ATTEMPT_STEP_METADATA_RE = re.compile(r"^step_(\d{4})_metadata\.json$")
OBSERVATION_STEP_METADATA_RE = re.compile(r"^step_(\d+)_metadata\.txt$")


@dataclass(frozen=True)
class IterationSummary:
    iteration_id: int
    timestamp: str | None
    current_level_index: int | None
    n_steps_total: int
    n_steps_current_level: int
    n_game_over_attempts_current_level: int
    is_game_over: bool
    is_solved: bool
    protocols: tuple[str, ...]
    step_index: int | None = None
    terminal: bool = False


@dataclass(frozen=True)
class AttemptInfo:
    level_index: int
    attempt_index: int
    initial_step_index: int
    final_step_index: int
    n_steps: int


@dataclass(frozen=True)
class ObservationStep:
    step_index: int
    metadata: dict
    path: Path


@dataclass(frozen=True)
class ObservationAttempt:
    level_index: int
    attempt_index: int
    step_indices: tuple[int, ...]

    @property
    def n_steps(self) -> int:
        return len(self.step_indices)


@dataclass(frozen=True)
class ObservationPosition:
    level_index: int
    attempt_index: int
    step_on_attempt: int
    step_on_level: int


@dataclass(frozen=True)
class ObservationTimeline:
    attempts_by_level: dict[int, list[ObservationAttempt]]
    state_levels: dict[int, int]
    action_positions: dict[int, ObservationPosition]


def read_json_lines(log_path: Path) -> list[dict]:
    events: list[dict] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {log_path}: {exc}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"Expected a JSON object on line {line_number} of {log_path}")
            events.append(event)
    return events


def summarize_iterations(events: list[dict]) -> list[IterationSummary]:
    summaries: list[IterationSummary] = []
    pending_index: int | None = None

    for event in events:
        message = event.get("message")

        if message == "iteration inspection":
            step_index = event.get("step_index")
            summaries.append(
                IterationSummary(
                    iteration_id=int(event["iteration_id"]),
                    timestamp=event.get("timestamp"),
                    current_level_index=event.get("current_level_index"),
                    n_steps_total=int(event.get("n_steps_total", 0)),
                    n_steps_current_level=int(event.get("n_steps_current_level", 0)),
                    n_game_over_attempts_current_level=int(
                        event.get("n_game_over_attempts_current_level", 0)
                    ),
                    is_game_over=bool(event.get("is_game_over", False)),
                    is_solved=bool(event.get("is_solved", False)),
                    protocols=(),
                    step_index=int(step_index) if step_index is not None else None,
                    terminal=bool(event.get("terminal", False)),
                )
            )
            pending_index = len(summaries) - 1
            continue

        if message == "selected protocol" and pending_index is not None:
            pending = summaries[pending_index]
            protocol = event.get("protocol")
            if isinstance(protocol, str) and protocol:
                summaries[pending_index] = IterationSummary(
                    iteration_id=pending.iteration_id,
                    timestamp=pending.timestamp,
                    current_level_index=pending.current_level_index,
                    n_steps_total=pending.n_steps_total,
                    n_steps_current_level=pending.n_steps_current_level,
                    n_game_over_attempts_current_level=pending.n_game_over_attempts_current_level,
                    is_game_over=pending.is_game_over,
                    is_solved=pending.is_solved,
                    protocols=pending.protocols + (protocol,),
                    step_index=pending.step_index,
                    terminal=pending.terminal,
                )

    return summaries


def latest_session_dir(sessions_root: Path) -> Path:
    if not sessions_root.is_dir():
        raise FileNotFoundError(f"Sessions directory does not exist: {sessions_root}")

    has_attempts = any(
        path.is_dir() and ATTEMPT_DIR_RE.match(path.name)
        for path in sessions_root.iterdir()
    )
    if has_attempts:
        return sessions_root

    session_dirs = sorted(path for path in sessions_root.iterdir() if path.is_dir())
    if not session_dirs:
        raise FileNotFoundError(f"No session directories found in {sessions_root}")
    return session_dirs[-1]


def session_root_for_log(log_path: Path) -> Path:
    client_dir = log_path.parent / "agent_run" / "client"
    session_dir = client_dir / "session"
    if session_dir.is_dir():
        return session_dir
    return client_dir / "sessions"


def observations_dir_for_log(log_path: Path) -> Path:
    run_dir = log_path.parent
    candidates = (
        run_dir / "agent_run" / "client" / "observations",
        run_dir / "agent_run" / "observations",
        run_dir / "client" / "observations",
        run_dir / "observations",
    )
    for candidate in candidates:
        if candidate.is_dir() and any(
            path.is_file() and OBSERVATION_STEP_METADATA_RE.match(path.name)
            for path in candidate.iterdir()
        ):
            return candidate
    raise FileNotFoundError(f"No flat observations directory found next to {log_path}")


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _step_metadata_numbers(attempt_dir: Path) -> list[int]:
    return sorted(
        int(match.group(1))
        for path in attempt_dir.glob("step_*_metadata.json")
        if (match := ATTEMPT_STEP_METADATA_RE.match(path.name))
    )


def read_observation_steps(observations_dir: Path) -> list[ObservationStep]:
    metadata_paths: list[tuple[int, Path]] = []
    for path in observations_dir.iterdir():
        if not path.is_file():
            continue
        match = OBSERVATION_STEP_METADATA_RE.match(path.name)
        if match is not None:
            metadata_paths.append((int(match.group(1)), path))

    metadata_paths.sort(key=lambda item: (item[0], item[1].name))
    if not metadata_paths:
        raise FileNotFoundError(f"No step metadata files found in {observations_dir}")

    actual_indices = [step_index for step_index, _ in metadata_paths]
    expected_indices = list(range(actual_indices[-1] + 1))
    if actual_indices != expected_indices:
        missing = sorted(set(expected_indices) - set(actual_indices))
        raise ValueError(
            f"Step metadata files in {observations_dir} must be contiguous from "
            f"step_0000; missing indices: {missing}"
        )

    return [
        ObservationStep(
            step_index=step_index,
            metadata=_read_json(path),
            path=path,
        )
        for step_index, path in metadata_paths
    ]


def _metadata_nonnegative_int(step: ObservationStep, key: str) -> int:
    value = step.metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{step.path}: {key} must be a non-negative integer")
    return value


def _state_level(step: ObservationStep) -> int:
    levels_completed = _metadata_nonnegative_int(step, "levels_completed")
    win_levels = _metadata_nonnegative_int(step, "win_levels")
    if win_levels == 0:
        raise ValueError(f"{step.path}: win_levels must be positive")
    if levels_completed > win_levels:
        raise ValueError(f"{step.path}: levels_completed exceeds win_levels")

    state = step.metadata.get("state")
    if not isinstance(state, str):
        raise ValueError(f"{step.path}: state must be a string")
    if state == "WIN":
        if levels_completed != win_levels:
            raise ValueError(
                f"{step.path}: WIN state has {levels_completed} of {win_levels} levels completed"
            )
        return win_levels
    if levels_completed == win_levels:
        raise ValueError(f"{step.path}: all levels are completed but state is not WIN")
    return levels_completed + 1


def _action_id(step: ObservationStep) -> str:
    action_input = step.metadata.get("action_input")
    if not isinstance(action_input, dict):
        raise ValueError(f"{step.path}: action_input must be an object")
    action_id = action_input.get("id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{step.path}: action_input.id must be a non-empty string")
    return action_id


def read_observation_timeline(observations_dir: Path) -> ObservationTimeline:
    steps = read_observation_steps(observations_dir)
    state_levels = {step.step_index: _state_level(step) for step in steps}
    attempt_steps: dict[int, list[list[int]]] = {state_levels[0]: [[]]}
    action_positions: dict[int, ObservationPosition] = {}

    for previous, current in zip(steps, steps[1:]):
        if previous.metadata.get("state") == "WIN":
            raise ValueError(f"{current.path}: action recorded after the game was won")

        level_before = state_levels[previous.step_index]
        level_after = state_levels[current.step_index]
        if level_after not in {level_before, level_before + 1}:
            raise ValueError(
                f"{current.path}: current level changed from {level_before} to {level_after}"
            )

        attempts = attempt_steps.setdefault(level_before, [[]])
        if _action_id(current) == "RESET":
            attempts.append([])
        attempts[-1].append(current.step_index)

        action_positions[current.step_index] = ObservationPosition(
            level_index=level_before,
            attempt_index=len(attempts),
            step_on_attempt=len(attempts[-1]),
            step_on_level=sum(len(step_indices) for step_indices in attempts),
        )

        if level_after != level_before:
            attempt_steps.setdefault(level_after, [[]])

    attempts_by_level = {
        level_index: [
            ObservationAttempt(
                level_index=level_index,
                attempt_index=attempt_index,
                step_indices=tuple(step_indices),
            )
            for attempt_index, step_indices in enumerate(attempts, start=1)
        ]
        for level_index, attempts in sorted(attempt_steps.items())
    }
    return ObservationTimeline(
        attempts_by_level=attempts_by_level,
        state_levels=state_levels,
        action_positions=action_positions,
    )


def _latest_metadata_path(attempt_dir: Path, n_steps: int) -> Path:
    if n_steps == 0:
        return attempt_dir / "initial_metadata.json"
    return attempt_dir / f"step_{n_steps:04d}_metadata.json"


def read_attempts_by_level(session_dir: Path) -> dict[int, list[AttemptInfo]]:
    attempts_by_level: dict[int, list[AttemptInfo]] = {}

    for path in sorted(p for p in session_dir.iterdir() if p.is_dir()):
        match = ATTEMPT_DIR_RE.match(path.name)
        if match is None:
            continue
        level_index = int(match.group(1))
        attempt_index = int(match.group(2))
        step_numbers = _step_metadata_numbers(path)
        initial_metadata = _read_json(path / "initial_metadata.json")
        latest_metadata = _read_json(_latest_metadata_path(path, len(step_numbers)))
        attempts_by_level.setdefault(level_index, []).append(
            AttemptInfo(
                level_index=level_index,
                attempt_index=attempt_index,
                initial_step_index=int(initial_metadata["step_index"]),
                final_step_index=int(latest_metadata["step_index"]),
                n_steps=len(step_numbers),
            )
        )

    for attempts in attempts_by_level.values():
        attempts.sort(key=lambda attempt: attempt.attempt_index)

    if not attempts_by_level:
        raise FileNotFoundError(f"No attempt directories found in {session_dir}")
    return attempts_by_level


def level_step_totals(attempts_by_level: dict[int, list[AttemptInfo]]) -> dict[int, int]:
    return {
        level_index: attempts[-1].final_step_index - attempts[0].initial_step_index
        for level_index, attempts in attempts_by_level.items()
    }


def steps_before_level(level_index: int, totals_by_level: dict[int, int]) -> int:
    return sum(total for level, total in totals_by_level.items() if level < level_index)


def locate_iteration_position(
    summary: IterationSummary,
    attempts_by_level: dict[int, list[AttemptInfo]],
    totals_by_level: dict[int, int],
) -> tuple[int | None, int | None, int | None, int | None]:
    level_index = summary.current_level_index
    if level_index is None:
        return None, None, None, None

    attempts = attempts_by_level.get(level_index)
    if not attempts:
        raise ValueError(f"Level {level_index} from iteration {summary.iteration_id} is missing in session")

    prior_total = steps_before_level(level_index, totals_by_level)
    if prior_total + summary.n_steps_current_level != summary.n_steps_total:
        raise ValueError(
            f"Iteration {summary.iteration_id}: log/session mismatch for n_steps_total "
            f"(log={summary.n_steps_total}, session_prefix={prior_total + summary.n_steps_current_level})"
        )

    step_on_level = summary.n_steps_current_level
    total_steps_on_level = totals_by_level[level_index]
    if step_on_level > total_steps_on_level:
        raise ValueError(
            f"Iteration {summary.iteration_id}: n_steps_current_level={step_on_level} exceeds "
            f"session total {total_steps_on_level} for level {level_index}"
        )

    level_start_step_index = attempts[0].initial_step_index
    for attempt in attempts:
        attempt_start = attempt.initial_step_index - level_start_step_index
        attempt_end = attempt.final_step_index - level_start_step_index
        if attempt_start <= step_on_level <= attempt_end:
            step_on_attempt = step_on_level - attempt_start
            return attempt.attempt_index, len(attempts), step_on_attempt, attempt.n_steps

    raise ValueError(
        f"Iteration {summary.iteration_id}: failed to locate level position for "
        f"n_steps_current_level={step_on_level}"
    )


def format_summary(summary: IterationSummary, attempts_by_level: dict[int, list[AttemptInfo]]) -> str:
    protocol = ",".join(summary.protocols) if summary.protocols else "-"

    if summary.current_level_index is None:
        return f"iteration={summary.iteration_id} level=? attempt=? step_a=? step_l=? protocol={protocol}"

    totals_by_level = level_step_totals(attempts_by_level)
    attempt_index, total_attempts, step_on_attempt, total_steps_on_attempt = locate_iteration_position(
        summary,
        attempts_by_level,
        totals_by_level,
    )
    total_steps_on_level = totals_by_level[summary.current_level_index]

    return (
        f"iteration={summary.iteration_id} "
        f"level={summary.current_level_index} "
        f"attempt={attempt_index}/{total_attempts} "
        f"step_a={step_on_attempt}/{total_steps_on_attempt} "
        f"step_l={summary.n_steps_current_level}/{total_steps_on_level} "
        f"protocol={protocol}"
    )


def locate_observation_position(
    summary: IterationSummary,
    timeline: ObservationTimeline,
) -> tuple[int, int, int, int, int, int, int]:
    if summary.step_index is None:
        raise ValueError(f"Iteration {summary.iteration_id} is missing step_index")

    level_index = timeline.state_levels.get(summary.step_index)
    if level_index is None:
        raise ValueError(
            f"Iteration {summary.iteration_id}: step {summary.step_index} is missing in observations"
        )

    attempts = timeline.attempts_by_level.get(level_index)
    if not attempts:
        raise ValueError(
            f"Iteration {summary.iteration_id}: level {level_index} is missing in observations"
        )

    position = timeline.action_positions.get(summary.step_index)
    if position is not None and position.level_index == level_index:
        attempt_index = position.attempt_index
        step_on_attempt = position.step_on_attempt
        step_on_level = position.step_on_level
    else:
        # Step zero and the state produced by a level-completing action are the
        # initial state of an attempt, before any action on the displayed level.
        attempt_index = 1
        step_on_attempt = 0
        step_on_level = 0

    attempt = attempts[attempt_index - 1]
    total_steps_on_level = sum(item.n_steps for item in attempts)
    return (
        level_index,
        attempt_index,
        len(attempts),
        step_on_attempt,
        attempt.n_steps,
        step_on_level,
        total_steps_on_level,
    )


def format_observation_summary(
    summary: IterationSummary,
    timeline: ObservationTimeline,
) -> str:
    protocol = ",".join(summary.protocols) if summary.protocols else "-"
    (
        level_index,
        attempt_index,
        total_attempts,
        step_on_attempt,
        total_steps_on_attempt,
        step_on_level,
        total_steps_on_level,
    ) = locate_observation_position(summary, timeline)
    return (
        f"iteration={summary.iteration_id} "
        f"level={level_index} "
        f"attempt={attempt_index}/{total_attempts} "
        f"step_a={step_on_attempt}/{total_steps_on_attempt} "
        f"step_l={step_on_level}/{total_steps_on_level} "
        f"protocol={protocol}"
    )


def summarize_log(log_path: Path) -> list[str]:
    events = read_json_lines(log_path)
    summaries = summarize_iterations(events)
    if not summaries:
        return []

    has_step_indices = [summary.step_index is not None for summary in summaries]
    if all(has_step_indices):
        timeline = read_observation_timeline(observations_dir_for_log(log_path))
        return [format_observation_summary(summary, timeline) for summary in summaries]
    if any(has_step_indices):
        raise ValueError(f"Mixed iteration inspection formats in {log_path}")

    attempts_by_level = read_attempts_by_level(
        latest_session_dir(session_root_for_log(log_path))
    )
    return [format_summary(summary, attempts_by_level) for summary in summaries]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read an agent.log JSONL file and print a short per-iteration summary "
            "using the session or flat observation data next to that log."
        )
    )
    parser.add_argument("log_path", type=Path, help="Path to agent.log")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    log_path = args.log_path.resolve()
    if not log_path.is_file():
        print(f"Log file does not exist: {log_path}", file=sys.stderr)
        return 1

    try:
        for line in summarize_log(log_path):
            print(line)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
