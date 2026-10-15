#!/usr/bin/env python3
"""Minimal Web UI server (no external deps).

This server is intentionally simple:
- standard-library only (http.server)
- in-memory sessions
- serves a tiny HTML/JS UI and a JSON API

Run:
  python3 scripts/webui_server.py --host 127.0.0.1 --port 8000

Then open:
  http://127.0.0.1:8000/
"""

from __future__ import annotations

import argparse
import json
import secrets
from dataclasses import dataclass, field
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

# local imports
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labyrinth.config import config_from_dict
from labyrinth.engine import Engine, available_actions
from labyrinth.i18n import t
from labyrinth.generator import Generated, generate
from labyrinth.loader import load_or_generate
from labyrinth.render import render, render_blank


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webui"
PACKS = ROOT / "packs"
LOGS = ROOT / "logs"


def _json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_leaf_dir(p: Path) -> bool:
    if not p.is_dir():
        return False
    # leaf if contains any bundle/config file
    for _ in p.glob("*.json"):
        return True
    if (p / "base.cfg").exists():
        return True
    return False


def _list_dirs(base: Path) -> list[str]:
    if not base.exists():
        return []
    return sorted([p.name for p in base.iterdir() if p.is_dir()])


def _list_files(base: Path, exts=(".json", ".cfg")) -> list[str]:
    if not base.exists():
        return []
    out = []
    for p in base.iterdir():
        if p.is_file() and p.suffix in exts:
            out.append(p.name)
    return sorted(out)


def _pick_training_source(series: str, stage: str, *, rng, seed: int | None) -> dict:
    """Choose what to load for TRAINING.

    Rule:
    - If seed is NOT provided: pick a random saved bundle JSON from the stage folder.
    - If seed IS provided: load base.cfg and override rng.seed.

    This matches the desired UX: stable base config per stage, but many pre-generated
    instances when seed is not specified.
    """

    stage_dir = PACKS / "TRAINING" / series / stage
    if not stage_dir.exists():
        raise FileNotFoundError(f"No such training stage: {stage_dir}")

    if seed is not None:
        base_cfg = stage_dir / "base.cfg"
        if not base_cfg.exists():
            raise FileNotFoundError(f"Missing base.cfg in: {stage_dir}")
        return _load_config_with_seed(_read_json(base_cfg), seed)

    # seed is None -> choose random json bundle (exclude base.cfg)
    bundles = sorted([p for p in stage_dir.glob("*.json") if p.name != "base.cfg"])
    if not bundles:
        # fallback to base.cfg even without seed
        base_cfg = stage_dir / "base.cfg"
        if not base_cfg.exists():
            raise FileNotFoundError(f"No bundles and no base.cfg in: {stage_dir}")
        return _read_json(base_cfg)

    return _read_json(rng.choice(bundles))


def _load_config_with_seed(d: dict, seed: Optional[int]) -> dict:
    if seed is None:
        return d
    dd = json.loads(json.dumps(d))  # cheap deep copy
    dd.setdefault("rng", {})
    dd["rng"]["seed"] = int(seed)
    return dd


@dataclass
class Session:
    eng: Engine
    gen: Generated
    lang: str
    debug: bool
    created_at: float
    last_seen: float

    client: str  # "webui" | "api"
    task_ref: dict

    # Runtime state (must be thread-safe: ThreadingHTTPServer runs handlers concurrently)
    steps_taken: int = 0
    logged_win: bool = False

    # Per-session lock to serialize /api/step updates.
    lock: threading.Lock = field(default_factory=threading.Lock)

    # Optional client-side sequencing. If provided by the client, we can safely ignore
    # duplicates/out-of-order requests without breaking the session.
    last_seq: Optional[int] = None
    last_response: Optional[dict] = None

    # Dataset trace: whether we already wrote the initial <MAP> and initial <POS>
    trace_started: bool = False
    trace_ended: bool = False

    max_steps: Optional[int] = None


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL_SECONDS = 30 * 60 * 1000  # 30000 minutes
SERVER_DEBUG = False

