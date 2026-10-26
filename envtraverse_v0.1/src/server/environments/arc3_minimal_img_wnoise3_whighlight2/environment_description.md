# Environment Description

## 1. General Description Of The Environment

You are playing a dynamic visual puzzle game. Your objective is to solve all levels using as few actions as possible.

## 2. Actions

Actions are JSON objects.

Simple actions:

```json
{"id":"ACTION1"}
{"id":"ACTION2"}
{"id":"ACTION3"}
{"id":"ACTION4"}
{"id":"ACTION5"}
{"id":"ACTION7"}
{"id":"RESET"}
```

Coordinate actions:

```json
{"id":"ACTION6","data":{"x":12,"y":34}}
{"id":"highlight_pixel","data":{"x":12,"y":34}}
```

General action conventions:

- `ACTION1`: usually direction-like, commonly up.
- `ACTION2`: usually direction-like, commonly down.
- `ACTION3`: usually direction-like, commonly left.
- `ACTION4`: usually direction-like, commonly right.
- `ACTION5`: game-specific interaction, such as use, select, rotate, attach, detach, confirm, or execute.
- `ACTION6`: coordinate-based interaction; provide `x` and `y`, with the origin at the top-left. The board size is 64x64.
- `highlight_pixel`: auxiliary action. Highlight the selected board pixel. Does not consume the game-solving budget.
- `ACTION7`: usually undo.
- `RESET`: reset the current level without resetting the action count.
