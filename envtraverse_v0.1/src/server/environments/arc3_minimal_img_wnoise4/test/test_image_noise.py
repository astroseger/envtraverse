from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import itertools
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.arc3_minimal_img_wnoise4 import artifacts_preprocessing as images
from environments.arc3_minimal_img_wnoise4 import environment_manager as environment
from environments.arc3_minimal_img_wnoise4 import ffd

PROJECT = Path(__file__).resolve().parents[5]
CONFIG = yaml.safe_load((PROJECT / "run_config_arc3_minimal_img_wnoise4.yaml").read_text())
RAW = CONFIG["environment_parameters"]
EXAMPLE_SETTINGS = images.ImageNoiseParameters.from_dict(RAW["image_noise"])
# Keep the original FFD/homography regression cases at their original canvas size.
SETTINGS = replace(EXAMPLE_SETTINGS, geometry_noise=replace(
    EXAMPLE_SETTINGS.geometry_noise,
    pre_rotation=replace(EXAMPLE_SETTINGS.geometry_noise.pre_rotation, enabled=False),
))
CLEAN = replace(SETTINGS, color_noise=replace(SETTINGS.color_noise, enabled=False))


def observation(frames):
    return SimpleNamespace(frame=frames, state=SimpleNamespace(name="NOT_FINISHED"),
                           levels_completed=0, win_levels=1, available_actions=[1, 6], action_input=None)


def board(shape=(64, 64)):
    return np.arange(np.prod(shape), dtype=np.int16).reshape(shape) % 16


def render(settings, frames=None, scale=2, seed=17, image_format="png", angle=None):
    return images.build_files(
        observation([board()] if frames is None else frames),
        image_scale=scale, image_format=image_format, image_quality=25,
        image_noise=settings, rng=np.random.default_rng(seed),
        pre_rotation_deg=angle,
    )


def arrays(files):
    return [np.array(Image.open(BytesIO(data))) for name, data in files.items() if name.endswith(".png")]


def geometry(settings=CLEAN, **changes):
    return replace(settings, geometry_noise=replace(settings.geometry_noise, **changes))


def rotation(settings=CLEAN, **changes):
    return geometry(settings, pre_rotation=replace(settings.geometry_noise.pre_rotation, enabled=True, **changes))


