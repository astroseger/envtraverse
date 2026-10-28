from __future__ import annotations

from dataclasses import dataclass, fields
from functools import lru_cache
from io import BytesIO
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from . import ffd

HIGHLIGHT_ACTION_NAME = "highlight_pixel"
CURSOR_ASSET_DIR = Path(__file__).resolve().parent / "assets"

COLOR_MAP: dict[int, str] = {
    0: "#FFFFFFFF",
    1: "#CCCCCCFF",
    2: "#999999FF",
    3: "#666666FF",
    4: "#333333FF",
    5: "#000000FF",
    6: "#E53AA3FF",
    7: "#FF7BCCFF",
    8: "#F93C31FF",
    9: "#1E93FFFF",
    10: "#88D8F1FF",
    11: "#FFDC00FF",
    12: "#FF851BFF",
    13: "#921231FF",
    14: "#4FCC30FF",
    15: "#A356D6FF",
}
RESAMPLING_METHODS = {
    "nearest": cv2.INTER_NEAREST,
    "bilinear": cv2.INTER_LINEAR,
    "bicubic": cv2.INTER_CUBIC,
}


def validate_image_scale(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("Environment manager parameter 'image_scale' must be a positive integer.")
    if value < 1:
        raise ValueError("Environment manager parameter 'image_scale' must be a positive integer.")
    return value


def validate_image_format(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("Environment manager parameter 'image_format' must be 'jpg' or 'png'.")
    value = value.strip().lower()
    if value == "jpeg":
        value = "jpg"
    if value not in ("jpg", "png"):
        raise ValueError("Environment manager parameter 'image_format' must be 'jpg' or 'png'.")
    return value


def validate_image_quality(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("Environment manager parameter 'image_quality' must be an integer from 1 to 100.")
    if not 1 <= value <= 100:
        raise ValueError("Environment manager parameter 'image_quality' must be an integer from 1 to 100.")
    return value


def validate_mouse_size(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("Environment manager parameter 'mouse_size' must be a nonnegative integer.")
    if value < 0:
        raise ValueError("Environment manager parameter 'mouse_size' must be a nonnegative integer.")
    return value


def _require_fields(raw: Any, parameter_class: type, path: str) -> None:
    if not isinstance(raw, dict):
        raise TypeError(f"{path} must be a dictionary.")
    names = {item.name for item in fields(parameter_class)}
    unknown = raw.keys() - names
    if unknown:
        raise ValueError(f"Unknown {path} parameters: {', '.join(sorted(repr(key) for key in unknown))}")
    missing = names - raw.keys()
    if missing:
        raise ValueError(f"Missing required {path} parameters: {', '.join(sorted(missing))}")


def _validate_magnitude(path: str, value: Any, upper_bound: float | None = None) -> None:
    if type(value) not in (int, float):
        raise TypeError(f"{path} must be a finite number.")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or (upper_bound is not None and value >= upper_bound):
        limit = "" if upper_bound is None else f" and less than {upper_bound}"
        raise ValueError(f"{path} must be finite, nonnegative{limit}.")


@dataclass(frozen=True)
class ColorNoiseParameters:
    enabled: bool
    palette_noise: int
    pixel_noise_std: float
    noise_refresh: str

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("image_noise.color_noise.enabled must be a boolean.")
        if type(self.palette_noise) is not int:
            raise TypeError("image_noise.color_noise.palette_noise must be an integer from 0 to 255.")
        if not 0 <= self.palette_noise <= 255:
            raise ValueError("image_noise.color_noise.palette_noise must be an integer from 0 to 255.")
        _validate_magnitude("image_noise.color_noise.pixel_noise_std", self.pixel_noise_std)
        if self.noise_refresh not in ("step", "frame"):
            raise ValueError("image_noise.color_noise.noise_refresh must be 'step' or 'frame'.")

    @classmethod
    def from_dict(cls, raw: Any) -> "ColorNoiseParameters":
        _require_fields(raw, cls, "image_noise.color_noise")
        return cls(**raw)


@dataclass(frozen=True)
class HomographyParameters:
    enabled: bool
    rotation_deg: float
    shear_deg: float
    scale_delta: float
    translation_delta: float
    perspective_delta: float

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("image_noise.geometry_noise.homography.enabled must be a boolean.")
        for name, upper_bound in (
            ("rotation_deg", None), ("shear_deg", 45),
            ("scale_delta", 1), ("translation_delta", None), ("perspective_delta", 0.5),
        ):
            _validate_magnitude(f"image_noise.geometry_noise.homography.{name}", getattr(self, name), upper_bound)

    @classmethod
    def from_dict(cls, raw: Any) -> "HomographyParameters":
        _require_fields(raw, cls, "image_noise.geometry_noise.homography")
        return cls(**raw)

    @property
    def has_geometry(self) -> bool:
        return self.enabled and bool(
            self.rotation_deg or self.shear_deg or self.scale_delta
            or self.translation_delta or self.perspective_delta
        )


@dataclass(frozen=True)
class FFDGridParameters:
    grid_shape: tuple[int, int]
    amplitude_fraction: float

    def __post_init__(self) -> None:
        if (not isinstance(self.grid_shape, (list, tuple)) or len(self.grid_shape) != 2
                or any(type(size) is not int or size < 4 for size in self.grid_shape)):
            raise ValueError(
                "image_noise.geometry_noise.ffd.grids[].grid_shape must contain "
                "two integers >= 4, in [rows, columns] order."
            )
        object.__setattr__(self, "grid_shape", tuple(self.grid_shape))
        _validate_magnitude("image_noise.geometry_noise.ffd.grids[].amplitude_fraction", self.amplitude_fraction)

    @classmethod
    def from_dict(cls, raw: Any) -> "FFDGridParameters":
        _require_fields(raw, cls, "image_noise.geometry_noise.ffd.grids[]")
        return cls(**raw)


@dataclass(frozen=True)
class FFDParameters:
    enabled: bool
    grids: tuple[FFDGridParameters, ...]
    minimum_sampled_jacobian: float

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("image_noise.geometry_noise.ffd.enabled must be a boolean.")
        if not isinstance(self.grids, (list, tuple)) or any(not isinstance(g, FFDGridParameters) for g in self.grids):
            raise TypeError("image_noise.geometry_noise.ffd.grids must be a list of grid parameters.")
        object.__setattr__(self, "grids", tuple(self.grids))
        _validate_magnitude("image_noise.geometry_noise.ffd.minimum_sampled_jacobian", self.minimum_sampled_jacobian)
        if not 0 < self.minimum_sampled_jacobian <= 1:
            raise ValueError("image_noise.geometry_noise.ffd.minimum_sampled_jacobian must be in (0, 1].")

    @classmethod
    def from_dict(cls, raw: Any) -> "FFDParameters":
        _require_fields(raw, cls, "image_noise.geometry_noise.ffd")
        if not isinstance(raw["grids"], (list, tuple)):
            raise TypeError("image_noise.geometry_noise.ffd.grids must be a list.")
        return cls(
            enabled=raw["enabled"], grids=tuple(FFDGridParameters.from_dict(g) for g in raw["grids"]),
            minimum_sampled_jacobian=raw["minimum_sampled_jacobian"],
        )

    @property
    def active_grids(self) -> tuple[FFDGridParameters, ...]:
        return tuple(g for g in self.grids if g.amplitude_fraction > 0) if self.enabled else ()


@dataclass(frozen=True)
class GeometryNoiseParameters:
    enabled: bool
    homography: HomographyParameters
    ffd: FFDParameters
    padding: str | int
    resampling: str
    geometry_refresh: str

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("image_noise.geometry_noise.enabled must be a boolean.")
        if self.padding != "auto" and (type(self.padding) is not int or self.padding < 0):
            raise ValueError("image_noise.geometry_noise.padding must be 'auto' or a nonnegative integer per side.")
        if not isinstance(self.resampling, str) or self.resampling not in RESAMPLING_METHODS:
            raise ValueError("image_noise.geometry_noise.resampling must be 'nearest', 'bilinear', or 'bicubic'.")
        if self.geometry_refresh not in ("step", "frame"):
            raise ValueError("image_noise.geometry_noise.geometry_refresh must be 'step' or 'frame'.")

    @classmethod
    def from_dict(cls, raw: Any) -> "GeometryNoiseParameters":
        _require_fields(raw, cls, "image_noise.geometry_noise")
        return cls(
            enabled=raw["enabled"], homography=HomographyParameters.from_dict(raw["homography"]),
            ffd=FFDParameters.from_dict(raw["ffd"]), padding=raw["padding"],
            resampling=raw["resampling"], geometry_refresh=raw["geometry_refresh"],
        )

    @property
    def has_homography(self) -> bool:
        return self.enabled and self.homography.has_geometry

    @property
    def active_grids(self) -> tuple[FFDGridParameters, ...]:
        return self.ffd.active_grids if self.enabled else ()

    @property
    def has_geometry(self) -> bool:
        return self.has_homography or bool(self.active_grids)

    def _minimum_padding_for(self, shape: tuple[int, int], image_scale: int) -> int:
        """Bound homography support, then expand by the summed FFD displacement."""
        if not self.has_geometry:
            return 0
        height, width = (dimension * image_scale for dimension in shape)
        radius = {"nearest": 1, "bilinear": 1, "bicubic": 2}[self.resampling]
        extent = float(radius)
        if self.has_homography:
            h = self.homography
            source_width, source_height = width + 2 * radius, height + 2 * radius
            denominator = 1 - h.perspective_delta * (source_width / width + source_height / height)
            if denominator <= 0:
                raise ValueError("image_noise.geometry_noise.homography.perspective_delta is too large for the frame size and interpolation support.")
            shear = math.tan(math.radians(h.shear_deg))
            extent_x = (1 + h.scale_delta) * (source_width + shear * source_height)
            extent_y = (1 + h.scale_delta) * source_height
            angle_limit = math.radians(min(h.rotation_deg, 90))
            angle_x = min(angle_limit, math.atan2(extent_y, extent_x))
            angle_y = min(angle_limit, math.atan2(extent_x, extent_y))
            bound_x = (extent_x * math.cos(angle_x) + extent_y * math.sin(angle_x)) / denominator
            bound_y = (extent_y * math.cos(angle_y) + extent_x * math.sin(angle_y)) / denominator
            bound_x += 2 * h.translation_delta * width
            bound_y += 2 * h.translation_delta * height
            if not math.isfinite(bound_x) or not math.isfinite(bound_y):
                raise ValueError("image_noise geometry ranges produce nonfinite image bounds.")
            extent = max(0, (bound_x - width) / 2, (bound_y - height) / 2)
        extent += sum(g.amplitude_fraction for g in self.active_grids) * min(width, height)
        if not math.isfinite(extent):
            raise ValueError("image_noise geometry ranges produce nonfinite image bounds.")
        return math.ceil(extent) + 4

    def automatic_padding_for(self, shape: tuple[int, int], image_scale: int) -> int:
        """Use this same domain even when a larger manual canvas is requested."""
        minimum = self._minimum_padding_for(shape, image_scale)
        if not minimum:
            return 0
        longest_side = max(shape) * image_scale
        canvas = math.ceil((longest_side + 2 * minimum) / 16) * 16
        return math.ceil((canvas - longest_side) / 2)

    def padding_for(self, shape: tuple[int, int], image_scale: int) -> int:
        if not self.enabled:
            return 0
        minimum = self._minimum_padding_for(shape, image_scale)
        if self.padding != "auto":
            if self.padding < minimum:
                raise ValueError(
                    f"image_noise.geometry_noise.padding must be at least {minimum} pixels per side "
                    "for the configured image_scale and distortion ranges; use 'auto' to calculate it."
                )
            padding = self.padding
        else:
            padding = self.automatic_padding_for(shape, image_scale)
        if self.has_geometry and max(shape) * image_scale + 2 * padding >= 32767:
            raise ValueError("Geometrically transformed image dimensions must be below 32767 pixels for OpenCV remap.")
        return padding


@dataclass(frozen=True)
class ImageNoiseParameters:
    color_noise: ColorNoiseParameters
    geometry_noise: GeometryNoiseParameters

    @classmethod
    def from_dict(cls, raw: Any) -> "ImageNoiseParameters":
        _require_fields(raw, cls, "image_noise")
        return cls(
            color_noise=ColorNoiseParameters.from_dict(raw["color_noise"]),
            geometry_noise=GeometryNoiseParameters.from_dict(raw["geometry_noise"]),
        )

    def padding_for(self, shape: tuple[int, int], image_scale: int) -> int:
        return self.geometry_noise.padding_for(shape, image_scale)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


COLOR_LOOKUP = np.array(
    [_hex_to_rgb(COLOR_MAP.get(index, "#000000FF")) for index in range(16)],
    dtype=np.uint8,
)
BLACK_RGB = np.array(_hex_to_rgb("#000000FF"), dtype=np.uint8)


def _frame_to_rgb_array(
    frame: np.ndarray,
    scale: int,
    *,
    palette: np.ndarray | None = None,
) -> np.ndarray:
    height, width = frame.shape
    values = frame.astype(np.int64, copy=False)
    palette = COLOR_LOOKUP if palette is None else palette

    rgb_array = np.empty((height, width, 3), dtype=np.uint8)
    rgb_array[...] = BLACK_RGB

    valid = (0 <= values) & (values < len(palette))
    rgb_array[valid] = palette[values[valid]]

    if scale == 1:
        return rgb_array
    return np.repeat(np.repeat(rgb_array, scale, axis=0), scale, axis=1)


@dataclass(frozen=True)
class HomographySample:
    """Eight independently sampled values; translations are board fractions."""

    rotation_deg: float
    shear_deg: float
    scale_x: float
    scale_y: float
    translation_x: float
    translation_y: float
    perspective_x: float
    perspective_y: float


def _sample_homography(parameters: HomographyParameters, rng: np.random.Generator) -> HomographySample:
    if not parameters.has_geometry:
        return HomographySample(0, 0, 1, 1, 0, 0, 0, 0)
    draws = rng.uniform(-1, 1, 8)
    return HomographySample(
        float(draws[0] * parameters.rotation_deg),
        float(draws[1] * parameters.shear_deg),
        float(1 + draws[2] * parameters.scale_delta),
        float(1 + draws[3] * parameters.scale_delta),
        float(draws[4] * parameters.translation_delta),
        float(draws[5] * parameters.translation_delta),
        float(draws[6] * parameters.perspective_delta),
        float(draws[7] * parameters.perspective_delta),
    )


def _geometry_matrix(
    geometry: HomographySample, shape: tuple[int, int], image_scale: int, padding: int,
) -> np.ndarray:
    """Map unpadded enlarged-board coordinates to the padded output canvas."""
    height, width = (dimension * image_scale for dimension in shape)
    # Reduce the sampled rotation before converting to radians; large finite
    # configured ranges otherwise risk overflowing intermediate calculations.
    angle = math.radians(geometry.rotation_deg % 360)
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    shear = np.array([[1, math.tan(math.radians(geometry.shear_deg))], [0, 1]])
    linear = np.eye(3)
    linear[:2, :2] = rotation @ shear @ np.diag([geometry.scale_x, geometry.scale_y])
    perspective = np.eye(3)
    perspective[2, :2] = [2 * geometry.perspective_x / width, 2 * geometry.perspective_y / height]
    center_source = np.eye(3)
    center_source[:2, 2] = [-width / 2, -height / 2]
    translate_output = np.eye(3)
    translate_output[:2, 2] = [
        width / 2 + padding + geometry.translation_x * width,
        height / 2 + padding + geometry.translation_y * height,
    ]
    forward = translate_output @ linear @ perspective @ center_source
    return forward / forward[2, 2]


@dataclass(frozen=True)
class GeometrySample:
    homography: HomographySample
    controls: tuple[np.ndarray, ...]


def _ffd_amplitudes(parameters: GeometryNoiseParameters, shape: tuple[int, int]) -> tuple[float, ...]:
    return tuple(g.amplitude_fraction * min(shape) for g in parameters.active_grids)


def _geometry_plan(parameters: GeometryNoiseParameters, shape: tuple[int, int], image_scale: int) -> ffd.FFDPlan:
    padding = parameters.padding_for(shape, image_scale)
    domain_padding = parameters.automatic_padding_for(shape, image_scale)
    return ffd.make_plan(
        tuple(size * image_scale for size in shape), padding, domain_padding,
        tuple(g.grid_shape for g in parameters.active_grids),
    )


def _sample_geometry(parameters: GeometryNoiseParameters, plans: tuple[ffd.FFDPlan, ...],
                     rng: np.random.Generator) -> GeometrySample:
    homography = (_sample_homography(parameters.homography, rng) if parameters.has_homography
                  else HomographySample(0, 0, 1, 1, 0, 0, 0, 0))
    controls = ()
    if parameters.active_grids:
        controls = ffd.sample_controls(
            plans, tuple(_ffd_amplitudes(parameters, plan.shape) for plan in plans),
            rng, parameters.ffd.minimum_sampled_jacobian,
        )
    return GeometrySample(homography, controls)


def _geometry_maps(parameters: GeometryNoiseParameters, geometry: GeometrySample,
                   plan: ffd.FFDPlan, frame_shape: tuple[int, int], image_scale: int) -> tuple[np.ndarray, np.ndarray]:
    """Compose H^-1(p + E(p)) analytically so the image is sampled only once."""
    map_x, map_y = ffd.build_maps(plan, geometry.controls, _ffd_amplitudes(parameters, plan.shape))
    if not parameters.has_homography:
        return map_x, map_y
    inverse = np.linalg.inv(_geometry_matrix(geometry.homography, frame_shape, image_scale, 0))
    # Float64 projective arithmetic; OpenCV accepts the final float32 sampling maps.
    x, y = map_x.astype(np.float64), map_y.astype(np.float64)
    denominator = inverse[2, 0] * x + inverse[2, 1] * y + inverse[2, 2]
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        mapped_x = ((inverse[0, 0] * x + inverse[0, 1] * y + inverse[0, 2]) / denominator).astype(np.float32)
        mapped_y = ((inverse[1, 0] * x + inverse[1, 1] * y + inverse[1, 2]) / denominator).astype(np.float32)
    invalid = ~np.isfinite(mapped_x) | ~np.isfinite(mapped_y)
    mapped_x[invalid], mapped_y[invalid] = -1e6, -1e6
    return mapped_x, mapped_y


def board_pixel_center(
    pixel: tuple[int, int], frame_shape: tuple[int, int], image_scale: int,
    parameters: GeometryNoiseParameters, geometry: GeometrySample,
    *, plan: ffd.FFDPlan | None = None, geometry_maps: tuple[np.ndarray, np.ndarray] | None = None,
) -> tuple[float, float]:
    """Locate the original cell center through the exact sampled homography/FFD."""
    x, y = pixel
    # OpenCV's integer coordinates are pixel centers. An enlarged game cell
    # spans centers x*scale through (x+1)*scale-1, so its midpoint includes -0.5.
    source = np.array([(x + 0.5) * image_scale - 0.5, (y + 0.5) * image_scale - 0.5])
    target = source
    if parameters.has_homography:
        projected = _geometry_matrix(geometry.homography, frame_shape, image_scale, 0) @ np.r_[source, 1]
        target = projected[:2] / projected[2]
    if parameters.active_grids:
        plan = _geometry_plan(parameters, frame_shape, image_scale) if plan is None else plan
        amplitudes = _ffd_amplitudes(parameters, plan.shape)
        try:
            target = ffd.invert_point(plan, geometry.controls, amplitudes, target)
        except ValueError:
            # A difficult Newton start can be recovered using the nearest source
            # coordinate in the already-built raster map, then refining again.
            maps = (_geometry_maps(parameters, geometry, plan, frame_shape, image_scale)
                    if geometry_maps is None else geometry_maps)
            distance = np.square(maps[0].astype(np.float64) - source[0])
            distance += np.square(maps[1].astype(np.float64) - source[1])
            row, column = np.unravel_index(np.argmin(distance), distance.shape)
            initial = np.array([column - plan.padding, row - plan.padding], dtype=float)
            target = ffd.invert_point(plan, geometry.controls, amplitudes, target, initial=initial)
    padding = parameters.padding_for(frame_shape, image_scale)
    # The cursor hotspot is measured from pixel boundaries in Pillow, hence +0.5.
    return float(target[0] + padding + 0.5), float(target[1] + padding + 0.5)


@lru_cache(maxsize=1)
def _mouse_cursor_asset() -> tuple[Image.Image, tuple[float, float]]:
    with Image.open(CURSOR_ASSET_DIR / "mouse_cursor.png") as source:
        sprite = source.convert("RGBA")
    metadata = json.loads((CURSOR_ASSET_DIR / "mouse_cursor.json").read_text(encoding="utf-8"))
    return sprite, tuple(metadata["hotspot"])


@lru_cache(maxsize=16)
def _mouse_cursor_sprite(mouse_size: int) -> tuple[Image.Image, tuple[float, float]]:
    source, hotspot = _mouse_cursor_asset()
    width = max(1, round(source.width * mouse_size / source.height))
    sprite = source.resize((width, mouse_size), Image.Resampling.LANCZOS)
    return sprite, (hotspot[0] * width / source.width, hotspot[1] * mouse_size / source.height)


def overlay_mouse_cursor(image: Image.Image, center: tuple[float, float], mouse_size: int) -> Image.Image:
    """Place the upright cursor's tip at the target, clipping to the existing canvas."""
    validate_mouse_size(mouse_size)
    if mouse_size == 0:
        return image
    sprite, hotspot = _mouse_cursor_sprite(mouse_size)
    position = tuple(math.floor(target - tip + 0.5) for target, tip in zip(center, hotspot))
    result = image.copy()
    result.paste(sprite, position, sprite)
    return result


def _sample_palette_noise(amplitude: int, rng: np.random.Generator) -> np.ndarray | None:
    """One independent uniform integer offset per palette entry and RGB channel."""
    if amplitude == 0:
        return None
    # Mapping uniform draws also lets previews share quantiles across amplitudes.
    uniform = rng.random(COLOR_LOOKUP.shape)
    return np.floor(uniform * (2 * amplitude + 1)).astype(np.int16) - amplitude


def _sample_pixel_noise(
    shape: tuple[int, ...], standard_deviation: float, rng: np.random.Generator,
) -> np.ndarray | None:
    """Zero-mean Gaussian RGB noise on the entire output canvas, after its warp."""
    if standard_deviation == 0:
        return None
    return rng.normal(0.0, standard_deviation, size=shape)


def _add_noise(rgb: np.ndarray, noise: np.ndarray | None) -> np.ndarray:
    if noise is None:
        return rgb
    noisy = rgb.astype(np.float64) + noise
    np.clip(noisy, 0, 255, out=noisy)
    # Round Gaussian samples instead of introducing a downward truncation bias.
    return np.rint(noisy).astype(np.uint8)


def _frame_to_image(
    frame: np.ndarray,
    image_scale: int,
    image_noise: ImageNoiseParameters,
    geometry_maps: tuple[np.ndarray, np.ndarray] | None,
    palette_jitter: np.ndarray | None,
    pixel_noise: np.ndarray | None,
) -> Image.Image:
    """Render all board geometry and color noise before cursor compositing."""
    palette = _add_noise(COLOR_LOOKUP, palette_jitter) if image_noise.color_noise.enabled else COLOR_LOOKUP
    rgb = _frame_to_rgb_array(frame, scale=image_scale, palette=palette)
    padding = image_noise.padding_for(frame.shape, image_scale)
    if geometry_maps is not None:
        rgb = cv2.remap(
            rgb, geometry_maps[0], geometry_maps[1],
            RESAMPLING_METHODS[image_noise.geometry_noise.resampling],
            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
        )
    elif padding:
        rgb = np.pad(rgb, ((padding, padding), (padding, padding), (0, 0)))
    if image_noise.color_noise.enabled and pixel_noise is not None:
        rgb = _add_noise(rgb, pixel_noise)
    return Image.fromarray(rgb)


def _frame_to_image_bytes(
    frame: np.ndarray,
    image_scale: int,
    image_noise: ImageNoiseParameters,
    geometry_maps: tuple[np.ndarray, np.ndarray] | None,
    palette_jitter: np.ndarray | None,
    pixel_noise: np.ndarray | None,
    *,
    image_format: str,
    image_quality: int,
    cursor: tuple[tuple[float, float], int] | None = None,
) -> bytes:
    image = _frame_to_image(frame, image_scale, image_noise, geometry_maps, palette_jitter, pixel_noise)
    if cursor is not None:
        center, mouse_size = cursor
        image = overlay_mouse_cursor(image, center, mouse_size)
    buffer = BytesIO()
    if image_format == "jpg":
        image.save(buffer, format="JPEG", quality=image_quality)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _serialize_action_input(frame_data: Any) -> dict[str, Any] | None:
    if frame_data.action_input is None:
        return None
    return {
        "id": frame_data.action_input.id.name
        if hasattr(frame_data.action_input.id, "name")
        else str(frame_data.action_input.id),
        "data": frame_data.action_input.data,
    }


def _serialize_available_action(action: Any) -> Any:
    if isinstance(action, int):
        return f"ACTION{action}"
    return action


def _metadata_for_frame(frame_data: Any, action_input_override: dict[str, Any] | None = None) -> dict[str, Any]:
    action_input = _serialize_action_input(frame_data)
    if action_input is None:
        action_input = action_input_override
    return {
        "state": frame_data.state.name if hasattr(frame_data.state, "name") else str(frame_data.state),
        "levels_completed": frame_data.levels_completed,
        "win_levels": frame_data.win_levels,
        "available_actions": [
            *[_serialize_available_action(action) for action in frame_data.available_actions],
            HIGHLIGHT_ACTION_NAME,
        ],
        "action_input": action_input,
    }


def build_files(
    frame_data: Any,
    action_input_override: dict[str, Any] | None = None,
    *,
    image_scale: int,
    image_format: str,
    image_quality: int,
    image_noise: ImageNoiseParameters,
    rng: np.random.Generator | None = None,
) -> dict[str, str | bytes]:
    """Render one step, sharing geometry/noise only when its refresh mode is step."""
    validate_image_scale(image_scale)
    image_format = validate_image_format(image_format)
    validate_image_quality(image_quality)
    rng = np.random.default_rng() if rng is None else rng
    metadata = _metadata_for_frame(frame_data, action_input_override)
    files: dict[str, str | bytes] = {"metadata.txt": json.dumps(metadata, indent=2)}

    geometry = None
    palette_jitter = None
    step_pixel_noise: dict[tuple[int, int], np.ndarray | None] = {}
    color_settings = image_noise.color_noise
    geometry_settings = image_noise.geometry_noise
    frames = [np.array(frame, dtype=np.int16) for frame in (frame_data.frame or [])]
    for array in frames:
        if array.ndim != 2 or 0 in array.shape:
            raise ValueError("Image frames must be nonempty two-dimensional grids.")
    plans = {}
    if geometry_settings.has_geometry:
        for shape in dict.fromkeys(array.shape for array in frames):
            plans[shape] = _geometry_plan(geometry_settings, shape, image_scale)
    maps_by_shape = {}
    for frame_index, array in enumerate(frames):
        if geometry is None or geometry_settings.geometry_refresh == "frame":
            relevant_plans = (tuple(plans.values()) if geometry_settings.geometry_refresh == "step"
                              else (plans[array.shape],) if plans else ())
            geometry = _sample_geometry(geometry_settings, relevant_plans, rng)
            maps_by_shape.clear()
        if geometry_settings.has_geometry and array.shape not in maps_by_shape:
            maps_by_shape[array.shape] = _geometry_maps(
                geometry_settings, geometry, plans[array.shape], array.shape, image_scale,
            )
        if color_settings.enabled and (frame_index == 0 or color_settings.noise_refresh == "frame"):
            palette_jitter = _sample_palette_noise(color_settings.palette_noise, rng)
        padding = image_noise.padding_for(array.shape, image_scale)
        output_shape = tuple(dimension * image_scale + 2 * padding for dimension in array.shape)
        if not color_settings.enabled:
            pixel_noise = None
        elif color_settings.noise_refresh == "frame" or output_shape not in step_pixel_noise:
            pixel_noise = _sample_pixel_noise(
                (*output_shape, 3), color_settings.pixel_noise_std, rng,
            )
            if color_settings.noise_refresh == "step":
                step_pixel_noise[output_shape] = pixel_noise
        else:
            pixel_noise = step_pixel_noise[output_shape]
        if frame_index == len(frames) - 1:
            name_prefix = "final_frame"
        else:
            name_prefix = f"intermediate_frame_{frame_index:02d}"

        files[f"{name_prefix}.{image_format}"] = _frame_to_image_bytes(
            array, image_scale, image_noise, maps_by_shape.get(array.shape), palette_jitter, pixel_noise,
            image_format=image_format, image_quality=image_quality,
        )

    return files


def build_highlight_files(
    frame_data: Any,
    highlight_pixel: tuple[int, int],
    *,
    image_scale: int,
    image_format: str,
    image_quality: int,
    image_noise: ImageNoiseParameters,
    mouse_size: int,
    rng: np.random.Generator | None = None,
) -> dict[str, str | bytes]:
    """Render the last settled frame twice with shared fresh geometry."""
    validate_image_scale(image_scale)
    image_format = validate_image_format(image_format)
    validate_image_quality(image_quality)
    validate_mouse_size(mouse_size)
    rng = np.random.default_rng() if rng is None else rng
    frames = frame_data.frame or []
    if not frames:
        raise ValueError("Cannot highlight an observation without a settled frame.")
    array = np.array(frames[-1], dtype=np.int16)
    if array.ndim != 2 or 0 in array.shape:
        raise ValueError("Image frames must be nonempty two-dimensional grids.")
    x, y = highlight_pixel
    if type(x) is not int or type(y) is not int or not (0 <= x < array.shape[1] and 0 <= y < array.shape[0]):
        raise ValueError("Highlight coordinates must be integer indices inside the game frame.")

    metadata = _metadata_for_frame(frame_data)
    metadata["action_input"] = {"id": HIGHLIGHT_ACTION_NAME, "data": {"x": x, "y": y}}
    files: dict[str, str | bytes] = {"metadata.txt": json.dumps(metadata, indent=2)}
    parameters = image_noise.geometry_noise
    plan = _geometry_plan(parameters, array.shape, image_scale) if parameters.has_geometry else None
    geometry = _sample_geometry(parameters, () if plan is None else (plan,), rng)
    maps = None if plan is None else _geometry_maps(parameters, geometry, plan, array.shape, image_scale)
    center = (board_pixel_center(highlight_pixel, array.shape, image_scale, parameters, geometry,
                                plan=plan, geometry_maps=maps)
              if mouse_size else None)
    padding = image_noise.padding_for(array.shape, image_scale)
    output_shape = tuple(dimension * image_scale + 2 * padding for dimension in array.shape)
    color = image_noise.color_noise
    # Both images share one fresh homography/FFD sample. Color noise is sampled
    # independently for each image, regardless of the normal refresh settings.
    for name, highlighted in (("frame", False), ("frame_highlighted", True)):
        palette = _sample_palette_noise(color.palette_noise, rng) if color.enabled else None
        pixels = _sample_pixel_noise((*output_shape, 3), color.pixel_noise_std, rng) if color.enabled else None
        files[f"{name}.{image_format}"] = _frame_to_image_bytes(
            array, image_scale, image_noise, maps, palette, pixels,
            image_format=image_format, image_quality=image_quality,
            cursor=(center, mouse_size) if highlighted and center is not None else None,
        )
    return files
