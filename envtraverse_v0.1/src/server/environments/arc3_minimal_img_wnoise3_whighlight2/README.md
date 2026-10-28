# ARC3 with image noise and pixel highlights

ARC-AGI-3 with the same image noise and deformation settings as
[the image variant](../arc3_minimal_img_wnoise3/README.md), plus an auxiliary
action that marks a selected board cell with a cursor.
For experiment commands, see
[Running experiments](../../../../docs/running/README.md#arc-agi-3-observation-variants).

The runner installs this adapter's dependencies in the server image. For a
direct server launch, install them from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_img_wnoise3_whighlight2/requirements.txt
~~~

Select `environment: arc3_minimal_img_wnoise3_whighlight2` and start with
[run_config_arc3_minimal_img_wnoise3_whighlight2.yaml](../../../../run_config_arc3_minimal_img_wnoise3_whighlight2.yaml).
It supplies every required image setting and forwards `ARC_API_KEY` to the
server. Export the key on the host. Normal mode can still need a key and
network access to download games.

## Parameters

All parameters, defaults, and validation rules match the
[image variant](../arc3_minimal_img_wnoise3/README.md#parameters), with the
additional required `mouse_size` parameter. Every image and noise field must
be present, including fields in disabled groups.

| Required setting | Supplied example | Meaning |
| --- | --- | --- |
| `image_scale` | `8` | Positive integer enlargement factor before padding |
| `image_format` | `jpg` | `jpg` or `png`; `jpeg` is accepted as `jpg` |
| `image_quality` | `25` | Integer 1–100, used when encoding JPEG |
| `mouse_size` | `48` | Nonnegative integer cursor height in output pixels; aspect ratio is preserved. Zero hides the cursor but still returns both images. |
| `image_noise.color_noise` | Palette jitter `5`, pixel-noise standard deviation `20`, refresh `frame` | Required color-noise settings described in the linked parameter tables |
| `image_noise.geometry_noise` | Two FFD grids, homography disabled, automatic padding, bilinear resampling, refresh `frame` | Required geometry settings described in the linked parameter tables |

The supplied configuration produces 688×688 images with 88 pixels of padding
per side from a 64×64 board. Its values are explicit choices, not defaults.
Disabling geometry forces zero padding. Disabling both noise groups and
selecting PNG preserves the enlarged board image.

## Observations and highlighting

The manager supplies [environment_description.md](environment_description.md).
Ordinary actions return `metadata.txt`, `final_frame.jpg`, and any
`intermediate_frame_00.jpg` and subsequent numbered frames. Metadata contains
JSON game state, level counts, available actions, and action input. Images use
`.png` when `image_format: png`.

The auxiliary action is:

```json
{"id":"highlight_pixel","data":{"x":12,"y":34}}
```

Both `highlight_pixel` and `ACTION6` use coordinates on the original 64×64
board. A highlight reads the cached observation's last frame and returns:

- `frame.jpg`: the board with the configured noise and geometry.
- `frame_highlighted.jpg`: the board with the same geometry, independently
  sampled color noise, and a cursor at the selected cell.
- `metadata.txt`: game metadata with the requested highlight coordinates;
  available actions include `highlight_pixel`.

Each highlight call draws fresh geometry shared within that pair and independent
color noise for each image, regardless of the refresh settings. Disabled groups
still apply. A highlight does not reuse an earlier encoded image or persist
into later observations.

Highlighting leaves the cached frame and game counters unchanged and consumes
no game-solving budget. It advances the server's observation index. Highlights
are accepted during `GAME_OVER`, but rejected after the manager becomes
terminal or closes. The analysis tools exclude highlights from game-step counts
and scorecard replay.

The cursor is composited after geometry and color noise, before image encoding.
Its height and orientation are fixed in output pixels; parts outside the canvas
are clipped. See [cursor attribution](assets/README.md) for the asset's source
and license, and [Image rendering](../arc3_minimal_img_wnoise3/rendering.md#cursor-placement)
for its positioning under deformation. Image settings and transformation
matrices are not included in observation metadata.

Each manager owns one game and creates a random generator from system entropy;
there is no seed parameter. `start_environment()` initializes the backend.
`stop()` releases the wrapper and finalizes the game's competition scorecard
once, including after a startup failure. It is safe to call before startup or
repeatedly. Scorecard output paths and private server diagnostics follow the
[ARC3 adapter](../arc3/README.md).

Run the image and integration checks from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_img_wnoise3_whighlight2/test/requirements.txt
python3 -B -m unittest discover -s src/server/environments/arc3_minimal_img_wnoise3_whighlight2/test -p 'test_*.py'
~~~

These tests use mocked game backends and cover rendering, cursor positioning,
and highlight behavior. PyYAML is needed to read the test configuration; the
runtime receives parsed parameters.