STEPS_LOG_PATH = LOGS / "steps.log"

# Optional dataset traces for supervised training (ground-truth map/action/position).
TRACE_DATASET = False
TRACE_DIR = LOGS / "dataset"

STEPS_LOG_ENABLED = False


def _append_log(*, client: str, task_ref: dict, steps_taken: int, session_id: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    path = LOGS / ("webui_wins.log" if client == "webui" else "api_wins.log")
    rec = {
        "ts": ts,
        "client": client,
        "session_id": session_id,
        "steps": int(steps_taken),
        "task": task_ref,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _append_step_log(rec: dict) -> None:
    """Append one JSONL record for step/debug logging."""
    if not STEPS_LOG_ENABLED:
        return
    LOGS.mkdir(parents=True, exist_ok=True)
    with STEPS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _trace_path_for_session(sid: str) -> Path:
    # Variant (2) lives under dataset/seq/
    d = TRACE_DIR / "seq"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.trace.txt"


def _trace_txt_path_for_session(sid: str) -> Path:
    d = TRACE_DIR / "txt"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.trace.txt"


def _trace_txt_append(sid: str, text: str) -> None:
    p = _trace_txt_path_for_session(sid)
    with p.open("a", encoding="utf-8") as f:
        f.write(text)


def _trace_append(sid: str, text: str) -> None:
    p = _trace_path_for_session(sid)
    with p.open("a", encoding="utf-8") as f:
        f.write(text)


def _gc_sessions() -> None:
    now = time.time()
    with SESSIONS_LOCK:
        dead = [sid for sid, s in SESSIONS.items() if (now - float(s.last_seen)) > SESSION_TTL_SECONDS]
        for sid in dead:
            SESSIONS.pop(sid, None)


def _map_for(*, gen: Generated, eng: Engine, debug: bool) -> str:
    if debug:
        return render(gen.board, eng.state)
    return render_blank(gen.board, gen.start.pos)


class Handler(BaseHTTPRequestHandler):
    server_version = "LabyrinthWebUI/0.1"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: Any) -> None:
        self._send(code, _json(obj), "application/json; charset=utf-8")

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self._send(200, body, content_type)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path == "/":
            return self._send_file(WEB / "index.html", "text/html; charset=utf-8")
        if path == "/play":
            return self._send_file(WEB / "play.html", "text/html; charset=utf-8")
        if path == "/webui/app.js":
            return self._send_file(WEB / "app.js", "text/javascript; charset=utf-8")
        if path == "/webui/styles.css":
            return self._send_file(WEB / "styles.css", "text/css; charset=utf-8")
        if path == "/webui/win.txt":
            return self._send_file(WEB / "win.txt", "text/plain; charset=utf-8")

        if path == "/api/description":
            lang = (qs.get("lang", ["en"])[0] or "en")
            if lang not in ("en", "ru"):
                lang = "en"
            return self._send_json(200, {"lang": lang, "description": t("game.description", lang)})

        if path == "/api/groups":
            groups = [p.name for p in PACKS.iterdir() if p.is_dir() and p.name.isupper()]
            groups = [g for g in groups if g in ("TUTORIAL", "TRAINING", "CLASSIC", "EXTENDED")]
            order = {"TUTORIAL": 0, "TRAINING": 1, "CLASSIC": 2, "EXTENDED": 3}
            groups = sorted(groups, key=lambda g: order.get(g, 99))
            return self._send_json(200, {"groups": groups})

        if path == "/api/training":
            series = _list_dirs(PACKS / "TRAINING")
            stages: dict[str, list[str]] = {}
            for s in series:
                stages[s] = _list_dirs(PACKS / "TRAINING" / s)
            return self._send_json(200, {"series": series, "stages": stages})

        if path == "/api/tutorial":
            base = PACKS / "TUTORIAL"
            stages = []
            if base.exists():
                stages = sorted([p.name for p in base.iterdir() if p.is_dir() and p.name.upper().startswith("STAGE-")])
            return self._send_json(200, {"stages": stages})

        if path == "/api/classic":
            base = PACKS / "CLASSIC"
            diffs = _list_dirs(base)
            # enforce human-friendly order
            order = {"EASY": 0, "MEDIUM": 1, "HARD": 2}
            diffs = sorted(diffs, key=lambda d: order.get(d.upper(), 99))
            elements_by_diff: dict[str, list[str]] = {}
            sizes_by_diff_elem: dict[str, dict[str, list[str]]] = {}
            for d in diffs:
                elements = _list_dirs(base / d)
                elements_by_diff[d] = elements
                sizes_by_diff_elem[d] = {}
                for e in elements:
                    sizes_by_diff_elem[d][e] = _list_dirs(base / d / e)
            return self._send_json(
                200,
                {
                    "difficulties": diffs,
                    "elements": elements_by_diff,
                    "sizes": sizes_by_diff_elem,
                },
            )

        if path == "/api/extended":
            base = PACKS / "EXTENDED"
            exts = _list_dirs(base)
            # fixed order for now (as requested)
            order = {"boat": 0, "flashlight": 1, "grenades": 2}
            exts = sorted(exts, key=lambda x: order.get(x, 99))
            stages_by_ext: dict[str, list[str]] = {}
            for e in exts:
                stages_by_ext[e] = _list_dirs(base / e)
            return self._send_json(200, {"extensions": exts, "stages": stages_by_ext})

        if path == "/api/browse":
            # generic directory browser for EXTENDED (and other future groups)
            group = (qs.get("group", [""])[0] or "").upper()
            rel = qs.get("path", [""])[0]
            base = (PACKS / group / rel).resolve()
            # basic sandboxing: require it stays under PACKS
            if PACKS not in base.parents and base != PACKS:
                return self._send_json(400, {"error": "bad_path"})

            return self._send_json(
                200,
                {
                    "group": group,
                    "path": rel,
                    "dirs": _list_dirs(base),
                    "files": _list_files(base),
                    "leaf": _is_leaf_dir(base),
                },
            )

        if path == "/api/session":
            _gc_sessions()
            sid = qs.get("id", [""])[0]
            with SESSIONS_LOCK:
                s = SESSIONS.get(sid)
            if not s:
                return self._send_json(404, {"error": "no_session"})
            with s.lock:
                s.last_seen = time.time()

            return self._send_json(
                200,
                {
                    "id": sid,
                    "lang": s.lang,
                    "inventory": s.eng.snapshot().get("inventory"),
                    "map": _map_for(gen=s.gen, eng=s.eng, debug=s.debug),
                    "actions": list(
                        available_actions(
                            grenades_enabled=s.eng.grenades_enabled,
                            flashlight_enabled=getattr(s.eng, "flashlight_enabled", False),
                        )
                    ),
                },
            )

        return self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}

        if path == "/api/start":
            _gc_sessions()
            req_path = (body.get("path") or "").strip()
            group = (body.get("group") or "TRAINING").upper()
            seed = body.get("seed")
            lang = body.get("lang") or "en"
            client = body.get("client") or "api"
            if client not in ("webui", "api"):
                client = "api"

            agent = body.get("agent")
            if client == "api":
                # For benchmark logs we require naming the agent that plays.
                if not isinstance(agent, str) or not agent.strip():
                    return self._send_json(400, {"error": "missing_agent"})
                agent = agent.strip()
            elif isinstance(agent, str):
                agent = agent.strip() or None
            else:
                agent = None

            # Use non-deterministic choice for "pick a random labyrinth" behavior.
            rng = __import__("random").Random()

            selected_file = None
            task_ref = {"group": group, "seed": seed}
            if agent:
                task_ref["agent"] = agent

            def _load_from_packs_rel(rel: str) -> dict:
                """Load a config/bundle given a PACKS-relative path.

                rel examples:
                  CLASSIC/EASY/pits/4x4
                  CLASSIC/EASY/pits/4x4/0000.json
                  EXTENDED/boat/STAGE-01-4x4
                  TRAINING/S0-keys/STAGE-01
                  TUTORIAL/STAGE-01
                
                If rel is a directory:
                  - seed is None: choose random *.json (excluding base.cfg)
                  - seed is not None: use base.cfg with rng.seed override (if present)
                If rel is a file (*.json|*.cfg): load that file and apply seed override.
                """

                nonlocal selected_file

                # Basic normalization / sandboxing
                rel = rel.strip().lstrip("/")
                if ".." in rel.split("/"):
                    raise ValueError("bad_path")

                p = (PACKS / rel)
                if not p.exists():
                    raise FileNotFoundError("bad_path")

                if p.is_file():
                    if p.suffix not in (".json", ".cfg"):
                        raise ValueError("bad_path")
                    selected_file = str(p.relative_to(ROOT))
                    return _load_config_with_seed(_read_json(p), seed)

                # directory
                base_cfg = p / "base.cfg"
                if seed is not None and base_cfg.exists():
                    selected_file = str(base_cfg.relative_to(ROOT))
                    return _load_config_with_seed(_read_json(base_cfg), seed)

                bundles = sorted([x for x in p.glob("*.json") if x.name != "base.cfg"])
                if bundles:
                    x = rng.choice(bundles)
                    selected_file = str(x.relative_to(ROOT))
                    return _read_json(x)

                if base_cfg.exists():
                    selected_file = str(base_cfg.relative_to(ROOT))
                    return _read_json(base_cfg)

                raise FileNotFoundError("no_config_or_bundle")

            if req_path:
                # "path" is overloaded:
                # - WebUI legacy payload uses "path" as GROUP-relative (e.g. EASY/pits/4x4 when group=CLASSIC)
                # - Benchmark API uses "path" as PACKS-relative INCLUDING group prefix (e.g. CLASSIC/EASY/pits/4x4)
                # We disambiguate by checking whether the first segment is a known group.
                parts = [x for x in req_path.replace("\\", "/").split("/") if x]
                known_groups = {"TUTORIAL", "TRAINING", "CLASSIC", "EXTENDED"}
                if parts and parts[0].upper() in known_groups:
                    # New API: one "path" that includes the group at the front.
                    try:
                        group2 = parts[0].upper()
                        group = group2
                        task_ref["group"] = group
                        rel = "/".join([group2] + parts[1:])
                        # NOTE: _load_from_packs_rel expects PACKS-relative path (already includes group)
                        bundle_or_cfg = _load_from_packs_rel(rel)

                        # For logging parity with old API
                        task_ref["path"] = "/".join(parts[1:])
                    except FileNotFoundError as e:
                        return self._send_json(400, {"error": str(e)})
                    except Exception:
                        return self._send_json(400, {"error": "bad_path"})
                else:
                    # Legacy path: handle below via group-specific logic.
                    # Keep the original group, and move the overloaded value into body["path"].
                    body["path"] = req_path
                    req_path = ""

            if "bundle_or_cfg" in locals():
                # We already resolved the task via new path API.
                pass
            elif group == "TUTORIAL":
                stage = body.get("stage") or "STAGE-01"
                stage_dir = PACKS / "TUTORIAL" / stage
                task_ref.update({"stage": stage})
                if not stage_dir.exists() or not stage_dir.is_dir():
                    return self._send_json(400, {"error": "missing_tutorial_stage"})

                if seed is not None:
                    base_cfg = stage_dir / "base.cfg"
                    if not base_cfg.exists():
                        return self._send_json(400, {"error": "missing_base_cfg"})
                    selected_file = str(base_cfg.relative_to(ROOT))
                    bundle_or_cfg = _load_config_with_seed(_read_json(base_cfg), seed)
                else:
                    bundles = sorted([p for p in stage_dir.glob("*.json") if p.name != "base.cfg"])
                    if bundles:
                        p = rng.choice(bundles)
                        selected_file = str(p.relative_to(ROOT))
                        bundle_or_cfg = _read_json(p)
                    else:
                        base_cfg = stage_dir / "base.cfg"
                        if not base_cfg.exists():
                            return self._send_json(400, {"error": "missing_base_cfg"})
                        selected_file = str(base_cfg.relative_to(ROOT))
                        bundle_or_cfg = _read_json(base_cfg)

            elif group == "TRAINING":
                series = body.get("series") or "S0-keys"
                stage = body.get("stage") or "STAGE-01"
                task_ref.update({"series": series, "stage": stage})

                # replicate training rule here to record selected file
                stage_dir = PACKS / "TRAINING" / series / stage
                if seed is not None:
                    base_cfg = stage_dir / "base.cfg"
                    selected_file = str(base_cfg.relative_to(ROOT))
                    bundle_or_cfg = _load_config_with_seed(_read_json(base_cfg), seed)
                else:
                    bundles = sorted([p for p in stage_dir.glob("*.json") if p.name != "base.cfg"])
                    if bundles:
                        p = rng.choice(bundles)
                        selected_file = str(p.relative_to(ROOT))
                        bundle_or_cfg = _read_json(p)
                    else:
                        base_cfg = stage_dir / "base.cfg"
                        selected_file = str(base_cfg.relative_to(ROOT))
                        bundle_or_cfg = _read_json(base_cfg)

            else:
                # CLASSIC/EXTENDED: require a path to a leaf directory; pick a json inside it.
                rel = body.get("path") or ""
                leaf_dir = (PACKS / group / rel)
                if not leaf_dir.exists() or not leaf_dir.is_dir():
                    return self._send_json(400, {"error": "bad_path"})
                cand = sorted(leaf_dir.glob("*.json"))
                if cand:
                    p = rng.choice(cand)
                    selected_file = str(p.relative_to(ROOT))
                    bundle_or_cfg = _read_json(p)
                elif (leaf_dir / "base.cfg").exists():
                    p = leaf_dir / "base.cfg"
                    selected_file = str(p.relative_to(ROOT))
                    bundle_or_cfg = _read_json(p)
                else:
                    return self._send_json(400, {"error": "no_config_or_bundle"})

            # Note: TRAINING path already applied seed override using base.cfg.
            if group != "TRAINING":
                bundle_or_cfg = _load_config_with_seed(bundle_or_cfg, seed)

            # Keep a stable reference for logging.
            if selected_file:
                task_ref["source"] = selected_file
            elif isinstance(bundle_or_cfg, dict) and bundle_or_cfg.get("format") == "labyrinth.bundle.v1":
                task_ref["source"] = "(bundle-inline)"
            else:
                task_ref["source"] = "(config-inline)"

            loaded = load_or_generate(bundle_or_cfg)
            gen = loaded.generated

            fl = getattr(gen.config.systems, "flashlight", None)
            fl_enabled = bool(getattr(fl, "enabled", False))
            fl_charges = int(getattr(fl, "charges", 0) or 0)

            eng = Engine(
                gen.board,
                gen.start,
                lang=lang,
                require_exit_key_to_leave=gen.goal.require_exit_key_to_leave,
                grenades_enabled=bool(gen.config.systems.grenades.enabled),
                boats_enabled=bool(gen.config.systems.boats.enabled),
                flashlight_enabled=fl_enabled,
                flashlight_charges=fl_charges,
            )

            # Info panel (if not hidden) + debug mode
            meta = dict((loaded.config or {}).get("metadata") or {})
            hide_info = bool(meta.get("hide_info", False))
            debug_meta = bool(meta.get("debug", False))
            debug_effective = bool(SERVER_DEBUG or debug_meta)

            sid = secrets.token_urlsafe(16)
            now = time.time()
            max_steps = None
            try:
                max_steps = int(meta.get("max_steps")) if meta.get("max_steps") is not None else None
            except Exception:
                max_steps = None

            with SESSIONS_LOCK:
                SESSIONS[sid] = Session(
                    eng=eng,
                    gen=gen,
                    lang=lang,
                    debug=debug_effective,
                    created_at=now,
                    last_seen=now,
                    client=client,
                    task_ref=task_ref,
                    max_steps=max_steps,
                )

            # Dataset trace (optional): write META + initial ground-truth map and start position.
            if TRACE_DATASET:
                gt_map = render(gen.board, eng.state)

                meta = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "session_id": sid,
                    "client": client,
                    "agent": task_ref.get("agent"),
                    "task": task_ref,
                }
                meta_txt = json.dumps(meta, ensure_ascii=False)

                # Variant (2): seq trace (META once, then MAP once, then POS once, then ACT/POS pairs)
                _trace_append(sid, f"<META>\n{meta_txt}\n</META>\n")
                _trace_append(sid, f"<MAP>\n{gt_map}\n</MAP>\n")
                _trace_append(sid, f"<POS>({int(gen.start.pos[0])}, {int(gen.start.pos[1])})</POS>\n")

                # Variant (1): per-step trace (META once, then initial POS+MAP, then per-step ACT/POS/MAP)
                step_path = TRACE_DIR / "per_step"
                step_path.mkdir(parents=True, exist_ok=True)
                p0 = step_path / f"{sid}.trace.txt"
                with p0.open("a", encoding="utf-8") as f:
                    # Keep ordering consistent with per-step records: POS then MAP.
                    f.write(f"<META>\n{meta_txt}\n</META>\n")
                    f.write(f"<POS>({int(gen.start.pos[0])}, {int(gen.start.pos[1])})</POS>\n<MAP>\n{gt_map}\n</MAP>\n")

                # Mark started so we don't duplicate on first step.
                with SESSIONS_LOCK:
                    ss = SESSIONS.get(sid)
                    if ss:
                        ss.trace_started = True

            # Agent-visible text trace (optional)
            if TRACE_DATASET:
                meta = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "session_id": sid,
                    "client": client,
                    "agent": task_ref.get("agent"),
                    "task": task_ref,
                }
                meta_txt = json.dumps(meta, ensure_ascii=False)

                # START block as plain text (not JSON)
                start_lines = [
                    f"board: {int(gen.board.n)}x{int(gen.board.m)}",
                    f"start: ({int(gen.start.pos[0])}, {int(gen.start.pos[1])})",
                ]
                _trace_txt_append(sid, f"<META>\n{meta_txt}\n</META>\n")
                _trace_txt_append(sid, "<START>\n" + "\n".join(start_lines) + "\n</START>\n")

            pits_cycles = set()
            pits_cnt = 0
            pits_idx_max = 0
            rivers_sources = set()
            for pos, feats in gen.board.features.items():
                for f in feats:
                    nm = getattr(f, "name", "")
                    if nm == "pit":
                        pits_cnt += 1
                        pits_cycles.add(int(getattr(f, "cycle_id", 0)))
                        pits_idx_max = max(pits_idx_max, int(getattr(f, "idx_in_cycle", 0)) + 1)
                    if nm == "river" and getattr(f, "kind", None) == "source":
                        rivers_sources.add(pos)

            info = None
            if not hide_info:
                info = {
                    "pits_cycles": len(pits_cycles),
                    "pits_cycle_len": pits_idx_max if pits_cnt else 0,
                    "rivers": len(rivers_sources),
                }

            start_formal = {
                "status": "success",
                "observe": "empty",
                "events": [],
                "inventory": eng.snapshot().get("inventory"),
            }

            task_meta = {
                "max_steps": meta.get("max_steps"),
                "board": {"n": int(gen.board.n), "m": int(gen.board.m)},
                # Start position is not treated as a spoiler for human mode.
                "start": {"row": int(gen.start.pos[0]), "col": int(gen.start.pos[1])},
                "debug": bool(debug_effective),
            }

            return self._send_json(
                200,
                {
                    "id": sid,
                    "map": _map_for(gen=gen, eng=eng, debug=debug_effective),
                    "text": "Game has started.",
                    "inventory": eng.snapshot().get("inventory"),
                    "formal": start_formal,
                    "actions": list(
                        available_actions(
                            grenades_enabled=eng.grenades_enabled,
                            flashlight_enabled=getattr(eng, "flashlight_enabled", False),
                        )
                    ),
                    "info": info,
                    "task_meta": task_meta,
                },
            )

        if path == "/api/step":
            _gc_sessions()
            sid = body.get("id")
            action = body.get("action")
            seq = body.get("seq")  # optional client request sequence

            with SESSIONS_LOCK:
                s = SESSIONS.get(sid)
            if not s:
                return self._send_json(404, {"error": "no_session"})
            if not action:
                return self._send_json(400, {"error": "no_action"})

            # Serialize all state updates for a given session.
            with s.lock:
                s.last_seen = time.time()

                # Best-effort dedup/out-of-order protection.
                # - If client provides seq and we already processed >= that seq: return last response.
                # - If seq is older than last_seq: ignore safely (return last response).
                # This prevents accidental double-steps on retries without breaking the client.
                if seq is not None:
                    try:
                        seq_i = int(seq)
                    except Exception:
                        seq_i = None

                    if seq_i is not None and s.last_seq is not None and seq_i <= s.last_seq:
                        # Return the last known response (idempotent-ish behavior)
                        if isinstance(s.last_response, dict):
                            return self._send_json(200, s.last_response)

                # Validate action early to avoid crashing the engine on invalid inputs
                allowed = set(
                    available_actions(
                        grenades_enabled=s.eng.grenades_enabled,
                        flashlight_enabled=getattr(s.eng, "flashlight_enabled", False),
                    )
                )
                if action not in allowed:
                    ts = datetime.now(timezone.utc).isoformat()
                    thr = threading.current_thread().name
                    _append_step_log(
                        {
                            "ts": ts,
                            "phase": "reject",
                            "thread": thr,
                            "remote": f"{self.client_address[0]}:{self.client_address[1]}",
                            "session_id": sid,
                            "seq": seq,
                            "action": action,
                            "error": "bad_action",
                            "allowed": sorted(list(allowed)),
                        }
                    )
                    return self._send_json(400, {"error": "bad_action", "action": action, "allowed": sorted(list(allowed))})

                # Normal step
                s.steps_taken += 1
                step_idx = int(s.steps_taken)
                ts = datetime.now(timezone.utc).isoformat()
                thr = threading.current_thread().name
                _append_step_log(
                    {
                        "ts": ts,
                        "phase": "start",
                        "thread": thr,
                        "remote": f"{self.client_address[0]}:{self.client_address[1]}",
                        "session_id": sid,
                        "seq": seq,
                        "step_idx": step_idx,
                        "action": action,
                    }
                )

                # Ground-truth trace (optional)
                # Initial MAP/POS are written at /api/start.

                pre_pos = tuple(s.eng.state.pos)
                out = s.eng.step(action)

                done = "exited" in out.obs.events

                if TRACE_DATASET:
                    # If the agent exits, the engine state remains at the last in-bounds cell.
                    # For dataset traces we record an out-of-bounds position and a map without the agent.
                    drdc = {
                        "up": (-1, 0),
                        "down": (1, 0),
                        "left": (0, -1),
                        "right": (0, 1),
                    }
                    if done and action in drdc:
                        dr, dc = drdc[action]
                        pos_for_trace = (int(pre_pos[0] + dr), int(pre_pos[1] + dc))
                    else:
                        pos_for_trace = (int(s.eng.state.pos[0]), int(s.eng.state.pos[1]))

                    if done:
                        map_for_trace = render(s.gen.board, None)
                    else:
                        map_for_trace = render(s.gen.board, s.eng.state)

                    # Variant (2): append ACT/POS pairs
                    _trace_append(sid, f"<ACT>{action}</ACT>\n")
                    _trace_append(sid, f"<POS>({pos_for_trace[0]}, {pos_for_trace[1]})</POS>\n")

                    # Variant (1): write ACT+POS+MAP per step (ground truth)
                    step_path = TRACE_DIR / "per_step"
                    step_path.mkdir(parents=True, exist_ok=True)
                    p = step_path / f"{sid}.trace.txt"
                    with p.open("a", encoding="utf-8") as f:
                        f.write(
                            f"<ACT>{action}</ACT>\n"
                            f"<POS>({pos_for_trace[0]}, {pos_for_trace[1]})</POS>\n"
                            f"<MAP>\n{map_for_trace}\n</MAP>\n"
                        )

                if TRACE_DATASET:
                    # Agent-visible trace: action + the text observation the agent/human sees.
                    _trace_txt_append(sid, f"<ACT>{action}</ACT>\n")
                    _trace_txt_append(sid, f"<TXT>{out.text}</TXT>\n")

                # Decide terminal outcome for <END> markers (dataset traces only)
                terminal: Optional[str] = None
                if done:
                    terminal = "Win"
                elif s.max_steps is not None and int(s.steps_taken) >= int(s.max_steps):
                    terminal = "Fail"

                if terminal is not None and TRACE_DATASET and not s.trace_ended:
                    end_tag = f"<END>{terminal}</END>\n"
                    _trace_append(sid, end_tag)  # seq

                    per_step_path = TRACE_DIR / "per_step" / f"{sid}.trace.txt"
                    with per_step_path.open("a", encoding="utf-8") as f:
                        f.write(end_tag)

                    _trace_txt_append(sid, end_tag)
                    s.trace_ended = True

                if done and not s.logged_win:
                    _append_log(client=s.client, task_ref=s.task_ref, steps_taken=s.steps_taken, session_id=sid)
                    s.logged_win = True

                # Provide a structured observation for agents (no coordinates / ground truth).
                formal = {
                    "status": out.obs.status,
                    "observe": out.obs.observe,
                    "events": list(out.obs.events),
                    "inventory": out.obs.inventory,
                    "details": out.obs.details or {},
                }

                resp = {
                    "text": out.text,
                    "events": list(out.obs.events),
                    "status": out.obs.status,
                    "observe": out.obs.observe,
                    "inventory": s.eng.snapshot().get("inventory"),
                    "formal": formal,
                    "map": _map_for(gen=s.gen, eng=s.eng, debug=s.debug),
                    "done": done,
                }

                _append_step_log(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "phase": "end",
                        "thread": thr,
                        "remote": f"{self.client_address[0]}:{self.client_address[1]}",
                        "session_id": sid,
                        "seq": seq,
                        "step_idx": step_idx,
                        "done": bool(done),
                        "events": list(out.obs.events),
                    }
                )

                if seq is not None:
                    # Only update seq tracking if it parsed.
                    try:
                        s.last_seq = int(seq)
                        s.last_response = resp
                    except Exception:
                        pass

                return self._send_json(200, resp)

        return self._send_json(404, {"error": "not_found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", type=str, default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--debug", action="store_true", help="show full map (also enabled by metadata.debug)")
    ap.add_argument("--trace-dataset", action="store_true", help="write ground-truth MAP/ACT/POS traces to logs/dataset/")
    # Agent-visible text traces are enabled together with --trace-dataset.
    ap.add_argument("--steps-log", action="store_true", help="enable JSONL step debug log (logs/steps.log)")
    args = ap.parse_args()

    if not WEB.exists():
        raise SystemExit(f"Missing webui directory: {WEB}")

    global SERVER_DEBUG, TRACE_DATASET, STEPS_LOG_ENABLED
    SERVER_DEBUG = bool(args.debug)
    TRACE_DATASET = bool(getattr(args, "trace_dataset", False))
    STEPS_LOG_ENABLED = bool(getattr(args, "steps_log", False))

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"WebUI: http://{args.host}:{args.port}/ (debug={SERVER_DEBUG})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
