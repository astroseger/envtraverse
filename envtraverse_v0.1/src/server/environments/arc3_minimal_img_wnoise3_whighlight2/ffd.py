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


@lru_cache(maxsize=32)
def _point_basis(count: int) -> tuple[BSpline, BSpline]:
    knots = np.r_[np.zeros(4), np.linspace(0, 1, count - 2)[1:-1], np.ones(4)]
    basis = BSpline(knots, np.eye(count), 3)
    return basis, basis.derivative()


def _point_weights(coordinate: float, size: int, padding: int, count: int) -> tuple[np.ndarray, np.ndarray]:
    span = size - 1 + 2 * padding
    position = (coordinate + padding) / span
    clipped = np.clip(position, 0, 1)
    basis, derivative = _point_basis(count)
    weights = basis(clipped)
    slopes = derivative(clipped) / span if 0 <= position <= 1 else np.zeros(count)
    return weights, slopes


def point_map_and_jacobian(
    plan: FFDPlan, controls: tuple[np.ndarray, ...], amplitudes: tuple[float, ...],
    point: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Evaluate p + E(p) and its analytic Jacobian at one unpadded output point."""
    point = np.asarray(point, dtype=np.float64)
    mapped, jacobian = point.copy(), np.eye(2)
    height, width = plan.shape
    for (rows, columns), coefficients, amplitude in zip(plan.grid_shapes, controls, amplitudes, strict=True):
        bx, dx = _point_weights(point[0], width, plan.domain_padding, columns)
        by, dy = _point_weights(point[1], height, plan.domain_padding, rows)
        scaled = coefficients.astype(np.float64) * amplitude
        mapped += np.einsum("i,ijc,j->c", by, scaled, bx)
        jacobian[:, 0] += np.einsum("i,ijc,j->c", by, scaled, dx)
        jacobian[:, 1] += np.einsum("i,ijc,j->c", dy, scaled, bx)
    return mapped, jacobian


def invert_point(
    plan: FFDPlan, controls: tuple[np.ndarray, ...], amplitudes: tuple[float, ...],
    target: np.ndarray, *, initial: np.ndarray | None = None,
) -> np.ndarray:
    """Solve p + E(p) = target with damped Newton steps and a checked residual."""
    target = np.asarray(target, dtype=np.float64)
    # Nonnegative partition-of-unity weights bound displacement on each axis.
    bound = sum(amplitudes)
    lower, upper = target - bound, target + bound
    point = np.clip(target if initial is None else initial, lower, upper)
    for _ in range(40):
        mapped, jacobian = point_map_and_jacobian(plan, controls, amplitudes, point)
        residual = mapped - target
        error = np.linalg.norm(residual, ord=np.inf)
        if error <= 1e-6:
            return point
        if not np.isfinite(error):
            break
        try:
            step = np.linalg.solve(jacobian, residual)
        except np.linalg.LinAlgError:
            break
        for backtrack in range(16):
            candidate = np.clip(point - step * (0.5 ** backtrack), lower, upper)
            candidate_map, _ = point_map_and_jacobian(plan, controls, amplitudes, candidate)
            if np.linalg.norm(candidate_map - target, ord=np.inf) < error:
                point = candidate
                break
        else:
            break
    raise ValueError("Could not locate the highlighted cell through FFD to subpixel accuracy.")


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
