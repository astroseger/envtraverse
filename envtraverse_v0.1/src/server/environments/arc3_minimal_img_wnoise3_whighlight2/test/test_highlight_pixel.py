from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import itertools
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image

from test_image_noise import CLEAN, RAW, SETTINGS, board, environment, ffd, images, observation
from environments.arc3_minimal_img_wnoise3 import artifacts_preprocessing as original


def highlight(x=12, y=34):
    return {"id": "highlight_pixel", "data": {"x": x, "y": y}}


def decode(files, name="frame.png"):
    with Image.open(BytesIO(files[name])) as image:
        return np.array(image)


def pair(settings=SETTINGS, *, pixel=(12, 34), scale=2, mouse_size=48, seed=17,
         frames=None, image_format="png", rng=None):
    return images.build_highlight_files(
        observation([board()] if frames is None else frames), pixel,
        image_scale=scale, image_format=image_format, image_quality=25, image_noise=settings,
        mouse_size=mouse_size, rng=np.random.default_rng(seed) if rng is None else rng,
    )


def variant(homography, elastic, *, settings=CLEAN, **changes):
    geometry = settings.geometry_noise
    return replace(settings, geometry_noise=replace(
        geometry, homography=replace(geometry.homography, enabled=homography),
        ffd=replace(geometry.ffd, enabled=elastic), **changes,
    ))


