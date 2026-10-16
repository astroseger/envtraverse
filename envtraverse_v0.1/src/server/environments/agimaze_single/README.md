# Single labyrinth

For experiment commands, see
[Running experiments](../../../../docs/running/README.md#agi-maze-individual-labyrinths).

The bundled labyrinth engine and game packs are under `labyrinth/`. This adapter
uses Python's standard library. Each manager owns one game and one local engine
process, created during `start_environment()` and released by `stop()`.
Startup failure also cleans up the child process.

Game IDs are paths relative to the engine's packs directory, for example
`TRAINING/S0-keys/STAGE-01/0000.json`.

Parameters:

- `port`: fixed engine port (default 5000).
- `lang`: description language (default `en`).
- `seed`: optional integer seed.

Each start result includes a description generated from the game's rules,
initial public map, task information, and actions. Observations contain
`state.txt`, with JSON fields `text` and `inventory`. Backend HTTP operations
use the shared one-hour `ENVIRONMENT_OPERATION_TIMEOUT_SECONDS`. Engine startup
still has a 10-second readiness window with one-second probes. `stop()`
terminates the child and kills it if it does not exit within two seconds.
The set environment inherits these timeouts through its child managers.
