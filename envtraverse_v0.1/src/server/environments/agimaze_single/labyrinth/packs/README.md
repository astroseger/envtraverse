# packs/

This directory is for *task packs* (saved labyrinth bundles) used for:
- training/progression for humans
- sanity checks for agents
- benchmark suites

## Calibration

### Training subset

The **TRAINING** subset is designed primarily for **development and calibration**, not for a public leaderboard-style challenge.

Key ideas:

- **Not a final benchmark.** TRAINING levels are intended to be comparatively simple and *representative*, so that:
  - humans can practice and learn mechanics;
  - agent developers can iterate quickly;
  - regressions are easy to spot.

- **Two step thresholds (soft + hard).** TRAINING packs typically use a deliberately *generous* step budget.
  This helps distinguish:
  - *qualitative failures* (the agent is stuck / does not understand the mechanic), vs
  - *quantitative slowness* (the agent solves, but inefficiently).

  Concretely:
  - **soft threshold** (“good”): a target step count for a clean solution;
  - **hard threshold** (“max steps”): exceeding it is treated as failure in calibration runs.

- **Calibration baseline = random agent.** The goal is *not* to make the random agent pass every level with equal probability.
  Instead, the intent is to make difficulty *approximately comparable* **relative to the random baseline**:
  - stages/sizes are selected and tuned so that the random agent’s success profile is reasonably smooth;
  - other agents can then be compared against the same calibrated difficulty to see whether they behave *meaningfully* better than random.

In other words, TRAINING is a **controlled reference distribution**: a stable substrate for iteration where “better than random” is interpretable.

#### Calibration table template (random baseline)

Fill these tables with results from `agents/benchmark_runner.py` + `agents/bench_report.py`.
Suggested fields:
- `soft_good_steps`: threshold for `good` (success within soft threshold)
- `hard_max_steps`: threshold for “must finish by” (exceeding -> fail)
- `success/good/late`: success rates for the **random agent**

##### S0-keys

| Stage | Size | soft_good_steps | hard_max_steps | success | good | late |
|------:|------|----------------:|---------------:|--------:|-----:|-----:|
| STAGE-01 | 3x3 | 35 | 70 | 24.3 | 4.5 | 19.8 |
| STAGE-02 | 3x3 | 35 | 70 | 16.7 | 2.2 | 14.5 |
| STAGE-03 | 4x4 | 80 | 160 | 9.2 | 1.3 | 7.9 |
| STAGE-04 | 3x3 | 35 | 70 | 14.2 | 1.1 | 13.1 |
| STAGE-05 | 4x4 | 80  | 160 | 4.6 | 0.3 | 4.3 |
| STAGE-06 | 4x5 | 100 | 200 | 1.9 | 0.2 | 1.7 |

##### S1-rivers

| Stage | Size | soft_good_steps | hard_max_steps | success | good | late |
|------:|------|----------------:|---------------:|--------:|-----:|-----:|
| STAGE-01 | 3x3 | 35 | 70 | 22.3 | 4.3 | 18.0 |
| STAGE-02 | 3x4 | 50 | 100 | 15.0 | 2.7 | 12.3 |
| STAGE-03 | 4x4 | 80 | 160 | 6.8 | 1.4 | 5.4 |
| STAGE-04 | 4x4 | 80 | 160 | 1.5 | 0.2 | 1.3 |
| STAGE-05 | 4x4 | 80 | 160 | 1.3 | 0.2 | 1.1 |
| STAGE-06 | 5x5 | 125 | 250 | 2.6 | 0.1 | 2.5 |

##### S2-pits

| Stage | Size | soft_good_steps | hard_max_steps | success | good | late |
|------:|------|----------------:|---------------:|--------:|-----:|-----:|
| STAGE-01 | 3x3 | 35 | 70 | 24.8 | 4.8 | 20.0 |
| STAGE-02 | 4x4 | 80 | 160 | 18.1 | 3.2 | 14.9 |
| STAGE-03 | 4x5 | 100 | 200 | 10.1 | 1.3 | 8.8 |
| STAGE-04 | 4x4 | 80 | 160 | 5.9 | 0.7 | 5.2 |
| STAGE-05 | 4x5 | 100 | 200 | 4.3 | 0.4 | 3.9 |
| STAGE-06 | 5x5 | 125 | 250 | 3.6 | 0.3 | 3.3 |

##### S3-full

| Stage | Size | soft_good_steps | hard_max_steps | success | good | late |
|------:|------|----------------:|---------------:|--------:|-----:|-----:|
| STAGE-01 | 3x4 | 50 | 100 | 20.6 | 4.5 | 16.1 |
| STAGE-02 | 4x4 | 80 | 160 | 7.3 | 1.6 | 5.7 |
| STAGE-03 | 4x5 | 100 | 200 | 3.2 | 0.4 | 2.8 |
| STAGE-04 | 5x5 | 125 | 250 | 4.2 | 0.4 | 3.8 |
| STAGE-05 | 5x5 | 125 | 250 | 3.2 | 0.2 | 3.0 |
| STAGE-06 | 4x6 | 125 | 250 | 1.5 | 0.2 | 1.3 |

---

## Recommended on-disk layout

Top-level groups:
- `TRAINING/` — small boards (3x3–5x5), explicit staged progression.
- `CLASSIC/` — classic rulesets (no pits/grenades/boats).
- `EXTENDED/` — advanced/extra systems (pits, grenades, boats, etc.).
- `HIDDEN/` — private/secret tasks.
- `DND/` — future roleplay-style tasks (separate group by design).

### TRAINING
TRAINING is a staged progression with a fixed directory layout.
We keep an explicit `STAGE-XX/` level. Each stage folder should contain multiple
bundles of the same difficulty and systems, e.g. 10 bundles.

Current layout:
- `TRAINING/S0-keys/STAGE-01..06/`
- `TRAINING/S1-rivers/STAGE-01..06/`
- `TRAINING/S2-pits/STAGE-01..06/`
- `TRAINING/S3-full/STAGE-01..06/`

Example:
`TRAINING/S1-rivers/STAGE-03/*.bundle.json`

### Bundle files
A bundle is a JSON file (format `labyrinth.bundle.v1`) produced by:

```bash
python3 scripts/save_bundle.py --config path/to/config.json --out out.bundle.json
```

We also recommend adding task metadata at the top level:
- `max_steps`: expected step limit for humans/agents.
- `id`: stable id/name.
- `tags`: free-form tags.

## Git policy
You can keep directories empty locally. Only bundles/metadata should be committed.