class CursorMappingTests(unittest.TestCase):
    def test_identity_manual_padding_and_master_disable(self):
        for enabled, padding, expected_padding in ((True, "auto", 0), (True, 13, 13), (False, 100, 0)):
            settings = variant(False, False, enabled=enabled, padding=padding).geometry_noise
            sample = images._sample_geometry(settings, (), np.random.default_rng(1))
            for shape, pixel, scale in (((64, 64), (12, 34), 8), ((36, 64), (63, 0), 3), ((64, 64), (0, 63), 1)):
                with self.subTest(enabled=enabled, padding=padding, pixel=pixel):
                    center = images.board_pixel_center(pixel, shape, scale, settings, sample)
                    expected = (np.array(pixel) + 0.5) * scale + expected_padding
                    np.testing.assert_allclose(center, expected, atol=1e-10)

    def test_constant_multigrid_displacement_moves_content_in_the_opposite_direction(self):
        for homography in (False, True):
            settings = variant(homography, True).geometry_noise
            shape, scale, pixel = (36, 64), 5, (51, 13)
            plan = images._geometry_plan(settings, shape, scale)
            sample = images._sample_geometry(settings, (plan,), np.random.default_rng(4))
            offsets = ((0.4, -0.3), (-0.5, 0.5))
            controls = tuple(np.broadcast_to(offset, (*grid, 2)).copy()
                             for grid, offset in zip(plan.grid_shapes, offsets))
            sample = replace(sample, controls=controls)
            source = (np.array(pixel) + 0.5) * scale - 0.5
            projected = images._geometry_matrix(sample.homography, shape, scale, 0) @ np.r_[source, 1]
            shift = sum(np.array(offset) * amplitude for offset, amplitude in
                        zip(offsets, images._ffd_amplitudes(settings, plan.shape)))
            expected = projected[:2] / projected[2] - shift + plan.padding + 0.5
            actual = images.board_pixel_center(pixel, shape, scale, settings, sample, plan=plan)
            np.testing.assert_allclose(actual, expected, atol=1e-6)

    def test_point_evaluation_matches_raster_maps_and_finite_difference(self):
        plan = ffd.make_plan((128, 160), 24, 16, ((4, 4), (8, 8), (5, 7), (4, 4)))
        rng = np.random.default_rng(6)
        controls = tuple(rng.uniform(-1, 1, (*shape, 2)).astype(np.float32) for shape in plan.grid_shapes)
        amplitudes = (16, 4, 0.5, 0.25)
        maps = ffd.build_maps(plan, controls, amplitudes)
        for column, row in ((0, 0), (30, 70), (207, 175), (100, 90)):
            point = np.array([column - plan.padding, row - plan.padding], dtype=float)
            actual, jacobian = ffd.point_map_and_jacobian(plan, controls, amplitudes, point)
            np.testing.assert_allclose(actual, [maps[0][row, column], maps[1][row, column]], atol=3e-5)
            delta = 1e-3
            numerical = np.column_stack([
                (ffd.point_map_and_jacobian(plan, controls, amplitudes, point + axis * delta)[0]
                 - ffd.point_map_and_jacobian(plan, controls, amplitudes, point - axis * delta)[0]) / (2 * delta)
                for axis in np.eye(2)
            ])
            np.testing.assert_allclose(jacobian, numerical, atol=1e-7)

    def test_cursor_center_lands_in_the_selected_rendered_cell_for_all_geometry_modes(self):
        cases = list(itertools.product((False, True), repeat=2))
        for homography, elastic in cases:
            settings = variant(homography, elastic)
            shape, scale = (64, 64), 8
            plan = images._geometry_plan(settings.geometry_noise, shape, scale)
            for seed in (0, 4, 17, 22):
                sample = images._sample_geometry(settings.geometry_noise, (plan,), np.random.default_rng(seed))
                maps = images._geometry_maps(settings.geometry_noise, sample, plan, shape, scale)
                for pixel in ((0, 0), (63, 63), (63, 0), (0, 63), (12, 34), (32, 32)):
                    with self.subTest(homography=homography, elastic=elastic, seed=seed, pixel=pixel):
                        grid = np.full(shape, 3, dtype=np.int16)
                        grid[pixel[1], pixel[0]] = 8
                        rendered = images._frame_to_image(grid, scale, settings, maps, None, None)
                        center = images.board_pixel_center(
                            pixel, shape, scale, settings.geometry_noise, sample, plan=plan, geometry_maps=maps,
                        )
                        self.assertEqual(rendered.getpixel(tuple(math.floor(v) for v in center)),
                                         tuple(images.COLOR_LOOKUP[8]))
                        # Interpolate the actual raster sampling maps to check the
                        # geometric position independently of the point solver.
                        px, py = np.array(center) - 0.5
                        x0, y0 = math.floor(px), math.floor(py)
                        wx, wy = px - x0, py - y0
                        source = [(1-wy)*((1-wx)*m[y0, x0] + wx*m[y0, x0+1])
                                  + wy*((1-wx)*m[y0+1, x0] + wx*m[y0+1, x0+1]) for m in maps]
                        np.testing.assert_allclose(source, (np.array(pixel)+0.5)*scale-0.5, atol=0.01)

    def test_inverse_fallback_refines_from_existing_map_and_failure_is_explicit(self):
        settings = variant(True, True).geometry_noise
        plan = images._geometry_plan(settings, (64, 64), 2)
        sample = images._sample_geometry(settings, (plan,), np.random.default_rng(9))
        maps = images._geometry_maps(settings, sample, plan, (64, 64), 2)
        expected = images.board_pixel_center((12, 34), (64, 64), 2, settings, sample, plan=plan)
        real_inverse = ffd.invert_point

        def fail_first(*args, **kwargs):
            if "initial" not in kwargs:
                raise ValueError("Simulated difficult initial guess")
            return real_inverse(*args, **kwargs)

        with patch.object(ffd, "invert_point", side_effect=fail_first) as solve:
            actual = images.board_pixel_center((12, 34), (64, 64), 2, settings, sample,
                                              plan=plan, geometry_maps=maps)
        self.assertEqual(solve.call_count, 2)
        np.testing.assert_allclose(actual, expected, atol=1e-5)
        with patch.object(ffd, "invert_point", side_effect=ValueError("Unable to solve")):
            with self.assertRaisesRegex(ValueError, "Unable to solve"):
                images.board_pixel_center((12, 34), (64, 64), 2, settings, sample, plan=plan, geometry_maps=maps)
        # A singular local Jacobian makes this Newton start unsolvable.
        singular_plan = ffd.make_plan((31, 31), 0, 0, ((4, 4),))
        controls = np.zeros((4, 4, 2))
        controls[:, :, 0] = -np.linspace(0, 1, 4)[None, :]
        with self.assertRaisesRegex(ValueError, "subpixel accuracy"):
            real_inverse(singular_plan, (controls,), (30,), np.array([10., 15.]))

    def test_sprite_size_hotspot_and_edge_clipping(self):
        source, original_tip = images._mouse_cursor_asset()
        for size in (1, 24, 48, 96):
            sprite, hotspot = images._mouse_cursor_sprite(size)
            self.assertEqual(sprite.height, size)
            self.assertLessEqual(abs(sprite.width - source.width * size / source.height), 0.5)
            np.testing.assert_allclose(np.array(hotspot) / sprite.size, np.array(original_tip) / source.size)
        canvas = Image.new("RGB", (40, 30), (70, 120, 180))
        for center in ((0, 0), (39.5, 0), (0, 29.5), (39.5, 29.5), (-200, -200), (200, 200)):
            small = images.overlay_mouse_cursor(canvas, center, 48)
            padded = Image.new("RGB", (640, 630), (70, 120, 180))
            padded = images.overlay_mouse_cursor(padded, tuple(v + 300 for v in center), 48)
            self.assertEqual(small.size, canvas.size)
            np.testing.assert_array_equal(small, padded.crop((300, 300, 340, 330)))


