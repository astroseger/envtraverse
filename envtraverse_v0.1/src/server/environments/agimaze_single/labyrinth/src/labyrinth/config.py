"""Config schema.

Goals:
- Reproducible generation with an explicit seed.
- Forward compatibility: adding new optional systems (rivers, pits, etc.)
  should not change old outputs when those systems are disabled.
- Multiple generation strategies selectable via config.

Principle used here:
- Use *separate RNG streams* per subsystem (walls/features/rivers/etc.)
  derived from a master seed + stable labels.
- Subsystems are enabled/disabled explicitly in config.

This avoids "adding one random() call changes everything" problems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .pits_gen import PitsConfig


@dataclass(frozen=True)
class BoatsConfig:
    enabled: bool = False
    strategy: str = "river-exit-v1"


@dataclass(frozen=True)
class GrenadesConfig:
    enabled: bool = False
    strategy: str = "seal-cell-v1"  # future: seal-region-v1
    extra_grenades: int = 1
    max_walls_to_add: int = 3


@dataclass(frozen=True)
class FlashlightConfig:
    """Flashlight: optional limited "look" actions.

    If enabled, player starts with `charges` flashlight charges.
    Each look_* action consumes 1 charge and reveals what would happen if the
    player attempted to step into that neighbor cell (without applying effects).

    If the config section is missing, flashlight is disabled (backward compatible).
    """

    enabled: bool = False
    charges: int = 10


@dataclass(frozen=True)
class RNGConfig:
    seed: int
    algorithm: str = "py-random-v1"  # reserved for future switch


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "basic-v1"  # future: other strategies
    params: Dict[str, Any] = None  # strategy-specific knobs (JSON-able)

    def __post_init__(self):
        object.__setattr__(self, "params", self.params or {})


@dataclass(frozen=True)
class RiversConfig:
    """River generation knobs.

    We keep a separate config object so that enabling/disabling rivers and
    choosing a river strategy is explicit and reproducible.

    density: approximate fraction of cells that are river cells (0..1).
    mean_len: approximate mean length of a river in cells.
    """

    enabled: bool = False
    strategy: str = "linear-v1"  # must be explicit in config when enabled

    density: float = 0.10
    mean_len: float = 1.0  # multiplier for effective length ~ ((n+m)/2) * mean_len

    scatter: float = 0.25  # random jitter for k: factor in [1-scatter, 1+scatter]

    # shape control
    straight_bias: float = 2.0  # weight multiplier for continuing in the same direction
    adj_penalty: float = 0.55  # penalty base in (0,1]; weight *= adj_penalty**adj_effective_neighbors

    # source placement control: bias sources to be far from existing rivers
    source_dist_power: float = 3.0  # weight *= (1 + dist_to_river)**source_dist_power

    allow_adjacent: bool = True  # whether river cells may touch side-by-side


@dataclass(frozen=True)
class WallsConfig:
    enabled: bool = True
    strategy: str = "degree-v1"

    # Stage-1 (fast) walls: only between non-river cells.
    # Target mean number of available directions from a non-river cell.
    # Range: [0,4], but borders/rivers will constrain achievable values.
    target_mean_degree: float = 2.4

    # Stage-1.5: additional walls between non-river cells using engine-based reachability
    # (allows alternative connectivity via rivers), while preserving "no sinks from start".
    nonriver_engine_enabled: bool = True

    # Stage-2 river-adjacent walls: add walls between river and non-river cells,
    # while preserving "no sinks from start" under the full movement rules.
    river_enabled: bool = True
    target_mean_river_walls: float = 1.5  # mean blocked river-to-nonriver edges per river cell


@dataclass(frozen=True)
class SystemsConfig:
    # Each system must be explicitly enabled to participate in generation.
    walls: WallsConfig = WallsConfig()
    keys: bool = True
    treasure: bool = True
    exit_key: bool = False

    rivers: RiversConfig = RiversConfig()
    pits: bool = False  # deprecated: use pits_cfg
    pits_cfg: PitsConfig = PitsConfig()

    grenades: GrenadesConfig = GrenadesConfig()
    boats: BoatsConfig = BoatsConfig()
    flashlight: FlashlightConfig = FlashlightConfig()


@dataclass(frozen=True)
class BoardConfig:
    n: int
    m: int


@dataclass(frozen=True)
class GameConfig:
    version: int = 1  # config schema version

    board: BoardConfig = None
    rng: RNGConfig = None
    strategy: StrategyConfig = StrategyConfig()
    systems: SystemsConfig = SystemsConfig()

    metadata: Dict[str, Any] = None  # arbitrary user notes; not used in RNG

    def __post_init__(self):
        if self.board is None:
            raise ValueError("board is required")
        if self.rng is None:
            raise ValueError("rng is required")
        object.__setattr__(self, "metadata", self.metadata or {})


def config_from_dict(d: Dict[str, Any]) -> GameConfig:
    # minimal manual parsing (keep deps at zero). We'll improve later.
    board = BoardConfig(**d["board"])
    rng = RNGConfig(**d["rng"])
    strategy = StrategyConfig(**d.get("strategy", {}))

    systems_d = dict(d.get("systems", {}))

    # walls
    walls_d = systems_d.get("walls", {})
    if isinstance(walls_d, bool):
        walls = WallsConfig(enabled=walls_d)
    else:
        walls = WallsConfig(**walls_d)
    systems_d["walls"] = walls

    rivers_d = systems_d.get("rivers", {})
    # Backward compat: allow bool for rivers.
    if isinstance(rivers_d, bool):
        rivers = RiversConfig(enabled=rivers_d)
    else:
        rivers = RiversConfig(**rivers_d)
    systems_d["rivers"] = rivers

    # pits: backward compat supports bool; new config is systems.pits_cfg
    pits_d = systems_d.get("pits_cfg", systems_d.get("pits", False))
    if isinstance(pits_d, bool):
        pits_cfg = PitsConfig(enabled=pits_d)
    else:
        pits_cfg = PitsConfig(**pits_d)
    systems_d["pits_cfg"] = pits_cfg

    # grenades: new optional system
    gren_d = systems_d.get("grenades", {})
    if isinstance(gren_d, bool):
        grenades = GrenadesConfig(enabled=gren_d)
    else:
        grenades = GrenadesConfig(**gren_d)
    systems_d["grenades"] = grenades

    # boats: new optional system
    boats_d = systems_d.get("boats", {})
    if isinstance(boats_d, bool):
        boats = BoatsConfig(enabled=boats_d)
    else:
        boats = BoatsConfig(**boats_d)
    systems_d["boats"] = boats

    # flashlight: new optional system
    fl_d = systems_d.get("flashlight", {})
    if isinstance(fl_d, bool):
        flashlight = FlashlightConfig(enabled=fl_d)
    else:
        flashlight = FlashlightConfig(**fl_d)
    systems_d["flashlight"] = flashlight

    systems = SystemsConfig(**systems_d)

    metadata = d.get("metadata", {})
    version = d.get("version", 1)
    return GameConfig(version=version, board=board, rng=rng, strategy=strategy, systems=systems, metadata=metadata)
