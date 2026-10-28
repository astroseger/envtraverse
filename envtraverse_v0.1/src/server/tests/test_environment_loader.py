from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from environment_loader import available_environments, load_environment_manager


class EnvironmentLoaderTest(unittest.TestCase):
    def test_manager_requires_start_move_and_stop(self) -> None:
        class Manager:
            def start_environment(self, game_id):
                pass

            def move(self, action):
                pass

            def stop(self):
                pass

        with patch("environment_loader.import_module", return_value=SimpleNamespace(EnvironmentManager=Manager)):
            self.assertIs(load_environment_manager("example"), Manager)

    def test_incomplete_manager_is_rejected(self) -> None:
        class IncompleteManager:
            def start_environment(self, game_id):
                pass

        with patch(
            "environment_loader.import_module", return_value=SimpleNamespace(EnvironmentManager=IncompleteManager)
        ):
            with self.assertRaisesRegex(TypeError, "start_environment.*move.*stop"):
                load_environment_manager("example")

    def test_discovers_the_environments_without_importing_them(self) -> None:
        with patch("environment_loader.import_module") as import_module:
            self.assertTrue({
                "agimaze_set_v1", "agimaze_single", "arc3", "example", "terminal_bench",
                "arc3_minimal_all", "arc3_minimal_ascii", "arc3_minimal_png", "arc3_minimal_raw",
                "arc3_minimal_img_wnoise3", "arc3_minimal_img_wnoise3_whighlight2",
            } <= set(available_environments()))
            import_module.assert_not_called()

    def test_unknown_names_cannot_import_other_modules(self) -> None:
        with patch("environment_loader.import_module") as import_module:
            for name in ("", "../arc3", "arc3.environment_manager", "/tmp/example", "missing"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Available environments"):
                    load_environment_manager(name)
            import_module.assert_not_called()

    def test_missing_dependency_has_environment_context(self) -> None:
        error = ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency")
        with patch("environment_loader.import_module", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "arc3.*missing_dependency"):
                load_environment_manager("arc3")

    def test_import_and_help_work_without_game_configuration_or_arc_dependencies(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(SERVER_DIR), PYTHONDONTWRITEBYTECODE="1")
        with tempfile.TemporaryDirectory() as directory:
            help_result = subprocess.run(
                [sys.executable, str(SERVER_DIR / "server.py"), "--help"],
                cwd=directory, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            choices = help_result.stdout.split("--environment {", 1)[1].split("}", 1)[0].split(",")
            self.assertEqual(choices, available_environments())
            script = (
                "import sys; "
                "sys.modules.update({name: None for name in ('arc_agi', 'arcengine', 'numpy', 'PIL')}); "
                "import server; "
                "from environment_loader import load_environment_manager; "
                "load_environment_manager('example')({}).stop(); "
                "load_environment_manager('agimaze_single')({}).stop(); "
                "load_environment_manager('agimaze_set_v1')({}).stop()"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