class HighlightRenderingTests(unittest.TestCase):
    def test_all_palette_colors_can_be_highlighted_at_edges_in_every_geometry_mode(self):
        edge_points = ((63, 63), (0, 0), (0, 63), (63, 0), (32, 0), (0, 32), (63, 32), (32, 63))
        for h, f in itertools.product((False, True), repeat=2):
            settings = variant(h, f, settings=SETTINGS)
            for color in range(len(images.COLOR_LOOKUP)):
                pixel = edge_points[color % len(edge_points)]
                grid = board()
                grid[pixel[1], pixel[0]] = color
                with self.subTest(homography=h, ffd=f, color=color, pixel=pixel):
                    files = pair(settings, pixel=pixel, frames=[grid], scale=8,
                                 seed=100 + color, image_format="jpg")
                    self.assertEqual(json.loads(files["metadata.txt"])["action_input"], highlight(*pixel))
                    side = 512 + 2 * settings.padding_for((64, 64), 8)
                    for name in ("frame.jpg", "frame_highlighted.jpg"):
                        with Image.open(BytesIO(files[name])) as rendered:
                            rendered.load()
                            self.assertEqual(rendered.size, (side, side))

    def test_normal_rendering_matches_v3_byte_for_byte(self):
        for h, f, refresh, image_format in itertools.product((False, True), (False, True),
                                                           ("frame", "step"), ("png", "jpg")):
            raw = deepcopy(RAW["image_noise"])
            raw["geometry_noise"]["homography"]["enabled"] = h
            raw["geometry_noise"]["ffd"]["enabled"] = f
            raw["geometry_noise"]["geometry_refresh"] = raw["color_noise"]["noise_refresh"] = refresh
            kwargs = dict(image_scale=2, image_format=image_format, image_quality=25)
            frame = observation([board(), board()])
            before = original.build_files(frame, image_noise=original.ImageNoiseParameters.from_dict(raw),
                                          rng=np.random.default_rng(71), **kwargs)
            after = images.build_files(frame, image_noise=images.ImageNoiseParameters.from_dict(raw),
                                       rng=np.random.default_rng(71), **kwargs)
            metadata = json.loads(before.pop("metadata.txt"))
            metadata["available_actions"].append("highlight_pixel")
            self.assertEqual(json.loads(after.pop("metadata.txt")), metadata)
            self.assertEqual(before, after)

    def test_pair_uses_last_frame_without_mutation_and_advertises_highlight(self):
        settings = variant(False, False)
        last = np.full((64, 64), 8, dtype=np.int16)
        last.setflags(write=False)
        files = pair(settings, frames=[board(), last], mouse_size=0, scale=1)
        self.assertEqual(set(files), {"metadata.txt", "frame.png", "frame_highlighted.png"})
        expected = np.broadcast_to(images.COLOR_LOOKUP[8], (64, 64, 3))
        np.testing.assert_array_equal(decode(files), expected)
        np.testing.assert_array_equal(decode(files, "frame_highlighted.png"), expected)
        self.assertTrue(np.all(last == 8))
        metadata = json.loads(files["metadata.txt"])
        self.assertEqual(metadata["action_input"], highlight())
        self.assertEqual(metadata["available_actions"], ["ACTION1", "ACTION6", "highlight_pixel"])

    def test_shared_fresh_geometry_and_independent_color_noise_in_every_refresh_mode(self):
        for geometry_mode, color_mode in itertools.product(("step", "frame"), repeat=2):
            for palette, pixels in ((5, 0), (0, 20), (5, 20)):
                settings = variant(True, True, settings=SETTINGS, geometry_refresh=geometry_mode)
                settings = replace(settings, color_noise=replace(
                    settings.color_noise, palette_noise=palette, pixel_noise_std=pixels, noise_refresh=color_mode,
                ))
                with patch.object(images, "_sample_geometry", wraps=images._sample_geometry) as sample, \
                     patch.object(images, "_frame_to_image_bytes", wraps=images._frame_to_image_bytes) as encode:
                    files = pair(settings, mouse_size=0)
                self.assertEqual(sample.call_count, 1)
                left, right = encode.call_args_list
                self.assertIs(left.args[3], right.args[3])  # Exact same homography + FFD map.
                if palette:
                    self.assertFalse(np.array_equal(left.args[4], right.args[4]))
                if pixels:
                    self.assertFalse(np.array_equal(left.args[5], right.args[5]))
                self.assertNotEqual(files["frame.png"], files["frame_highlighted.png"])
                rng = np.random.default_rng(91)
                self.assertNotEqual(pair(settings, mouse_size=0, rng=rng)["frame.png"],
                                    pair(settings, mouse_size=0, rng=rng)["frame.png"])

    def test_cursor_is_composited_after_all_noise_and_geometry(self):
        settings = variant(True, True, settings=SETTINGS)
        plain = pair(settings, mouse_size=0, scale=8)
        with patch.object(images, "overlay_mouse_cursor", wraps=images.overlay_mouse_cursor) as overlay:
            marked = pair(settings, mouse_size=48, scale=8)
        self.assertEqual(overlay.call_count, 1)
        self.assertEqual(plain["frame.png"], marked["frame.png"])
        base, actual = decode(plain, "frame_highlighted.png"), decode(marked, "frame_highlighted.png")
        np.testing.assert_array_equal(overlay.call_args.args[0], base)
        _, center, size = overlay.call_args.args
        self.assertEqual(size, 48)
        sprite, hotspot = images._mouse_cursor_sprite(size)
        left, top = (math.floor(value - tip + 0.5) for value, tip in zip(center, hotspot))
        self.assertTrue(np.all(np.abs(np.array([left, top]) + hotspot - center) <= 0.5))
        rgba = np.asarray(sprite)
        opaque = rgba[:, :, 3] == 255
        region = actual[top:top+sprite.height, left:left+sprite.width]
        np.testing.assert_array_equal(region[opaque], rgba[:, :, :3][opaque])
        allowed = np.zeros(actual.shape[:2], dtype=bool)
        allowed[top:top+sprite.height, left:left+sprite.width] = rgba[:, :, 3] > 0
        np.testing.assert_array_equal(actual[~allowed], base[~allowed])
        self.assertTrue(np.any(actual[allowed] != base[allowed]))

    def test_jpeg_quality_25_encodes_both_fully_rendered_images(self):
        png = pair(scale=8)
        jpg = pair(scale=8, image_format="jpg")
        for name in ("frame", "frame_highlighted"):
            with Image.open(BytesIO(png[f"{name}.png"])) as image:
                expected = BytesIO()
                image.save(expected, format="JPEG", quality=25)
            self.assertEqual(jpg[f"{name}.jpg"], expected.getvalue())
            with Image.open(BytesIO(jpg[f"{name}.jpg"])) as image:
                self.assertEqual(image.size, (688, 688))
                self.assertEqual(image.quantization[0][0], 32)

    def test_geometry_disabled_has_no_padding_and_color_disabled_draws_no_noise(self):
        settings = variant(True, True, enabled=False, padding=500)
        with patch.object(images, "_sample_palette_noise") as palette, \
             patch.object(images, "_sample_pixel_noise") as pixels, \
             patch.object(cv2, "remap") as remap:
            files = pair(settings, scale=8)
        palette.assert_not_called()
        pixels.assert_not_called()
        remap.assert_not_called()
        self.assertEqual(decode(files).shape, (512, 512, 3))
        self.assertEqual(decode(files, "frame_highlighted.png").shape, (512, 512, 3))

    def test_zero_mouse_avoids_cursor_asset_and_inverse_solver(self):
        with patch.object(images, "_mouse_cursor_sprite") as sprite, patch.object(ffd, "invert_point") as inverse:
            pair(mouse_size=0)
        sprite.assert_not_called()
        inverse.assert_not_called()

    def test_invalid_or_missing_frames_and_out_of_frame_points_are_rejected(self):
        for frames in ([], [None], [[]], [[1, 2, 3]], [[[[1]]]]):
            with self.subTest(frames=frames), self.assertRaises((ValueError, TypeError)):
                pair(frames=frames)
        for pixel in ((-1, 0), (64, 0), (1.5, 2), (True, 3)):
            with self.subTest(pixel=pixel), self.assertRaisesRegex(ValueError, "indices"):
                pair(pixel=pixel)


