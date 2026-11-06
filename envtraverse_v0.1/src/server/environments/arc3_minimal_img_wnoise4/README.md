# ARC3 with image noise, deformation, and step rotation

ARC-AGI-3 with the image noise and deformation controls described in
[arc3_minimal_img_wnoise3](../arc3_minimal_img_wnoise3/README.md), plus a rotation
schedule applied before homography and free-form deformation (FFD). This page
covers the differences from v3: rotation settings, observation behavior, and padding.

The runner installs this adapter's dependencies in the server image. For a
direct server launch, install them from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_img_wnoise4/requirements.txt
~~~

Select `environment: arc3_minimal_img_wnoise4` and start with
[run_config_arc3_minimal_img_wnoise4.yaml](../../../../run_config_arc3_minimal_img_wnoise4.yaml).
It supplies every required image setting and forwards `ARC_API_KEY` to the
server. Export the key on the host. Normal mode can still need a key and
network access to download games.

The supplied configuration adds clockwise rotation of 2° per successful action,
starting at 0°, to the same color-noise and FFD settings as the v3 example.
It produces fixed 896×896 JPEG images from a 64×64 board at scale 8, with
192 pixels of padding on each side. These are explicit configuration choices,
not defaults.

## Parameters

The [v3 parameter reference](../arc3_minimal_img_wnoise3/README.md#parameters)
also applies here, with the additional required
`image_noise.geometry_noise.pre_rotation` group:

| Required `pre_rotation` parameter | Example | Meaning and accepted values |
| --- | --- | --- |
| `enabled` | `true` | Boolean switch for rotation about the board center. Requires `geometry_noise.enabled: true`; homography and FFD can be disabled independently. |
| `mode` | `linear` | `linear`, `random_increment`, or `full_random`; see the schedules below. |
| `initial_angle_deg` | `0` | Angle of the initial observation in degrees; any finite signed number. |
| `deg_per_step` | `2` | Angle added per successful action in `linear` mode; any finite signed number. |
| `increment_range_deg` | `[0, 5]` | Uniform increment range in degrees for `random_increment`: two finite numbers with `min <= max`. Negative increments are allowed. |

All five fields are required and validated, including fields for inactive modes
and disabled groups. When adapting a v3 configuration, change the environment
name and add this group. Set `pre_rotation.enabled: false` to use v3's
transform order and padding rules.

## Observations and rotation

The [v3 observation format and lifecycle](../arc3_minimal_img_wnoise3/README.md#observations-and-lifecycle)
also apply here. This manager supplies its own
[environment_description.md](environment_description.md). `ACTION6` coordinates
refer to the original 64×64 board. Rotation angles and transformation matrices
are not included in observation metadata.

The initial observation is step zero and uses `initial_angle_deg` without a
random draw. Subsequent angles depend on `mode`:

- `linear`: `angle(t) = initial_angle_deg + t * deg_per_step`.
- `random_increment`: add one uniform draw from `increment_range_deg` to the
  previous angle after each successful action. For example, `[0, 5]` advances
  clockwise; `[-5, 5]` allows rotation in either direction.
- `full_random`: draw a new independent angle uniformly in `[0, 360)` after
  each successful action.

Positive angles rotate clockwise. Every successful action, including `RESET`,
advances the sequence once. Level changes preserve it; invalid actions and
backend failures do not advance it. A new manager starts a new sequence.
Angles are stored modulo 360.

All intermediate and final frames in an observation share the scheduled angle,
even with `geometry_refresh: frame`. Homography can add its own random rotation
per frame. Rotation uses a separate random generator, so its draws are independent
of frame counts and other image noise. There is no seed parameter.

## Rendering differences

See the [v3 rendering reference](../arc3_minimal_img_wnoise3/rendering.md) for
shared color-noise, homography, and FFD mechanics, with these changes:

- **Transform order:** pre-rotation about the board center, then homography,
  then FFD. They are composed into one resampling pass. The backward sampling
  map becomes `source = R_inverse(H_inverse(p + E(p)))`, where `R` is the
  scheduled rotation.
- **Padding:** enabling pre-rotation reserves space for a full 360° turn,
  including at step zero, so the canvas size stays fixed as the angle changes.
  This also affects perspective validation: some settings accepted without
  rotation are invalid for rotating rectangular frames.

For a 64×64 board at scale 8, with the supplied FFD and resampling settings
and homography disabled:

| Enabled transforms | Padding per side | Output |
| --- | ---: | ---: |
| Pre-rotation and both FFD grids (supplied example) | 192 px | 896×896 |
| Both FFD grids, pre-rotation disabled | 88 px | 688×688 |
| Pre-rotation only | 112 px | 736×736 |

Setting `geometry_noise.enabled: false` disables rotation, homography, and FFD
and forces zero padding, producing 512×512 images at scale 8.

Run the image and integration checks from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_img_wnoise4/test/requirements.txt
python3 -B -m unittest discover -s src/server/environments/arc3_minimal_img_wnoise4/test -p 'test_*.py'
~~~

These tests use mocked game backends and cover rotation schedules, frame sharing,
resets, rendering, padding, and manager integration. PyYAML is needed to read the
test configuration; the runtime receives parsed parameters.
