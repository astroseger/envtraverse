# ARC3 with image noise and deformation

ARC-AGI-3 with a minimal task description and image observations. Palette
jitter, Gaussian pixel noise, homography, and free-form deformation (FFD) can
be configured independently.
For experiment commands, see
[Running experiments](../../../../docs/running/README.md#arc-agi-3-observation-variants).

The runner installs this adapter's dependencies in the server image. For a
direct server launch, install them from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_img_wnoise3/requirements.txt
~~~

Select `environment: arc3_minimal_img_wnoise3` and start with
[run_config_arc3_minimal_img_wnoise3.yaml](../../../../run_config_arc3_minimal_img_wnoise3.yaml).
It supplies every required image setting and forwards `ARC_API_KEY` to the
server. Export the key on the host. Normal mode can still need a key and
network access to download games.

The supplied configuration enlarges 64×64 boards to 512×512, applies two FFD
grids, and produces 688×688 JPEG images with 88 pixels of padding on each side.
It uses palette jitter 5, pixel-noise standard deviation 20, JPEG quality 25,
and fresh noise and geometry for each frame. Homography is disabled. These
values are explicit configuration choices, not defaults.

## Parameters

Image parameters and every field in the noise groups are required, including
fields in disabled groups. The environment settings below have defaults.
Missing required fields, unknown names, and incorrect types are rejected
before backend initialization.

- `competition`: boolean, use competition mode (default `false`).
- `source_url`: nonblank string attached to competition scorecards (default
  `https://github.com/astroseger/arc-3-agents-baseline1`).
- `tags`: scorecard tags as a string or list of strings; `tag` is also accepted.
  Omission uses `["api-test"]`; an explicit value must contain a nonblank tag.
- `image_scale`: positive integer enlargement factor. A value of `8` turns a
  64×64 board into 512×512 before padding.
- `image_format`: `jpg` or `png`; `jpeg` is accepted as `jpg`. Values are
  case-insensitive.
- `image_quality`: integer 1–100, used when encoding JPEG.
- `image_noise`: mapping containing both groups below.

Specify only one of `tags` or `tag`. Strings are trimmed; other types are not
converted to strings.

| Required `image_noise.color_noise` parameter | Example | Meaning and accepted values |
| --- | --- | --- |
| `enabled` | `true` | Boolean switch for palette jitter and Gaussian pixel noise. |
| `palette_noise` | `5` | Uniform integer jitter in `[-value, +value]` independently for each palette color and RGB channel. All cells of the same color share the adjustment. Integer 0–255; zero disables jitter. |
| `pixel_noise_std` | `20` | Standard deviation of independent zero-mean Gaussian noise per output pixel and RGB channel, applied after the geometric transform across the entire canvas, including padding. Measured in RGB units on the 0–255 scale; any finite nonnegative number. Zero disables noise. |
| `noise_refresh` | `frame` | `frame` draws palette jitter and pixel noise separately for every frame; `step` shares one palette and one Gaussian field per output shape across frames in an observation. |

| Required `image_noise.geometry_noise` parameter | Example | Meaning and accepted values |
| --- | --- | --- |
| `enabled` | `true` | Boolean switch for all geometry and padding. When false, padding is always zero. |
| `homography` | Settings below | Required homography settings and independent `enabled` switch. |
| `ffd` | Settings below | Required FFD settings and independent `enabled` switch. |
| `padding` | `auto` | Pixels per side, calculated from every permitted combined transform or set explicitly. Manual padding must be large enough to avoid cropping. Ignored when the geometry group is disabled. |
| `resampling` | `bilinear` | Image interpolation for the composed map: `nearest`, `bilinear`, or `bicubic`. Initial upscaling repeats pixels exactly. |
| `geometry_refresh` | `frame` | `frame` draws homography and all FFD controls separately for every frame; `step` shares the homography and normalized FFD controls across the observation. |

| Required `image_noise.geometry_noise.homography` parameter | Example | Meaning and accepted values |
| --- | --- | --- |
| `enabled` | `false` | Boolean switch for homography, independent of FFD. |
| `rotation_deg` | `5` | One rotation angle sampled uniformly within ±value degrees; finite and nonnegative. |
| `shear_deg` | `5` | One x shear angle sampled uniformly within ±value degrees; coefficient is `tan(angle)`. Finite, `0 <= value < 45`. |
| `scale_delta` | `0.075` | Two independent axis scales sampled uniformly within `[1-value, 1+value]`; finite, `0 <= value < 1`. |
| `translation_delta` | `0.02` | Two independent offsets sampled within ±value times the enlarged board width/height. Finite and nonnegative. |
| `perspective_delta` | `0.075` | Two independent perspective coefficients sampled uniformly within ±value. Coordinates are normalized to ±1 about the board center. Finite, `0 <= value < 0.5`; interpolation support must also fit on the positive side of the projective denominator. |

| Required `image_noise.geometry_noise.ffd` parameter | Example | Meaning and accepted values |
| --- | --- | --- |
| `enabled` | `true` | Boolean switch for all FFD grids. |
| `grids` | 4×4 and 8×8 grids | Any number of grids. An empty list disables FFD. |
| `minimum_sampled_jacobian` | `0.1` | Finite number in `(0, 1]`. Minimum accepted determinant of the summed FFD pull map, checked at every output pixel center. |

Each entry in `ffd.grids` requires:

- `grid_shape`: `[rows, columns]`, two integers each at least 4. Rectangular
  grids and repeated grid shapes are supported.
- `amplitude_fraction`: finite nonnegative number. Each control's x/y components
  are initially independent uniform draws within ±this fraction of the unpadded,
  enlarged image's shorter side. Zero disables that grid.

Setting `color_noise.enabled: false` disables palette jitter and Gaussian noise.
Setting `geometry_noise.enabled: false` disables both transforms and forces zero
padding, even when manual padding is configured. Keep all fields in disabled
groups. To obtain the unchanged enlarged image, disable both groups and select
PNG.

See [Image rendering](rendering.md) for sampling, transform composition,
padding calculations, and deformation validation.

## Observations and lifecycle

The manager supplies [environment_description.md](environment_description.md).
Each observation contains `metadata.txt`, with JSON game state, level counts,
available actions, and action input; `final_frame.jpg`; and any
`intermediate_frame_00.jpg` and subsequent numbered frames. Image filenames use
`.png` when `image_format: png`. `ACTION6` coordinates refer to the original
64×64 board. Image settings and transformation matrices are not included in
observation metadata.

The constructor validates configuration and creates a random generator from
system entropy. There is no seed parameter. Each manager owns one game and
initializes the backend in `start_environment()`. `stop()` releases the wrapper
and finalizes the game's competition scorecard once, including after a startup
failure. It is safe to call before startup or repeatedly.

Scorecard output paths and private server diagnostics follow the
[ARC3 adapter](../arc3/README.md). Rendering is implemented in
[artifacts_preprocessing.py](artifacts_preprocessing.py) and [ffd.py](ffd.py);
the shared server handles observation encoding and response caching.

Run the image and integration checks from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_img_wnoise3/test/requirements.txt
python3 -B -m unittest discover -s src/server/environments/arc3_minimal_img_wnoise3/test -p 'test_*.py'
~~~

These tests use mocked game backends. PyYAML is needed to read the test
configuration; the runtime receives parsed parameters.