class HighlightManagerTests(unittest.TestCase):
    def setUp(self):
        self.frame = observation([board(), board()])
        self.frame.action_input = SimpleNamespace(id=environment.GameAction.ACTION6, data={"x": 1, "y": 2})
        self.wrapper = Mock(observation_space=self.frame)
        self.wrapper.step.return_value = self.frame
        self.wrapper.reset.return_value = self.frame
        self.arc = Mock()
        self.arc.make.return_value = self.wrapper
        self.raw = deepcopy(RAW)
        self.raw.update(image_scale=1, image_format="png")
        self.raw["image_noise"]["geometry_noise"]["enabled"] = False
        self.raw["image_noise"]["color_noise"]["enabled"] = False

    def start(self):
        with patch.object(environment, "Arcade", return_value=self.arc):
            manager = environment.EnvironmentManager(self.raw)
            started = manager.start_environment("test-game")
        self.addCleanup(manager.stop)
        return manager, started

    def test_mouse_size_is_required_and_validated_before_backend_start(self):
        self.assertEqual(environment.EnvironmentManagerParameters.from_dict(RAW).mouse_size, 48)
        for value in (0, 1, 48, 96):
            self.assertEqual(environment.EnvironmentManagerParameters.from_dict(dict(RAW, mouse_size=value)).mouse_size,
                             value)
        with patch.object(environment, "Arcade") as backend:
            for value in (-1, True, False, 48.0, "48", None):
                with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                    environment.EnvironmentManager(dict(RAW, mouse_size=value))
            missing = deepcopy(RAW)
            del missing["mouse_size"]
            with self.assertRaisesRegex(ValueError, "mouse_size"):
                environment.EnvironmentManager(missing)
        backend.assert_not_called()

    def test_all_4096_coordinate_pairs_are_dispatched_without_advancing_the_game(self):
        manager, _ = self.start()
        # Isolate input validation and dispatch; the rendering tests separately
        # exercise geometry, cursor clipping, and every palette color.
        with patch.object(environment, "build_highlight_files", return_value={"metadata.txt": "{}"}) as render:
            for y, x in itertools.product(range(64), repeat=2):
                result = manager.move(highlight(x, y))
                self.assertEqual(render.call_args.kwargs["highlight_pixel"], (x, y))
                self.assertFalse(result.terminal)
            self.assertEqual(render.call_count, 4096)
        self.wrapper.step.assert_not_called()
        self.wrapper.reset.assert_not_called()
        self.assertEqual((manager.current_level_steps, manager.current_attempt_steps), (0, 0))

    def test_out_of_bounds_on_either_axis_is_rejected_before_rendering(self):
        manager, _ = self.start()
        with patch.object(environment, "build_highlight_files") as render:
            for outside, inside in itertools.product((-10000, -1, 64, 10000), range(64)):
                for pixel in ((outside, inside), (inside, outside)):
                    for x, y in (pixel, tuple(str(value) for value in pixel)):
                        with self.subTest(x=x, y=y), self.assertRaisesRegex(environment.InvalidActionError, "0..63"):
                            manager.move(highlight(x, y))
            render.assert_not_called()
        self.wrapper.step.assert_not_called()
        self.wrapper.reset.assert_not_called()
        self.assertEqual((manager.current_level_steps, manager.current_attempt_steps), (0, 0))

    def test_highlight_preserves_budget_cached_frame_and_previous_action(self):
        manager, _ = self.start()
        manager.current_level_steps = environment.MAX_STEPS_PER_LEVEL
        manager.current_attempt_steps = 123
        for pixel in ((12, 34), (0, 0), (63, 63)):
            result = manager.move(highlight(*pixel))
            self.assertFalse(result.terminal)
            self.assertEqual((manager.current_level_steps, manager.current_attempt_steps),
                             (environment.MAX_STEPS_PER_LEVEL, 123))
            self.assertEqual(json.loads(result.files["metadata.txt"])["action_input"], highlight(*pixel))
            self.assertIs(self.wrapper.observation_space, self.frame)
            self.assertEqual(self.frame.action_input.data, {"x": 1, "y": 2})
        self.wrapper.step.assert_not_called()
        self.wrapper.reset.assert_not_called()
        self.arc.create_scorecard.assert_not_called()
        regular = manager.move({"id": "ACTION1"})
        self.assertTrue(regular.terminal)
        self.assertNotIn("frame_highlighted.png", regular.files)
        self.wrapper.step.assert_called_once_with(environment.GameAction.ACTION1, data=None)

    def test_highlighting_after_move_and_reset_reads_latest_observation(self):
        manager, _ = self.start()
        manager.move({"id": "ACTION6", "data": {"x": "63", "y": "0"}})
        new_frame = observation([np.full((64, 64), 8)])
        self.wrapper.observation_space = new_frame
        with patch.object(environment, "build_highlight_files", wraps=images.build_highlight_files) as render:
            manager.move(highlight("0", "63"))
            self.assertIs(render.call_args.args[0], new_frame)
            self.assertEqual(render.call_args.kwargs["highlight_pixel"], (0, 63))
        manager.move({"id": "RESET"})
        manager.move(highlight())
        self.assertEqual((manager.current_level_steps, manager.current_attempt_steps), (2, 0))
        self.wrapper.step.assert_called_once_with(environment.GameAction.ACTION6, data={"x": 63, "y": 0})
        self.wrapper.reset.assert_called_once_with()

    def test_validation_terminal_and_game_over_match_existing_highlight_behavior(self):
        manager, _ = self.start()
        invalid = [None, [], {}, {"id": None}, {"id": ""}, {"id": "unknown"},
                   {"id": "highlight_pixel"}, {"id": "highlight_pixel", "data": []},
                   {"id": "highlight_pixel", "data": {"x": 0}}]
        invalid += [highlight(value, 0) for value in (True, -1, 64, 1.0, None, "1.5")]
        invalid += [highlight(0, value) for value in (True, -1, 64, 1.0, None, "1.5")]
        with patch.object(environment, "build_highlight_files") as render:
            for action in invalid:
                with self.subTest(action=action), self.assertRaises(environment.InvalidActionError):
                    manager.move(action)
            render.assert_not_called()
        self.wrapper.step.assert_not_called()
        self.wrapper.reset.assert_not_called()
        self.assertEqual((manager.current_level_steps, manager.current_attempt_steps), (0, 0))
        with self.assertRaises(environment.InvalidActionError):
            manager.move({"id": "RESET"})
        manager.state_name = "GAME_OVER"
        manager.move(highlight())
        with self.assertRaises(environment.InvalidActionError):
            manager.move({"id": "ACTION1"})
        manager.terminal = True
        with self.assertRaisesRegex(environment.InvalidActionError, "terminal"):
            manager.move(highlight())
        manager.stop()
        with self.assertRaisesRegex(environment.EnvironmentError, "closed"):
            manager.move(highlight())

    def test_missing_settled_observation_is_fatal(self):
        manager, _ = self.start()
        for frame in (None, observation([])):
            self.wrapper.observation_space = frame
            with self.assertRaisesRegex(environment.FatalEnvironmentError, "settled frame"):
                manager.move(highlight())


if __name__ == "__main__":
    unittest.main()
