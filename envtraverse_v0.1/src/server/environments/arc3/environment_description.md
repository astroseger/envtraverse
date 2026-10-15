# Environment Description

## 1. General Description Of The Environment

You are playing a dynamic visual puzzle game. Your objective is to reach `WIN`, avoid `GAME_OVER`, and use as few actions as possible.

The game consists of levels. Levels usually form a curriculum: later levels often extend earlier mechanics rather than replacing them. If you reach `GAME_OVER`, use `RESET` to begin another attempt at the current level. `RESET` counts toward the action budget and does not reset the current level's action count, so avoid using it unless the state is `GAME_OVER`. The game is deterministic: every attempt starts from exactly the same state, so the same action sequence produces exactly the same results. You cannot return to already solved levels.

The game is shown as a `64 x 64` visual frame made from color indices `0..15`.

A single action may produce multiple frames. When this happens, the last frame is the final settled state, but the intermediate frames are important evidence. Multiple frames usually mean something meaningful happened, such as animation, collision, damage, item use, state transition, level completion, or failure. Explore such animations with your own eyes.

This is a visual puzzle. ASCII frames are exact data, but PNG frames show the game more like a human would see it. Inspect PNG files with your own eyes.

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

Coordinate action:

```json
{"id":"ACTION6","data":{"x":12,"y":34}}
```

General action conventions:

- `RESET`: start or restart play.
- `ACTION1`: usually direction-like, commonly up.
- `ACTION2`: usually direction-like, commonly down.
- `ACTION3`: usually direction-like, commonly left.
- `ACTION4`: usually direction-like, commonly right.
- `ACTION5`: game-specific interaction, such as use, select, rotate, attach, detach, confirm, or execute.
- `ACTION6`: coordinate-based interaction; provide `x` and `y`, with origin at the top-left.
- `ACTION7`: usually undo.

These are only conventions. Infer the real effect of each action from observations. The valid actions can change during play; check `available_actions` in metadata before choosing.

## 3. Provided Artifacts

Each observation step may provide:

- `metadata.txt`: JSON metadata with current state, level progress, available actions, and the action that produced the observation.
- `final.txt`: the final settled visual frame as a `64 x 64` ASCII grid of hexadecimal color indices.
- `final.png`: PNG rendering of `final.txt`.
- `intermediate_XX.txt`: optional intermediate visual frame from an animation or transition.
- `intermediate_XX.png`: PNG rendering of the corresponding `intermediate_XX.txt`.

ASCII files represent the exact visual frame data. PNG files are generated from the corresponding ASCII frames and are provided so you can see the game as humans are meant to see it.

## 4. Provided Helper Programs

Use `ascii_to_png.py` to convert any ASCII frame dump into a PNG image:

The same file also provides reusable functions for scripts and notebooks:

- `parse_ascii_grid(path)`: read an ASCII frame dump into a NumPy array.
- `frame_to_rgb_array(frame, scale=8)`: convert a NumPy frame array to an RGB NumPy array.
- `convert_ascii_to_png(input_path, output_path, scale)`: convert an ASCII frame file directly to PNG.

Use these helpers for simulated, reconstructed, or extracted ASCII frames when you need to inspect them visually. PNG inspection is especially useful for understanding object identity, animations, small visual differences, and mismatches between prediction and reality.
