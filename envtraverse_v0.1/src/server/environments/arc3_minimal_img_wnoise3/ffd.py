"""Vectorized cubic B-spline pull maps for any number of control grids."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.interpolate import BSpline


def _basis(coordinates: np.ndarray, size: int, domain_padding: int,
           count: int, derivative: bool) -> np.ndarray:
    # Open-uniform cubic knots. Clamped knots do not pin displacement coefficients.
    knots = np.r_[np.zeros(4), np.linspace(0, 1, count - 2)[1:-1], np.ones(4)]
    span = size - 1 + 2 * domain_padding
    if span <= 0:
        raise ValueError("The FFD domain must have positive extent.")
    positions = (coordinates.astype(np.float64) + domain_padding) / span
    clipped = np.clip(positions, 0, 1)
    if derivative:
        weights = BSpline(knots, np.eye(count), 3).derivative()(clipped) / span
        weights[(positions < 0) | (positions > 1)] = 0
    else:
        weights = BSpline.design_matrix(clipped, knots, 3).toarray()
    return np.ascontiguousarray(weights, dtype=np.float32)


@dataclass(frozen=True)
class FFDPlan:
    shape: tuple[int, int]
    padding: int
    domain_padding: int
    grid_shapes: tuple[tuple[int, int], ...]
    x: np.ndarray
    y: np.ndarray
    weights: dict[tuple[int, int], tuple[np.ndarray, ...]]


@lru_cache(maxsize=8)
def make_plan(shape: tuple[int, int], padding: int, domain_padding: int,
              grid_shapes: tuple[tuple[int, int], ...]) -> FFDPlan:
    """Cache geometry-independent weights and coordinates, never random controls."""
    height, width = shape
    x = np.arange(width + 2 * padding, dtype=np.float32) - padding
    y = np.arange(height + 2 * padding, dtype=np.float32) - padding
    xx, yy = np.meshgrid(x, y)
    weights = {}
    for rows, columns in dict.fromkeys(grid_shapes):
        weights[(rows, columns)] = (
            _basis(x, width, domain_padding, columns, False),
            _basis(y, height, domain_padding, rows, False),
            _basis(x, width, domain_padding, columns, True),
            _basis(y, height, domain_padding, rows, True),
        )
    for value in (xx, yy, *(array for item in weights.values() for array in item)):
        value.flags.writeable = False
    return FFDPlan(shape, padding, domain_padding, grid_shapes, xx, yy, weights)


def _evaluate(by: np.ndarray, coefficients: np.ndarray, bx: np.ndarray) -> np.ndarray:
    return (by @ coefficients) @ bx.T


def build_maps(plan: FFDPlan, controls: tuple[np.ndarray, ...],
               amplitudes: tuple[float, ...]) -> tuple[np.ndarray, np.ndarray]:
    """source = output - padding + sum(displacement fields), before homography."""
    map_x, map_y = plan.x.copy(), plan.y.copy()
    for shape, coefficients, amplitude in zip(plan.grid_shapes, controls, amplitudes, strict=True):
        bx, by, _, _ = plan.weights[shape]
        scaled = coefficients * amplitude
        map_x += _evaluate(by, scaled[:, :, 0], bx)
        map_y += _evaluate(by, scaled[:, :, 1], bx)
    return map_x, map_y


def minimum_jacobian(plan: FFDPlan, controls: tuple[np.ndarray, ...],
                     amplitudes: tuple[float, ...]) -> float:
    """Check the summed FFD at pixel centers, not each constituent field separately."""
    xx, yy = np.ones(plan.x.shape, dtype=np.float32), np.ones(plan.x.shape, dtype=np.float32)
    xy, yx = np.zeros_like(xx), np.zeros_like(xx)
    for shape, coefficients, amplitude in zip(plan.grid_shapes, controls, amplitudes, strict=True):
        bx, by, dx, dy = plan.weights[shape]
        scaled = coefficients * amplitude
        xx += _evaluate(by, scaled[:, :, 0], dx)
        xy += _evaluate(dy, scaled[:, :, 0], bx)
        yx += _evaluate(by, scaled[:, :, 1], dx)
        yy += _evaluate(dy, scaled[:, :, 1], bx)
    return float(np.min(xx * yy - xy * yx))


def sample_controls(plans: tuple[FFDPlan, ...], amplitudes: tuple[tuple[float, ...], ...],
                    rng: np.random.Generator, threshold: float) -> tuple[np.ndarray, ...]:
    """Share normalized controls across step frames, checking every distinct frame shape."""
    for _ in range(100):
        controls = tuple(
            rng.uniform(-1, 1, (*shape, 2)).astype(np.float32)
            for shape in plans[0].grid_shapes
        )
        if all(minimum_jacobian(plan, controls, bounds) >= threshold
               for plan, bounds in zip(plans, amplitudes, strict=True)):
            return controls
    raise ValueError(
        "Unable to sample FFD above minimum_sampled_jacobian after 100 attempts; "
        "reduce grid amplitudes or the Jacobian threshold."
    )
