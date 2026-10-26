# ARC3 image rendering

This reference describes the rendering used by
[arc3_minimal_img_wnoise3](README.md) and
[arc3_minimal_img_wnoise3_whighlight2](../arc3_minimal_img_wnoise3_whighlight2/README.md).
For accepted settings, see the [parameter tables](README.md#parameters). The
supplied [configuration](../../../../run_config_arc3_minimal_img_wnoise3.yaml)
contains the values used in the examples below.

## Color noise and sampling

Palette jitter starts from the original 16-color palette and never accumulates
across frames. Its uniform integer bounds and the standard deviation of Gaussian
pixel noise have different meanings, even when their values match. Clipping near
black and white changes the resulting noise distribution at those boundaries.

Each ordinary action produces one observation. Startup and RESET also produce
separate observations. Samples shared by `step` are discarded after rendering
that observation. There is no `episode` refresh mode and no seed parameter.
Each manager initializes its random generator from system entropy once, then
draws fresh samples according to the refresh settings.

With both noise groups enabled, rendering jitters and clips the palette,
converts the grid to RGB, and enlarges it by `image_scale`. It applies the
combined geometry to a padded canvas in one resampling pass. Gaussian noise is
then added independently at every output pixel and channel, including padding
and transform fill. Values are clipped to 0–255 and rounded to the nearest
integer before PNG or JPEG encoding. JPEG compression can smooth the noise.

With `noise_refresh: frame`, palette jitter and pixel noise are drawn separately
for every frame. With `noise_refresh: step`, frames in one observation share
the palette adjustment and one Gaussian field per output shape. Geometry has
its own refresh setting: `frame` samples every frame; `step` shares the
homography and normalized FFD controls across the observation.

## Homography and free-form deformation

The homography uses eight independent uniform draws: one rotation, one shear,
two scales, two translations, and two perspective coefficients. In centered
board coordinates its composition is
`translation @ rotation @ shear @ scale @ perspective`. The perspective
denominator is `1 + px*u + py*v`, where `u` and `v` range from -1 to 1 across the
board. Translation follows the other transforms, and positive rotation appears
clockwise.

FFD uses open-uniform, clamped cubic B-splines. Clamping describes the knots;
edge and corner control coefficients remain random, so borders can bend.
The displacement fields are added: `E = E_1 + E_2 + ...`. For an output
coordinate `p` with the canvas padding offset removed, the source coordinate is:

```text
source = H_inverse(p + E(p))
```

This is a backward sampling map: positive x displacement samples farther right,
making visible content move left. It produces homography followed by bending.
Each control's x and y coefficients are independent uniform draws bounded by
`amplitude_fraction` times the unpadded, enlarged image's shorter side.

SciPy computes spline basis weights and derivatives once per geometry plan;
NumPy evaluates the fields with separable matrix products. OpenCV
[`remap`](https://docs.opencv.org/4.x/da/d54/group__imgproc__transform.html)
samples the enlarged RGB image once. Basis and coordinate caching is bounded;
random control fields are never cached across observations.

The determinant test applies to the summed FFD, before homography. If it fails,
all active FFD grids are redrawn together, up to 100 attempts. Failure after
that raises a configuration/runtime error. The test uses analytic spline
derivatives sampled at pixel centers; it does not prove that the continuous
field has no folds. With `geometry_refresh: step`, one set of normalized
controls must pass for every distinct frame shape in the observation. Sampling
maps are reused for repeated shapes in that observation.

## Padding

Automatic padding uses the configured ranges, so its size does not change with
the sampled transform. It first bounds the homography of the source and its
interpolation support, then adds the sum of active FFD coefficient bounds.
B-spline weights are nonnegative and sum to one, so this sum bounds displacement
on each axis. A four-pixel safety margin is added, and the target longest canvas
side is rounded up to a multiple of 16 before choosing symmetric integer
padding. OpenCV requires transformed image dimensions below 32767 pixels;
oversized configurations are rejected.

All grids share a domain covering the automatically sized canvas. Larger manual
padding does not stretch this domain or alter the deformation; displacements
extend constantly outside it. For a 512×512 enlarged board, using the stored
transform settings in the supplied configuration:

| Enabled transforms | Padding per side | Output |
| --- | ---: | ---: |
| Both FFD grids | 88 px | 688×688 |
| Homography only | 144 px | 800×800 |
| Homography and both FFD grids | 224 px | 960×960 |
| Neither, with automatic padding | 0 | 512×512 |

`geometry_noise.enabled: false` forces zero padding. When geometry is enabled
but both transforms are inactive, explicitly requested manual padding is still
honored. Select PNG and disable both noise groups for an unchanged enlarged
image. Action coordinates always refer to the original 64×64 board.

## Cursor placement

The [highlight adapter](../arc3_minimal_img_wnoise3_whighlight2/README.md#observations-and-highlighting)
renders the cached observation's last frame twice. Each call samples fresh
geometry shared by the pair. Palette jitter and Gaussian noise are independent
between the images, regardless of the ordinary refresh settings. Disabled
groups still apply.

The center of source cell `(x, y)` in OpenCV's pixel-center coordinates is
`s = ((x + 0.5)*scale - 0.5, (y + 0.5)*scale - 0.5)`. The renderer solves
`p + E(p) = H(s)` for the unpadded output position using damped Newton steps
with analytic B-spline derivatives. If the initial solve fails, the closest
point in the sampling map supplies a new starting point. A checked residual
prevents silently placing the cursor at a failed solution.

Adding padding and `0.5` converts the result to the cursor's pixel-boundary
coordinates. Final sprite placement rounds to integer pixels with at most
half an output pixel of error per axis. The cursor has constant height and
orientation in output pixels. It is composited after geometry and color noise,
before encoding; JPEG compression also affects it. Parts outside the canvas
are clipped without increasing padding. Asset details and attribution are in
[the cursor README](../arc3_minimal_img_wnoise3_whighlight2/assets/README.md).
