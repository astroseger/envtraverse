from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import itertools
import json
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
from environments.arc3_minimal_img_wnoise3_whighlight2 import artifacts_preprocessing as images
from environments.arc3_minimal_img_wnoise3_whighlight2 import environment_manager as environment
from environments.arc3_minimal_img_wnoise3_whighlight2 import ffd

PROJECT = Path(__file__).resolve().parents[5]
CONFIG = yaml.safe_load((PROJECT / "run_config_arc3_minimal_img_wnoise3_whighlight2.yaml").read_text())
RAW = CONFIG["environment_parameters"]
SETTINGS = images.ImageNoiseParameters.from_dict(RAW["image_noise"])
CLEAN = replace(SETTINGS, color_noise=replace(SETTINGS.color_noise, enabled=False))


def observation(frames):
    return SimpleNamespace(frame=frames, state=SimpleNamespace(name="NOT_FINISHED"),
                           levels_completed=0, win_levels=1, available_actions=[1, 6], action_input=None)


def board(shape=(64, 64)):
    return np.arange(np.prod(shape), dtype=np.int16).reshape(shape) % 16


def render(settings, frames=None, scale=2, seed=17, image_format="png"):
    return images.build_files(
        observation([board()] if frames is None else frames),
        image_scale=scale, image_format=image_format, image_quality=25,
        image_noise=settings, rng=np.random.default_rng(seed),
    )


def arrays(files):
    return [np.array(Image.open(BytesIO(data))) for name, data in files.items() if name.endswith(".png")]


def geometry(settings=CLEAN, **changes):
    return replace(settings, geometry_noise=replace(settings.geometry_noise, **changes))


class ConfigurationTests(unittest.TestCase):
    def test_example_preserves_v3_parameters_and_adds_mouse_size(self):
        previous = yaml.safe_load((PROJECT / "run_config_arc3_minimal_img_wnoise3.yaml").read_text())
        expected = deepcopy(previous)
        expected["environment"] = "arc3_minimal_img_wnoise3_whighlight2"
        expected["environment_parameters"]["mouse_size"] = 48
        self.assertEqual(CONFIG, expected)
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
        for group in ("homography", "ffd"):
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

    def test_known_combined_rejections_and_retry_limit(self):
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
        with patch.object(ffd, "minimum_jacobian", return_value=-1) as check:
            with self.assertRaisesRegex(ValueError, "100 attempts"):
                ffd.sample_controls((plan,), ((64, 16),), np.random.default_rng(seed), 0.1)
            self.assertEqual(check.call_count, 100)

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
                        self.assertEqual(image.size, (688, 688))
                        self.assertEqual(image.quantization[0][0], 32)
        metadata = json.loads(moved.files["metadata.txt"])
        self.assertEqual(metadata["action_input"], {"id": "ACTION6", "data": {"x": 63, "y": 0}})
        self.assertNotIn("image_noise", metadata)


if __name__ == "__main__":
    unittest.main()
