# Labyrinth sets (v1)

Each game is an ordered sequence of labyrinths from `sets/<game_id>.json`.
The 233 bundled set definitions and the short `environment_description.md` are
copied from `lab_general_baseline1_v2.11c`. The engine and maze packs are reused
from `../agimaze_single/labyrinth/`; they are not duplicated here.

For example, in `run_config.yaml`:

~~~yaml
environment: agimaze_set_v1
games: [set01]
environment_parameters: {}
environment_variables: []
~~~

`set01` contains 56 labyrinths. Smaller sets such as
`ts0_TRAINING_S0-keys_STAGE-01` contain seven tutorial labyrinths followed by one
training labyrinth. The `game_id` is the set filename without `.json`.

Parameters are forwarded to `agimaze_single`: optional `port` (default
5000), `lang` (default `en`), and `seed`. No API key is required.

## Interaction

- Starting a set returns the fixed environment description and `message.md`.
  No maze subprocess is started yet. Send `{"id":"start"}` to begin the first
  labyrinth.
- Starting each labyrinth returns `state.txt` and `labyrinth_description.md`.
  Read the latter for that labyrinth's rules and initial position. The global
  `environment_description.md` does not change.
- During play, actions are delegated to the single-labyrinth game. `state.txt`
  contains JSON fields `text`, `inventory`, and zero-based `labyrinth_index`.
  Tutorial labyrinths also return `map.txt` at the start and after ordinary moves.
- Solving a labyrinth or exhausting its action budget closes its subprocess and
  returns only `message.md`. The budget is five times the maze's `max_steps`, as
  in `agimaze_single`; the `start` action does not consume this budget.
- If more labyrinths remain, the message requests another `{"id":"start"}`.
  Only finishing the final labyrinth makes the whole set terminal, whether that
  final labyrinth was solved or its budget was exhausted.

The server/client handle artifact naming and storage normally. A `start` is a
regular action in the server's global step count and action logs.

## Set definitions and resource ownership

A definition is a nonempty JSON list of objects with `path` (relative to the
shared engine's packs directory) and optional boolean `tutorial` (default
`false`). Entries are played in their listed order. The bundled tutorial flags
match the maze packs' public-map settings.

One manager owns the entire set as a single logical game. It creates a fresh
`agimaze_single.EnvironmentManager` for each maze and stops it before advancing.
At most one child is active, so every maze reuses the configured backend port.
`stop()` closes the active child, including on shutdown or after a failed start.
Starting a second set on the same manager is rejected.

See the [analysis guide](../../../../analysis_scripts/README.md#agi-maze-sets)
for curriculum reports and replay of the final maze.
