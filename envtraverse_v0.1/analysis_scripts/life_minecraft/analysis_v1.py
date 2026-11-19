#!/usr/bin/env python3
"""Report Life Minecraft outcomes and gameplay statistics from a run archive.

Usage: python3 analysis_v1.py /path/to/experiment [--json]
Reads only experiment/run/ and experiment/run_mc/. Uses the standard library,
does not import the environment, and does not modify files or contact a runtime.
Statistics cover the entire recorded session; task success uses the saved verdict,
including its latched timeout, rather than process exit codes or later kills.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def count(value):
    return value if type(value) is int and value >= 0 else None


def timestamp(event):
    milliseconds = number(event.get("time_ms"))
    return milliseconds / 1000 if milliseconds is not None else number(event.get("time"))


class Reader:
    """Keep archive reads within the two output directories, including symlinks."""

    def __init__(self, root):
        self.roots = (root / "run", root / "run_mc")
        self.warnings = []

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def exists(self, path):
        if not any(path.resolve().is_relative_to(base) for base in self.roots):
            self.warn(f"Skipped path outside run/ and run_mc/: {path}")
            return False
        return path.is_file()

    def lines(self, path):
        if not self.exists(path):
            return
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                yield from source
        except OSError as exc:
            self.warn(f"Could not read {path}: {exc}")

    def object(self, path):
        if not self.exists(path):
            return {}
        try:
            value = json.loads("".join(self.lines(path)))
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            return value
        except ValueError as exc:
            self.warn(f"Invalid JSON in {path}: {exc}")
            return {}

    def events(self, path):
        for line_number, line in enumerate(self.lines(path), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("expected a JSON object")
                if "data" in value and not isinstance(value["data"], dict):
                    raise ValueError("expected an event data object")
                yield value
            except ValueError:
                self.warn(f"Skipped malformed record at {path}:{line_number}; counts may be partial")


def discover(root, reader):
    names = {}
    for line in reader.lines(root / "run/runner.log"):
        match = re.search(r"starting life_minecraft server for game (\S+) as alias ([\w-]+)", line)
        if match:
            names[match[2]] = match[1].rstrip(";")
        if "game alias mapping=" in line:
            try:
                mapping = ast.literal_eval(line.split("game alias mapping=", 1)[1].strip())
                if isinstance(mapping, dict):
                    names.update({alias: task for alias, task in mapping.items()
                                  if isinstance(alias, str) and re.fullmatch(r"[\w-]+", alias)
                                  and isinstance(task, str)})
            except (ValueError, SyntaxError):
                reader.warn("Could not parse runner game alias mapping")
    for path in sorted((root / "run").glob("*/server/server.log")):
        for line in reader.lines(path):
            match = re.search(r"starting requested_game_id=\S+ game_id=(\S+)", line)
            if match:
                names[path.parents[1].name] = match[1]
                break
    for path in sorted((root / "run_mc/sessions").glob("*")):
        if path.is_dir():
            names.setdefault(path.name, "unknown")
    return names


def merged_events(directory, reader):
    """Minecraft seq is unique within its session; runtime mirrors retain it."""
    merged, seen = [], set()
    for path in (directory / "telemetry/events.jsonl", directory / "events.jsonl"):
        for event in reader.events(path):
            if "seq" in event:
                key = (str(event.get("session_id", "")), str(event["seq"]))
            else:
                key = json.dumps({k: v for k, v in event.items() if k != "source"}, sort_keys=True)
            if key not in seen:
                seen.add(key)
                merged.append(event)
    return sorted(merged, key=lambda e: timestamp(e) if timestamp(e) is not None else float("inf"))


def saved_result(root, alias, directory, events, reader):
    result = reader.object(directory / "result.json")
    if isinstance(result.get("success"), bool):
        return result, "result.json"
    if result:
        reader.warn("result.json lacks an explicit success boolean; checking other verdict records")
    for event in events:
        if event.get("type") in {"timeout", "goal_achieved", "task_finished"}:
            value = event.get("data")
            if isinstance(value, dict) and isinstance(value.get("success"), bool):
                return {**value, "finished_at": timestamp(event)}, "events.jsonl"
    observations = root / "run" / alias / "observations"
    candidates = []
    for path in observations.glob("step_*_action.json"):
        match = re.fullmatch(r"step_(\d+)_action\.json", path.name)
        if match:
            candidates.append((int(match[1]), path))
    for _, path in sorted(candidates, reverse=True):
        value = reader.object(path)
        if value:
            if value.get("terminal") is True:
                return value, str(path.relative_to(root))
            break
    return {}, "no saved verdict"


ENVIRONMENTAL_DEATHS = (
    ("Drowning", ("drowned",)),
    ("Cactus", ("walked into a cactus", "was pricked to death")),
    ("Void", ("fell out of the world", "didn't want to live in the same world")),
    ("Falling", ("fell from a high place", "hit the ground too hard", "fell off",
                 "fell while", "was doomed to fall", "was impaled on a stalagmite")),
    ("Lava", ("tried to swim in lava",)),
    ("Magma", ("discovered the floor was lava", "walked into danger zone")),
    ("Fire", ("went up in flames", "burned to death", "was burnt to a crisp")),
    ("Suffocation", ("suffocated in a wall", "was squished too much")),
    ("Starvation", ("starved to death",)),
    ("Freezing", ("froze to death",)),
    ("Lightning", ("was struck by lightning",)),
    ("Kinetic damage", ("experienced kinetic energy",)),
)


def environmental_cause(message):
    for cause, phrases in ENVIRONMENTAL_DEATHS:
        if any(message.startswith(phrase) for phrase in phrases):
            return cause
    return None


def death_messages(directory, player_names, reader):
    messages = []
    for line in reader.lines(directory / "containers.log"):
        match = re.search(r"\[(\d{2}:\d{2}:\d{2})\] \[Server thread/INFO\]: (.*)", line)
        if not match:
            continue
        for player in player_names:
            if match[2].startswith(player + " "):
                body = match[2][len(player) + 1:]
                if environmental_cause(body) or body.startswith((
                    "was slain by", "was shot by", "was killed", "blew up", "was blown up",
                    "was pummeled by", "was impaled by", "was fireballed by", "was stung to death",
                    "withered away", "died",
                )):
                    messages.append((match[1], body))
                break
    return messages


def death_breakdown(deaths, messages, total, reader):
    causes, escape_details = Counter(), Counter()
    # With a complete death log, vanilla messages and telemetry have the same
    # chronological order. For partial logs, use UTC seconds only when unambiguous.
    def clock_of(event):
        value = timestamp(event)
        return datetime.fromtimestamp(value, timezone.utc).strftime("%H:%M:%S") if value is not None else None

    aligned = (len(messages) == len(deaths)
               and all(clock_of(event) == message[0] for event, message in zip(deaths, messages)))
    by_time = {}
    for clock, body in messages:
        by_time.setdefault(clock, []).append(body)
    for index, event in enumerate(deaths):
        body = ""
        if aligned:
            body = messages[index][1]
        elif timestamp(event) is not None:
            clock = clock_of(event)
            if len(by_time.get(clock, [])) == 1:
                body = by_time[clock][0]
        data = event.get("data", {})
        attacker = data.get("direct_attacker")
        if not isinstance(attacker, dict):
            attacker = {}
        cause = environmental_cause(body) or attacker.get("name") or "Unknown"
        causes[cause] += 1
        if " while trying to escape " in body:
            escape_details[body] += 1
    if total is not None and total > len(deaths):
        causes["Unknown"] += total - len(deaths)
        reader.warn("Death-event history is incomplete; missing causes are reported as Unknown")
    return dict(causes), dict(escape_details)


def valid_counter_map(value):
    if not isinstance(value, dict):
        return None
    if any(not isinstance(key, str) or count(n) is None for key, n in value.items()):
        return None
    return value


def gameplay_stats(events, state, reader):
    counters = state.get("counters") or {}
    if not isinstance(counters, dict):
        counters = {}
    recorded = any(e.get("type") in {"server_started", "join", "death", "respawn", "kill", "craft", "entity_death"}
                   for e in events)
    deaths = [e for e in events if e.get("type") == "death"]
    respawns = [e for e in events if e.get("type") == "respawn" and e.get("data", {}).get("after_death") is True]
    kills, crafted = Counter(), Counter()
    for event in events:
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        if event.get("type") == "kill" and data.get("credited_to_player") is True:
            entity = data.get("entity_type")
            if isinstance(entity, str):
                kills[entity] += 1
        elif event.get("type") == "craft" and isinstance(data.get("item"), str):
            amount = count(data.get("count"))
            if amount is not None:
                crafted[data["item"]] += amount
    values = {"deaths": len(deaths), "respawns": len(respawns), "kills": dict(kills), "crafted": dict(crafted)}
    max_seq = max((e["seq"] for e in events if type(e.get("seq")) is int), default=0)
    snapshot_complete = (count(state.get("last_event_seq")) or 0) >= max_seq
    for key, observed in values.items():
        saved = valid_counter_map(counters.get(key)) if isinstance(observed, dict) else count(counters.get(key))
        if saved is not None:
            if saved != observed and snapshot_complete:
                reader.warn(f"{key}: snapshot and event counts differ; using the larger recorded counts")
            values[key] = dict(Counter(observed) | Counter(saved)) if isinstance(observed, dict) else max(observed, saved)
        else:
            if key in counters:
                reader.warn(f"Invalid {key} snapshot counter; relying on available events")
            if not recorded:
                values[key] = None
    return values, deaths


def action_stats(directory, reader):
    path = directory / "actions.jsonl"
    if not reader.exists(path):
        return None, {}
    accepted, applied, seen = Counter(), Counter(), set()
    warnings_before = len(reader.warnings)
    for index, event in enumerate(reader.events(path)):
        action = event.get("action")
        if event.get("accepted") is True and isinstance(action, dict):
            key = ("accepted", str(event.get("action_id", index)))
            if key not in seen:
                seen.add(key)
                accepted[str(action.get("id", "unknown"))] += 1
        elif event.get("type") == "action_applied":
            key = ("applied", str(event.get("seq", index)))
            if key not in seen:
                seen.add(key)
                data = event.get("data") or {}
                applied[str(data.get("action", {}).get("id", "unknown"))] += 1
    if not accepted and applied:
        reader.warn("Accepted-action records missing; action count recovered from bridge-applied events")
    counts = accepted or applied
    if not counts and len(reader.warnings) > warnings_before:
        return None, {}
    return sum(counts.values()), dict(counts)


def analyze_game(root, alias, fallback_name):
    reader = Reader(root)
    directory = root / "run_mc/sessions" / alias
    metadata = reader.object(directory / "session.json")
    events = merged_events(directory, reader)
    result, result_source = saved_result(root, alias, directory, events, reader)
    snapshots = [reader.object(directory / "telemetry/state.json")]
    verdict = reader.object(directory / "verdict_state.json").get("state")
    if isinstance(verdict, dict):
        snapshots.append(verdict)
    state = max(snapshots, key=lambda s: number(s.get("time_ms")) or 0)
    stats, deaths = gameplay_stats(events, state, reader)
    task = metadata.get("task_id") or result.get("task_id") or fallback_name
    success = result.get("success")
    outcome = "completed" if success is True else "failed" if success is False else "unknown"
    if outcome == "unknown":
        reader.warn("No explicit task verdict; process exit or session closure does not establish success")
    started = number(metadata.get("ready_at"))
    ended = number(result.get("finished_at"))
    elapsed = number(result.get("elapsed_seconds"))
    if elapsed is None and started is not None:
        last = ended if ended is not None else number(state.get("time_ms"))
        if ended is None and last is not None:
            last /= 1000
        if last is not None:
            elapsed = max(0, last - started)
    player_names = {str(state.get("expected_player") or "AgentPlayer")}
    player_names.update(e.get("data", {}).get("name", "AgentPlayer") for e in deaths)
    causes, escape_details = death_breakdown(deaths, death_messages(directory, player_names, reader), stats["deaths"], reader)
    accepted, action_types = action_stats(directory, reader)
    post_result = Counter()
    for event in events:
        t = timestamp(event)
        if ended is not None and t is not None and t > ended:
            kind, data = event.get("type"), event.get("data", {})
            if kind == "death":
                post_result["deaths"] += 1
            elif kind == "respawn" and data.get("after_death") is True:
                post_result["respawns"] += 1
            elif kind == "kill" and data.get("credited_to_player") is True:
                post_result["kills"] += 1
    player_at_result = verdict.get("player") if isinstance(verdict, dict) else None
    return {
        "session_id": alias, "task_id": task, "outcome": outcome,
        "status": result.get("status", metadata.get("status", "unknown")),
        "reason": result.get("reason", "no saved verdict"), "result_source": result_source,
        "started_at": started or number(metadata.get("created_at")), "elapsed_seconds": elapsed,
        **stats, "death_causes": causes, "escape_deaths": escape_details,
        "accepted_actions": accepted, "action_types": action_types,
        "alive_at_result": player_at_result.get("alive") if isinstance(player_at_result, dict) else None,
        "post_result_events": dict(post_result), "warnings": reader.warnings,
    }


def analyze_run(root):
    root = Path(root).expanduser().resolve()
    reader = Reader(root)
    if not root.is_dir():
        raise ValueError(f"Run root is not a directory: {root}")
    names = discover(root, reader)
    if not names:
        raise ValueError(f"No games found under {root}/run or {root}/run_mc")
    games = [analyze_game(root, alias, task) for alias, task in names.items()]
    for game in games:
        game["warnings"] = reader.warnings + game["warnings"]
    return sorted(games, key=lambda g: (g["started_at"] if g["started_at"] is not None else float("inf"), g["session_id"]))


def display(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def items(values):
    if values is None:
        return "unknown"
    return ", ".join(f"{display(k.removeprefix('minecraft:'))}: {v}" for k, v in sorted(values.items())) or "none"


def render_report(games):
    rows = ["| Task | Session | Completed | Reason | Duration | Deaths | Respawns | Kills | Actions |",
            "|---|---|---|---|---|---:|---:|---:|---:|"]
    for game in games:
        completed = {"completed": "YES", "failed": "NO", "unknown": "UNKNOWN"}[game["outcome"]]
        kills = None if game["kills"] is None else sum(game["kills"].values())
        values = (game["task_id"], game["session_id"][:8], completed, game["reason"],
                  duration(game["elapsed_seconds"]), game["deaths"], game["respawns"], kills, game["accepted_actions"])
        rows.append("| " + " | ".join("?" if v is None else display(v) for v in values) + " |")
    completed = sum(g["outcome"] == "completed" for g in games)
    unknown = sum(g["outcome"] == "unknown" for g in games)
    rows += ["", f"Tasks completed: {completed}/{len(games)}; unknown outcomes: {unknown}.",
             "Statistics cover the entire recorded session; completion uses the saved task verdict."]
    if all(g["deaths"] is not None and g["respawns"] is not None and g["kills"] is not None for g in games):
        rows.append(f"Recorded totals: {sum(g['deaths'] for g in games)} deaths, "
                    f"{sum(g['respawns'] for g in games)} respawns, "
                    f"{sum(sum(g['kills'].values()) for g in games)} credited kills.")
    for game in games:
        rows += ["", f"{display(game['task_id'])} [{game['session_id']}]"]
        rows.append("  Death causes: " + items(game["death_causes"]) if game["deaths"] is not None else "  Death causes: unknown")
        rows += ["  Credited kills: " + items(game["kills"]), "  Crafted items: " + items(game["crafted"])]
        if game["escape_deaths"]:
            rows.append("  Environmental death details: " + items(game["escape_deaths"]))
        if game["alive_at_result"] is not None:
            rows.append("  Player at task verdict: " + ("alive" if game["alive_at_result"] else "dead"))
        if game["post_result_events"]:
            rows.append("  Events after task verdict (included above): " + items(game["post_result_events"]))
        rows.extend("  Warning: " + warning for warning in game["warnings"])
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path, help="Experiment directory containing run/ and run_mc/")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of the text report")
    args = parser.parse_args(argv)
    try:
        games = analyze_run(args.run_root)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"games": games}, indent=2, ensure_ascii=False, allow_nan=False) if args.json else render_report(games))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
