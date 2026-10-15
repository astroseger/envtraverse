# Labyrinth (paper game) — procedural generator + engine

## Goal
Procedurally generate "Labyrinth" boards (grid with walls, monolith border + one exit, rivers with current/forced displacement, keys/treasure, optional exit-key, pits/teleports, etc.), **validate solvability**, and estimate/target **difficulty**.

Later:
- game engine that simulates player actions via a clean API
- web version (humans + bots)

## Repo layout
- `src/labyrinth/` — library code (generator, rules, engine)
- `tests/` — unit/property tests
- `docs/` — specs and notes

## Next step
Implement an extensible generator skeleton with:
- core data model (grid, walls, objects)
- rule hooks for special cell types (river, pit, etc.)
- solvability checker (graph search over game-state)
- initial difficulty metrics