class ConfigurationTests(unittest.TestCase):
    def test_v4_example_and_manager_defaults(self):
        self.assertEqual(CONFIG["environment"], "arc3_minimal_img_wnoise4")
        self.assertEqual(EXAMPLE_SETTINGS.geometry_noise.pre_rotation,
                         images.PreRotationParameters(True, "linear", 0, 2, (0, 5)))
        self.assertEqual(EXAMPLE_SETTINGS.padding_for((64, 64), 8), 192)
        self.assertEqual(SETTINGS.padding_for((64, 64), 8), 88)
        p = environment.EnvironmentManagerParameters.from_dict(RAW)
        self.assertFalse(p.competition)
        self.assertEqual(p.tags, ["api-test"])
        explicit = environment.EnvironmentManagerParameters.from_dict(dict(
            RAW, competition=True, source_url="https://example.org/agent", tag="custom",
        ))
        self.assertTrue(explicit.competition)
        self.assertEqual(explicit.tags, ["custom"])

    def test_required_fields_even_in_disabled_groups(self):
        paths = [(), ("image_noise",), ("image_noise", "color_noise"),
                 ("image_noise", "geometry_noise"), ("image_noise", "geometry_noise", "homography"),
                 ("image_noise", "geometry_noise", "pre_rotation"),
                 ("image_noise", "geometry_noise", "ffd"),
                 ("image_noise", "geometry_noise", "ffd", "grids", 0)]
        with patch.object(environment, "Arcade") as backend:
            for path in paths:
                source = RAW
                for key in path:
                    source = source[key]
                for field in source:
                    raw = deepcopy(RAW)
                    target = raw
                    for key in path:
                        target = target[key]
                    del target[field]
                    with self.subTest(path=path, field=field), self.assertRaisesRegex(ValueError, "Missing required"):
                        environment.EnvironmentManager(raw)
            backend.assert_not_called()

    def test_invalid_ffd_and_homography_settings(self):
        invalid = [
            ("grids", value) for value in (None, {}, "4")
        ] + [("minimum_sampled_jacobian", value) for value in (0, -1, 1.1, True, "0.1", float("nan"))]
        for key, value in invalid:
            raw = deepcopy(RAW)
            raw["image_noise"]["geometry_noise"]["ffd"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                environment.EnvironmentManagerParameters.from_dict(raw)
        for value in (4, [3, 8], [4], [4, 8, 16], [True, 4], [4.0, 4]):
            with self.subTest(grid_shape=value), self.assertRaises((ValueError, TypeError)):
                images.FFDGridParameters(value, 0.1)
        for value in (-0.1, float("nan"), float("inf"), True, "0.1", 10**400):
            with self.subTest(amplitude=value), self.assertRaises((ValueError, TypeError)):
                images.FFDGridParameters((4, 4), value)
        for group in ("pre_rotation", "homography", "ffd"):
            raw = deepcopy(RAW)
            raw["image_noise"]["geometry_noise"][group]["enabled"] = "false"
            with self.assertRaisesRegex(TypeError, "boolean"):
                environment.EnvironmentManagerParameters.from_dict(raw)
        raw = deepcopy(RAW)
        raw["image_noise"]["geometry_noise"]["homography"]["perspective_delta"] = 0.5
        with self.assertRaises(ValueError):
            environment.EnvironmentManagerParameters.from_dict(raw)
        raw = deepcopy(RAW)
        raw["image_noise"]["geometry_noise"]["ffd"]["coarse_grid"] = 4
        with self.assertRaisesRegex(ValueError, "Unknown"):
            environment.EnvironmentManagerParameters.from_dict(raw)

    def test_manual_padding_validation_and_master_disable(self):
        with self.assertRaisesRegex(ValueError, "at least"):
            geometry(padding=80).padding_for((64, 64), 8)
        for padding in ("auto", 0, 1, 1000):
            settings = geometry(enabled=False, padding=padding)
            self.assertEqual(settings.padding_for((64, 64), 8), 0)
            with patch.object(ffd, "sample_controls") as sampler:
                result = arrays(render(settings, scale=8))[0]
            sampler.assert_not_called()
            expected = np.repeat(np.repeat(images.COLOR_LOOKUP[board()], 8, axis=0), 8, axis=1)
            np.testing.assert_array_equal(result, expected)


class PreRotationTests(unittest.TestCase):
    def test_validation_including_inactive_mode_fields(self):
        invalid = [
            ("enabled", "false"), ("mode", "unknown"),
            ("increment_range_deg", [5, 0]), ("increment_range_deg", [0]),
            ("increment_range_deg", "0,5"), ("increment_range_deg", [0, True]),
            ("increment_range_deg", [0, float("inf")]),
        ]
        for key in ("initial_angle_deg", "deg_per_step"):
            invalid.extend((key, value) for value in (True, "2", None, float("nan"), float("inf"), 10**400))
        for key, value in invalid:
            raw = deepcopy(RAW)
            raw["image_noise"]["geometry_noise"]["pre_rotation"]["enabled"] = False
            raw["image_noise"]["geometry_noise"]["pre_rotation"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                environment.EnvironmentManagerParameters.from_dict(raw)
        raw = deepcopy(RAW)
        raw["image_noise"]["geometry_noise"]["pre_rotation"]["unknown"] = 1
        with self.assertRaisesRegex(ValueError, "Unknown"):
            environment.EnvironmentManagerParameters.from_dict(raw)

    def test_linear_angles_initial_offset_direction_and_wrap(self):
        rng = Mock()
        for initial, increment, expected in (
            (350, 20, [350, 10, 30, 50, 70]),
            (-45, -90, [315, 225, 135, 45, 315]),
            (0, 2, [0, 2, 4, 6, 8]),
        ):
            parameters = images.PreRotationParameters(True, "linear", initial, increment, [-5, 5])
            actual = [parameters.angle_for_step(step, 123, rng) for step in range(5)]
            self.assertEqual(actual, expected)
        parameters = images.PreRotationParameters(True, "linear", 1e308, 1e308, [-5, 5])
        self.assertTrue(math.isfinite(parameters.angle_for_step(10, 0, rng)))
        rng.random.assert_not_called()

    def test_random_increments_accumulate_once_and_allow_both_directions(self):
        parameters = images.PreRotationParameters(True, "random_increment", 350, 2, [0, 8])
        rng = Mock()
        rng.random.side_effect = [0.25, 0.5, 0.75]
        angle = parameters.angle_for_step(0, 0, rng)
        rng.random.assert_not_called()
        angles = [angle]
        for step in range(1, 4):
            angle = parameters.angle_for_step(step, angle, rng)
            angles.append(angle)
        self.assertEqual(angles, [350, 352, 356, 2])
        self.assertEqual(rng.random.call_count, 3)
        rng.random.side_effect = None
        rng.random.return_value = 0.25
        self.assertEqual(replace(parameters, increment_range_deg=[-4, 4]).angle_for_step(1, 1, rng), 359)
        large = replace(parameters, increment_range_deg=[-1e308, 1e308])
        self.assertTrue(math.isfinite(large.angle_for_step(1, 0, rng)))

    def test_full_random_draws_absolute_angles_over_the_full_circle(self):
        parameters = images.PreRotationParameters(True, "full_random", 37, 73, [10, 20])
        rng = Mock()
        rng.random.side_effect = [0.75, 0.25, 0.5, 0, np.nextafter(1.0, 0.0)]
        angle = parameters.angle_for_step(0, 0, rng)
        self.assertEqual(angle, 37)
        rng.random.assert_not_called()
        angles = []
        for step in range(1, 6):
            angle = parameters.angle_for_step(step, angle, rng)
            angles.append(angle)
        self.assertEqual(angles[:4], [270, 90, 180, 0])
        self.assertTrue(359 < angles[-1] < 360)
        self.assertEqual(rng.random.call_count, 5)

    def test_rotation_only_is_clockwise_and_samples_original_once(self):
        settings = geometry(rotation(initial_angle_deg=90),
                            ffd=replace(CLEAN.geometry_noise.ffd, enabled=False), resampling="nearest")
        frame = np.full((8, 12), 5, dtype=np.int16)
        frame[1, 9], frame[5, 2] = 8, 10
        with patch.object(cv2, "remap", wraps=cv2.remap) as remap, \
             patch.object(ffd, "sample_controls") as controls:
            actual = arrays(render(settings, [frame], scale=1))[0]
        remap.assert_called_once()
        controls.assert_not_called()
        pad = settings.padding_for(frame.shape, 1)
        expected = np.zeros_like(actual)
        expected[pad + 7, pad + 9] = images.COLOR_LOOKUP[8]
        expected[pad, pad + 5] = images.COLOR_LOOKUP[10]
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(actual, arrays(render(settings, [frame], scale=1, angle=450))[0])
        disabled = geometry(settings, enabled=False)
        np.testing.assert_array_equal(arrays(render(disabled, [frame], scale=1))[0], images.COLOR_LOOKUP[frame])

    def test_pre_rotation_precedes_noncommuting_homography_and_ffd(self):
        settings = geometry(rotation(), homography=replace(CLEAN.geometry_noise.homography, enabled=True))
        parameters = settings.geometry_noise
        shape, scale, angle = (32, 64), 2, 37
        plan = images._geometry_plan(parameters, shape, scale)
        sample = images._sample_geometry(parameters, (plan,), np.random.default_rng(25))
        maps = images._geometry_maps(parameters, sample, plan, shape, scale, pre_rotation_deg=angle)
        elastic = ffd.build_maps(plan, sample.controls, images._ffd_amplitudes(parameters, plan.shape))
        points = np.column_stack([elastic[0][::11, ::13].ravel(), elastic[1][::11, ::13].ravel(),
                                  np.ones(elastic[0][::11, ::13].size)])
        # Undo H first, then independently undo clockwise rotation about the board center.
        inverse_h = np.linalg.inv(images._geometry_matrix(sample.homography, shape, scale, 0))
        projected = points @ inverse_h.T
        x, y = (projected[:, :2] / projected[:, 2, None]).T
        cx, cy = shape[1] * scale / 2, shape[0] * scale / 2
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        expected_x = cx + c * (x - cx) + s * (y - cy)
        expected_y = cy - s * (x - cx) + c * (y - cy)
        np.testing.assert_allclose(maps[0][::11, ::13].ravel(), expected_x, atol=3e-5)
        np.testing.assert_allclose(maps[1][::11, ::13].ravel(), expected_y, atol=3e-5)

    def test_one_angle_for_every_frame_and_shape_in_both_refresh_modes(self):
        for refresh in ("step", "frame"):
            settings = geometry(rotation(), geometry_refresh=refresh)
            frames = [board((16, 16)), board((8, 16)), board((16, 16))]
            with patch.object(images, "_geometry_maps", wraps=images._geometry_maps) as maps, \
                 patch.object(ffd, "sample_controls", wraps=ffd.sample_controls) as controls:
                result = arrays(render(settings, frames, angle=37))
            self.assertEqual(maps.call_count, 2 if refresh == "step" else 3)
            self.assertEqual(controls.call_count, 1 if refresh == "step" else 3)
            self.assertEqual([call.kwargs["pre_rotation_deg"] for call in maps.call_args_list], [37] * maps.call_count)
            self.assertEqual(np.array_equal(result[0], result[2]), refresh == "step")

    def test_padding_contains_full_rotations_with_extreme_homography_and_ffd(self):
        for shape, method in itertools.product(((64, 64), (24, 64)), ("nearest", "bicubic")):
            parameters = geometry(rotation(), resampling=method,
                                  homography=replace(CLEAN.geometry_noise.homography, enabled=True)).geometry_noise
            height, width = np.array(shape) * 8
            radius = 1 if method == "nearest" else 2
            pad = parameters.padding_for(shape, 8)
            ffd_bound = sum(images._ffd_amplitudes(parameters, (height, width)))
            corners = np.array([[-radius, -radius, 1], [width+radius, -radius, 1],
                                [width+radius, height+radius, 1], [-radius, height+radius, 1]])
            for signs in itertools.product((-1, 1), repeat=8):
                rng = Mock()
                rng.uniform.return_value = np.array(signs)
                sample = images._sample_homography(parameters.homography, rng)
                for angle in (0, 17, 45, 90, 123, 269):
                    forward = images._geometry_matrix(sample, shape, 8, pad, pre_rotation_deg=angle)
                    projected = corners @ forward.T
                    points = projected[:, :2] / projected[:, 2, None]
                    self.assertTrue(np.all(points - ffd_bound >= 4 - 1e-7))
                    self.assertTrue(np.all(points + ffd_bound <= [width+2*pad-4+1e-7, height+2*pad-4+1e-7]))

    def test_manual_padding_and_perspective_validation_include_pre_rotation(self):
        with self.assertRaisesRegex(ValueError, "at least"):
            geometry(rotation(), padding=88).padding_for((64, 64), 8)
        parameters = geometry(rotation(), homography=replace(
            CLEAN.geometry_noise.homography, enabled=True, perspective_delta=0.3,
        )).geometry_noise
        with self.assertRaisesRegex(ValueError, "perspective_delta"):
            parameters.padding_for((16, 64), 8)
        # This rectangular frame is valid without rotation; a square remains valid with it.
        replace(parameters, pre_rotation=replace(parameters.pre_rotation, enabled=False)).padding_for((16, 64), 8)
        replace(parameters, homography=replace(parameters.homography, perspective_delta=0.4)).padding_for((64, 64), 8)

    def test_full_rotation_canvas_is_stable_and_preserves_all_content(self):
        source = np.full((64, 128, 3), 255, dtype=np.uint8)
        for homography_enabled, method in itertools.product((False, True), images.RESAMPLING_METHODS):
            parameters = geometry(rotation(), resampling=method, homography=replace(
                CLEAN.geometry_noise.homography, enabled=homography_enabled,
            )).geometry_noise
            plan = images._geometry_plan(parameters, (32, 64), 2)
            sample = images._sample_geometry(parameters, (plan,), np.random.default_rng(13))
            sample = replace(sample, controls=tuple(np.ones_like(c) for c in sample.controls))
            larger_parameters = replace(parameters, padding=plan.padding + 19)
            larger_plan = images._geometry_plan(larger_parameters, (32, 64), 2)
            for angle in (0, 45, 90, 213):
                maps = images._geometry_maps(parameters, sample, plan, (32, 64), 2, pre_rotation_deg=angle)
                larger_maps = images._geometry_maps(larger_parameters, sample, larger_plan,
                                                   (32, 64), 2, pre_rotation_deg=angle)
                actual = cv2.remap(source, *maps, images.RESAMPLING_METHODS[method])
                larger = cv2.remap(source, *larger_maps, images.RESAMPLING_METHODS[method])
                self.assertEqual(actual.shape, (64 + 2*plan.padding, 128 + 2*plan.padding, 3))
                np.testing.assert_array_equal(actual, larger[19:-19, 19:-19])
                larger[19:-19, 19:-19] = 0
                self.assertFalse(larger.any())
                for edge in (actual[:4], actual[-4:], actual[:, :4], actual[:, -4:]):
                    self.assertFalse(edge.any())


class RotationManagerTests(unittest.TestCase):
    def make_manager(self, *, noisy=False, geometry_enabled=True, **rotation_changes):
        raw = deepcopy(RAW)
        raw.update(image_scale=1, image_format="png")
        raw["image_noise"]["color_noise"]["enabled"] = noisy
        raw["image_noise"]["geometry_noise"]["ffd"]["enabled"] = noisy
        raw["image_noise"]["geometry_noise"]["enabled"] = geometry_enabled
        raw["image_noise"]["geometry_noise"]["pre_rotation"].update(rotation_changes)
        manager = environment.EnvironmentManager(raw)
        self.addCleanup(manager.stop)
        backend = Mock()
        backend.make.return_value.observation_space = observation([board((16, 16))])
        backend.make.return_value.step.return_value = observation([board((16, 16))] * 3)
        backend.make.return_value.reset.return_value = observation([board((16, 16))] * 2)
        return manager, backend

    def test_global_steps_across_resets_and_level_changes(self):
        for mode, expected_angles in (("linear", [350, 10, 30, 50]), ("full_random", [350, 270, 90, 180])):
            with self.subTest(mode=mode):
                manager, backend = self.make_manager(mode=mode, initial_angle_deg=350, deg_per_step=20)
                manager._rotation_rng = Mock()
                manager._rotation_rng.random.side_effect = [0.75, 0.25, 0.5]
                with patch.object(environment, "Arcade", return_value=backend), \
                     patch.object(environment, "build_files", wraps=images.build_files) as rendered:
                    outputs = [manager.start_environment("test-game").observation]
                    with self.assertRaises(environment.InvalidActionError):
                        manager.move({"id": "RESET"})
                    outputs.append(manager.move({"id": "ACTION1"}))
                    outputs.append(manager.move({"id": "RESET"}))
                    backend.make.return_value.step.return_value.levels_completed = 1
                    outputs.append(manager.move({"id": "ACTION1"}))
                angles = [call.kwargs["pre_rotation_deg"] for call in rendered.call_args_list]
                self.assertEqual(angles, expected_angles)
                self.assertEqual(manager._rotation_rng.random.call_count, 0 if mode == "linear" else 3)
                self.assertEqual(manager._rotation_step, 3)
                self.assertEqual(manager.current_level_steps, 0)
                for output, angle in zip(outputs, angles):
                    expected = arrays(render(manager.parameters.image_noise, [board((16, 16))], scale=1, angle=angle))[0]
                    for actual in arrays(output.files):
                        np.testing.assert_array_equal(actual, expected)
                self.assertEqual([len(arrays(output.files)) for output in outputs], [1, 3, 2, 3])

    def test_invalid_and_failed_actions_do_not_advance_or_draw(self):
        manager, backend = self.make_manager(mode="random_increment")
        manager._rotation_rng = Mock()
        with patch.object(environment, "Arcade", return_value=backend):
            manager.start_environment("test-game")
        for action in ({"id": "UNKNOWN"}, {"id": "RESET"}, {"id": "ACTION6", "data": {"x": 64, "y": 0}}):
            with self.assertRaises(environment.InvalidActionError):
                manager.move(action)
        backend.make.return_value.step.side_effect = RuntimeError("backend failure")
        with self.assertRaisesRegex(RuntimeError, "backend failure"):
            manager.move({"id": "ACTION1"})
        backend.make.return_value.step.side_effect = None
        backend.make.return_value.step.return_value = None
        with self.assertRaises(environment.FatalEnvironmentError):
            manager.move({"id": "ACTION1"})
        self.assertEqual(manager._rotation_step, 0)
        self.assertEqual(manager._rotation_angle_deg, 0)
        manager._rotation_rng.random.assert_not_called()

    def test_random_sequence_is_independent_of_frames_and_other_noise(self):
        for mode in ("random_increment", "full_random"):
            with self.subTest(mode=mode):
                sequences = []
                for noisy in (False, True):
                    manager, backend = self.make_manager(noisy=noisy, mode=mode, initial_angle_deg=10,
                                                        increment_range_deg=[-5, 7])
                    manager._rotation_rng = np.random.default_rng(99)
                    manager._image_rng = np.random.default_rng(123)
                    with patch.object(environment, "Arcade", return_value=backend):
                        manager.start_environment("test-game")
                    sequence = [manager._rotation_angle_deg]
                    for count in (1, 3, 2, 1):
                        backend.make.return_value.step.return_value = observation([board((16, 16))] * (count if noisy else 1))
                        manager.move({"id": "ACTION1"})
                        sequence.append(manager._rotation_angle_deg)
                    sequences.append(sequence)
                rng = np.random.default_rng(99)
                angles = (10 + np.cumsum(rng.uniform(-5, 7, 4))) % 360 if mode == "random_increment" else rng.uniform(0, 360, 4)
                np.testing.assert_allclose(sequences[0], np.r_[10, angles])
                np.testing.assert_array_equal(sequences[0], sequences[1])

    def test_disabling_rotation_or_all_geometry_skips_random_draws(self):
        for master in (False, True):
            manager, backend = self.make_manager(mode="random_increment", enabled=master,
                                                  geometry_enabled=not master, initial_angle_deg=90)
            manager._rotation_rng = Mock()
            with patch.object(environment, "Arcade", return_value=backend):
                initial = manager.start_environment("test-game").observation
            moved = manager.move({"id": "ACTION1"})
            for output in (initial, moved):
                np.testing.assert_array_equal(arrays(output.files)[-1], images.COLOR_LOOKUP[board((16, 16))])
            self.assertEqual(manager._rotation_angle_deg, 0)
            manager._rotation_rng.random.assert_not_called()


class FFDTests(unittest.TestCase):
    def test_cubic_basis_and_free_boundaries(self):
        coordinates = np.linspace(0, 100, 101)
        u = coordinates / 100
        expected = np.stack(((1-u)**3, 3*u*(1-u)**2, 3*u**2*(1-u), u**3), axis=1)
        np.testing.assert_allclose(ffd._basis(coordinates, 101, 0, 4, False), expected, atol=1e-7)
        for size in (4, 5, 8, 12):
            weights = ffd._basis(np.arange(-20, 121), 101, 10, size, False)
            self.assertGreaterEqual(weights.min(), 0)
            np.testing.assert_allclose(weights.sum(axis=1), 1, atol=2e-7)
        plan = ffd.make_plan((64, 64), 16, 16, ((4, 4),))
        controls = np.zeros((4, 4, 2), dtype=np.float32)
        controls[0, 0] = [1, -1]
        maps = ffd.build_maps(plan, (controls,), (8,))
        self.assertEqual(maps[0][0, 0] - plan.x[0, 0], 8)
        self.assertEqual(maps[1][0, 0] - plan.y[0, 0], -8)
        self.assertGreater(maps[0][16, 16], 0)

    def test_arbitrary_grid_counts_rectangular_grids_and_additivity(self):
        rng = np.random.default_rng(9)
        shapes = ((4, 4), (8, 8), (5, 7), (4, 4))
        controls = tuple(rng.uniform(-1, 1, (*shape, 2)).astype(np.float32) for shape in shapes)
        bounds = (4, 1, 0.5, 0.25)
        for count in range(5):
            plan = ffd.make_plan((64, 80), 16, 16, shapes[:count])
            maps = ffd.build_maps(plan, controls[:count], bounds[:count])
            expected = [plan.x.copy(), plan.y.copy()]
            for shape, control, amplitude in zip(shapes[:count], controls[:count], bounds[:count]):
                single = ffd.make_plan((64, 80), 16, 16, (shape,))
                part = ffd.build_maps(single, (control,), (amplitude,))
                expected[0] += part[0] - single.x
                expected[1] += part[1] - single.y
            for actual, target, base in zip(maps, expected, (plan.x, plan.y)):
                np.testing.assert_allclose(actual, target, atol=3e-5)
                self.assertLessEqual(np.max(np.abs(actual - base)), sum(bounds[:count]) + 1e-5)
            self.assertGreater(ffd.minimum_jacobian(plan, controls[:count], bounds[:count]), 0.1)

    def test_jacobian_matches_finite_difference(self):
        plan = ffd.make_plan((128, 128), 0, 0, ((4, 4), (8, 8), (5, 7)))
        rng = np.random.default_rng(44)
        controls = tuple(rng.uniform(-1, 1, (*shape, 2)).astype(np.float32) for shape in plan.grid_shapes)
        amplitudes = (3, 1, 0.5)
        maps = ffd.build_maps(plan, controls, amplitudes)
        dy_x, dx_x = np.gradient(maps[0], edge_order=2)
        dy_y, dx_y = np.gradient(maps[1], edge_order=2)
        numerical = float(np.min(dx_x * dy_y - dy_x * dx_y))
        self.assertAlmostEqual(ffd.minimum_jacobian(plan, controls, amplitudes), numerical, delta=0.002)

    def test_known_combined_rejections(self):
        plan = ffd.make_plan((512, 512), 88, 88, ((4, 4), (8, 8)))
        # Seed from test_ffd's grid/A: its first two combined-strong draws fail.
        seed = 13469436960999524731
        rng = np.random.default_rng(seed)
        candidates = [tuple(rng.uniform(-1, 1, (*shape, 2)).astype(np.float32) for shape in plan.grid_shapes)
                      for _ in range(3)]
        minima = [ffd.minimum_jacobian(plan, c, (64, 16)) for c in candidates]
        self.assertLess(minima[0], 0.1)
        self.assertLess(minima[1], 0)
        self.assertGreaterEqual(minima[2], 0.1)
        accepted = ffd.sample_controls((plan,), ((64, 16),), np.random.default_rng(seed), 0.1)
        for actual, expected in zip(accepted, candidates[2]):
            np.testing.assert_array_equal(actual, expected)

    def test_sampling_accepts_after_more_than_100_rejections(self):
        plan = ffd.make_plan((16, 24), 0, 0, ((4, 4), (8, 8)))
        seed = 42
        expected_rng = np.random.default_rng(seed)
        for _ in range(251):
            expected = tuple(expected_rng.uniform(-1, 1, (*shape, 2)).astype(np.float32)
                             for shape in plan.grid_shapes)
        rng = np.random.default_rng(seed)
        with patch.object(ffd, "minimum_jacobian", side_effect=[-1.0] * 250 + [0.1]) as check:
            accepted = ffd.sample_controls((plan,), ((4, 1),), rng, 0.1)
        self.assertEqual(check.call_count, 251)
        for actual, target in zip(accepted, expected, strict=True):
            np.testing.assert_array_equal(actual, target)
        self.assertEqual(rng.bit_generator.state, expected_rng.bit_generator.state)

    def test_sampling_remains_interruptible_after_many_rejections(self):
        plan = ffd.make_plan((16, 24), 0, 0, ((4, 4), (8, 8)))
        for interruption in (KeyboardInterrupt(), SystemExit(143)):
            with self.subTest(interruption=type(interruption).__name__), \
                 patch.object(ffd, "minimum_jacobian",
                              side_effect=[-1.0] * 250 + [interruption]) as check:
                with self.assertRaises(type(interruption)) as raised:
                    ffd.sample_controls((plan,), ((4, 1),), np.random.default_rng(42), 0.1)
                self.assertIs(raised.exception, interruption)
                self.assertEqual(check.call_count, 251)

    def test_zero_grids_disabled_ffd_and_zero_amplitude_are_identity(self):
        for parameters in (
            replace(CLEAN.geometry_noise.ffd, grids=()),
            replace(CLEAN.geometry_noise.ffd, enabled=False),
            replace(CLEAN.geometry_noise.ffd, grids=(images.FFDGridParameters((5, 7), 0),)),
        ):
            settings = geometry(ffd=parameters)
            self.assertFalse(settings.geometry_noise.has_geometry)
            self.assertEqual(settings.padding_for((64, 64), 8), 0)
            with patch.object(ffd, "sample_controls") as sample:
                result = arrays(render(settings, scale=1))[0]
            sample.assert_not_called()
            np.testing.assert_array_equal(result, images.COLOR_LOOKUP[board()])


class CompositionTests(unittest.TestCase):
    def test_independent_switches_and_sizes(self):
        for h, f in itertools.product((False, True), repeat=2):
            settings = geometry(
                homography=replace(CLEAN.geometry_noise.homography, enabled=h),
                ffd=replace(CLEAN.geometry_noise.ffd, enabled=f),
            )
            padding = settings.padding_for((64, 64), 8)
            self.assertEqual(padding, {(False, False): 0, (False, True): 88,
                                       (True, False): 144, (True, True): 224}[h, f])
            with patch.object(cv2, "remap", wraps=cv2.remap) as sample:
                result = arrays(render(settings, scale=8))[0]
            self.assertEqual(sample.call_count, int(h or f))
            self.assertEqual(result.shape, (512 + 2*padding, 512 + 2*padding, 3))

    def test_composed_map_matches_homogeneous_coordinate_formula(self):
        settings = replace(CLEAN.geometry_noise, homography=replace(CLEAN.geometry_noise.homography, enabled=True))
        plan = images._geometry_plan(settings, (64, 64), 2)
        sample = images._sample_geometry(settings, (plan,), np.random.default_rng(25))
        maps = images._geometry_maps(settings, sample, plan, (64, 64), 2)
        elastic = ffd.build_maps(plan, sample.controls, images._ffd_amplitudes(settings, plan.shape))
        inverse = np.linalg.inv(images._geometry_matrix(sample.homography, (64, 64), 2, 0))
        points = np.column_stack([elastic[0][::11, ::13].ravel(), elastic[1][::11, ::13].ravel(),
                                  np.ones(elastic[0][::11, ::13].size)])
        projected = points @ inverse.T
        expected = projected[:, :2] / projected[:, 2, None]
        np.testing.assert_allclose(maps[0][::11, ::13].ravel(), expected[:, 0], atol=2e-5)
        np.testing.assert_allclose(maps[1][::11, ::13].ravel(), expected[:, 1], atol=2e-5)

    def test_combined_padding_contains_extreme_homographies_and_ffd_bounds(self):
        for shape in ((64, 64), (32, 64)):
            height, width = np.array(shape) * 8
            for method, radius in (("nearest", 1), ("bilinear", 1), ("bicubic", 2)):
                settings = replace(CLEAN.geometry_noise, resampling=method,
                                   homography=replace(CLEAN.geometry_noise.homography, enabled=True))
                pad = settings.padding_for(shape, 8)
                ffd_bound = sum(images._ffd_amplitudes(settings, (height, width)))
                corners = np.array([[-radius, -radius, 1], [width+radius, -radius, 1],
                                    [width+radius, height+radius, 1], [-radius, height+radius, 1]])
                for signs in itertools.product((-1, 1), repeat=8):
                    rng = Mock()
                    rng.uniform.return_value = np.array(signs)
                    sample = images._sample_homography(settings.homography, rng)
                    projected = corners @ images._geometry_matrix(sample, shape, 8, pad).T
                    points = projected[:, :2] / projected[:, 2, None]
                    self.assertTrue(np.all(points - ffd_bound >= 4 - 1e-7))
                    self.assertTrue(np.all(points + ffd_bound <= [width+2*pad-4+1e-7, height+2*pad-4+1e-7]))

    def test_larger_manual_canvas_preserves_warp_and_loses_no_content(self):
        source = np.full((128, 128, 3), 255, dtype=np.uint8)
        for h, method in itertools.product((False, True), ("nearest", "bilinear", "bicubic")):
            settings = replace(CLEAN.geometry_noise, resampling=method,
                               homography=replace(CLEAN.geometry_noise.homography, enabled=h))
            plan = images._geometry_plan(settings, (64, 64), 2)
            sample = images._sample_geometry(settings, (plan,), np.random.default_rng(13))
            for coefficients in (sample.controls, tuple(np.ones_like(c) for c in sample.controls)):
                sample = replace(sample, controls=coefficients)
                maps = images._geometry_maps(settings, sample, plan, (64, 64), 2)
                warped = cv2.remap(source, *maps, images.RESAMPLING_METHODS[method])
                extra = 19
                larger_settings = replace(settings, padding=plan.padding + extra)
                larger_plan = images._geometry_plan(larger_settings, (64, 64), 2)
                larger_maps = images._geometry_maps(larger_settings, sample, larger_plan, (64, 64), 2)
                large = cv2.remap(source, *larger_maps, images.RESAMPLING_METHODS[method])
                np.testing.assert_array_equal(warped, large[extra:-extra, extra:-extra])
                large[extra:-extra, extra:-extra] = 0
                self.assertFalse(large.any())
                for edge in (warped[:4], warped[-4:], warped[:, :4], warped[:, -4:]):
                    self.assertFalse(edge.any())


class RenderingTests(unittest.TestCase):
    def test_frame_and_step_refresh_are_independent(self):
        for geometric, color in itertools.product(("frame", "step"), repeat=2):
            settings = geometry(SETTINGS, geometry_refresh=geometric)
            settings = replace(settings, color_noise=replace(settings.color_noise, noise_refresh=color))
            with patch.object(ffd, "sample_controls", wraps=ffd.sample_controls) as sample, \
                 patch.object(images, "_sample_pixel_noise", wraps=images._sample_pixel_noise) as pixel, \
                 patch.object(images, "_geometry_maps", wraps=images._geometry_maps) as maps:
                result = arrays(render(settings, [board()] * 3))
            self.assertEqual(sample.call_count, 1 if geometric == "step" else 3)
            self.assertEqual(maps.call_count, 1 if geometric == "step" else 3)
            self.assertEqual(pixel.call_count, 1 if color == "step" else 3)
            self.assertEqual(np.array_equal(result[0], result[1]), geometric == color == "step")

    def test_step_controls_are_shared_and_validated_across_shapes(self):
        settings = geometry(SETTINGS, geometry_refresh="step")
        settings = replace(settings, color_noise=replace(settings.color_noise, noise_refresh="step"))
        with patch.object(ffd, "sample_controls", wraps=ffd.sample_controls) as sample:
            result = arrays(render(settings, [board(), board((32, 64)), board()]))
        sample.assert_called_once()
        self.assertEqual(len(sample.call_args.args[0]), 2)
        np.testing.assert_array_equal(result[0], result[2])
        self.assertNotEqual(result[0].shape, result[1].shape)

    def test_gaussian_noise_is_added_after_all_geometry_including_padding(self):
        settings = geometry(CLEAN, homography=replace(CLEAN.geometry_noise.homography, enabled=True))
        clean = arrays(render(settings))[0]
        noise = np.random.default_rng(90).normal(0, 20, size=clean.shape)
        noise[0, 0] = [10, 20, 30]
        colored = replace(settings, color_noise=replace(settings.color_noise, enabled=True, palette_noise=0))
        with patch.object(images, "_sample_pixel_noise", return_value=noise):
            actual = arrays(render(colored))[0]
        expected = np.rint(np.clip(clean.astype(float) + noise, 0, 255)).astype(np.uint8)
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(actual[0, 0], [10, 20, 30])

    def test_jpeg_25_matches_encoding_the_same_png_pixels(self):
        png = arrays(render(SETTINGS, scale=8))[0]
        jpg = render(SETTINGS, scale=8, image_format="jpg")["final_frame.jpg"]
        expected = BytesIO()
        Image.fromarray(png).save(expected, format="JPEG", quality=25)
        self.assertEqual(jpg, expected.getvalue())
        with Image.open(BytesIO(jpg)) as image:
            self.assertEqual(image.size, (688, 688))
            self.assertEqual(image.quantization[0][0], 32)

    def test_arbitrary_grids_work_through_public_renderer(self):
        raw = deepcopy(RAW)
        raw["image_noise"]["geometry_noise"]["ffd"]["grids"].append(
            {"grid_shape": [5, 7], "amplitude_fraction": 0.005},
        )
        settings = environment.EnvironmentManagerParameters.from_dict(raw).image_noise
        result = arrays(render(settings))[0]
        side = 128 + 2 * settings.padding_for((64, 64), 2)
        self.assertEqual(result.shape, (side, side, 3))

    def test_manager_start_move_reset_keep_original_action_coordinates(self):
        backend = Mock()
        backend.make.return_value.observation_space = observation([board()])
        backend.make.return_value.step.return_value = observation([board(), board()])
        backend.make.return_value.reset.return_value = observation([board()])
        with patch.object(environment, "Arcade", return_value=backend):
            manager = environment.EnvironmentManager(deepcopy(RAW))
            started = manager.start_environment("test-game").observation
            moved = manager.move({"id": "ACTION6", "data": {"x": 63, "y": 0}})
            reset = manager.move({"id": "RESET"})
            manager.stop()
        backend.make.return_value.step.assert_called_once_with(environment.GameAction.ACTION6, data={"x": 63, "y": 0})
        for output in (started, moved, reset):
            self.assertIn("final_frame.jpg", output.files)
            for name, data in output.files.items():
                if name.endswith(".jpg"):
                    with Image.open(BytesIO(data)) as image:
                        self.assertEqual(image.size, (896, 896))
                        self.assertEqual(image.quantization[0][0], 32)
        metadata = json.loads(moved.files["metadata.txt"])
        self.assertEqual(metadata["action_input"], {"id": "ACTION6", "data": {"x": 63, "y": 0}})
        self.assertNotIn("image_noise", metadata)


if __name__ == "__main__":
    unittest.main()
