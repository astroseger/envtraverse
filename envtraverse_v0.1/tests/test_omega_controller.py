"""Offline contracts for the separate Omega account controller.

All account files are temporary fixtures. Docker and account preflight processes
are mocked; these tests never open real credentials or send model requests.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_controller_omega as controller
import run_funs as shared
import run_funs_omega as omega
from agent_api import AgentMount, LaunchPreparation


class OmegaFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source_accounts = self.directory / 'original accounts'
        self.source_accounts.mkdir()
        self.paths = omega.RunPaths(self.directory / 'experiment output', self.source_accounts)
        self.account = self.source_accounts / 'omega01'
        self.account.mkdir(mode=0o700)
        self.auth = self.account / 'chatgpt.json'
        self.auth.write_text('{"fixture": "not real credentials"}\n', encoding='utf-8')
        self.auth.chmod(0o600)
        self.config_path = self.directory / 'omega.yaml'

    def write_config(self, **settings):
        config = {
            'agent': 'omega_agent_v1', 'environment': 'example', 'games': ['counter'],
            'omega_accounts': ['omega01'], 'model': 'test-model', 'reasoning_effort': 'max',
            'server_inactivity_timeout_seconds': 1800, 'server_terminal_grace_seconds': 10,
        }
        config.update(settings)
        self.config_path.write_text(yaml.safe_dump(config), encoding='utf-8')
        return config

    def config(self, **settings):
        self.write_config(**settings)
        return controller.parse_run_config(self.config_path, paths=self.paths)

    def preparation(self, alias='alias'):
        game = self.paths.run_dir / alias
        return LaunchPreparation(
            command=['python3', 'agent.py', '--master', alias, '--model', 'test-model',
                     '--reasoning-effort', 'max', '--max-new-input-loops', '10000000'],
            mounts=[AgentMount(game / 'run', '/home/user/run'),
                    AgentMount(game / 'client', '/client'),
                    AgentMount(game / 'observations', '/observations'),
                    AgentMount(game / 'omega_memory', '/PeTTa/repos/Omega/memory')],
            environment={'TINI_KILL_PROCESS_GROUP': '1'},
        )


class OmegaAccountContractTest(OmegaFixture):
    def test_validation_only_reads_metadata_and_preserves_original_account(self):
        before = self.auth.read_bytes()
        with patch.object(Path, 'read_text', side_effect=AssertionError('credential content read')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('credential content read')), \
             patch.object(Path, 'open', side_effect=AssertionError('credential opened')):
            result = omega.validate_account('omega01', paths=self.paths)
        self.assertEqual(result, self.account.resolve())
        self.assertEqual(self.auth.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.account.iterdir()), ['chatgpt.json'])
        self.assertFalse(self.paths.root.exists())

    def test_mount_uses_original_canonical_directory_and_allows_atomic_refresh(self):
        alias = self.directory / 'account directory alias'
        alias.symlink_to(self.source_accounts, target_is_directory=True)
        paths = omega.RunPaths(self.paths.root, alias)
        mount = omega.account_mount_args('omega01', paths=paths)
        self.assertEqual(mount, ['--mount',
            f'type=bind,source={self.account.resolve()},target=/home/user/.omega_auth'])
        self.assertNotIn('readonly', mount[1])
        self.assertNotIn('chatgpt.json', mount[1])
        self.assertFalse(paths.run_dir.exists())

    def test_account_names_cannot_escape_account_directory(self):
        for name in ('../omega01', '/omega01', '.', '..', 'nested/omega01', '', 'omega01\n'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                omega.validate_account(name, paths=self.paths)

    def test_missing_account_and_missing_auth_file_are_rejected(self):
        with self.assertRaises(RuntimeError):
            omega.validate_account('missing', paths=self.paths)
        self.auth.unlink()
        with self.assertRaisesRegex(RuntimeError, 'chatgpt.json'):
            omega.validate_account('omega01', paths=self.paths)

    def test_auth_file_symlink_and_directory_are_rejected(self):
        target = self.directory / 'other-fixture.json'
        target.write_text('{}', encoding='utf-8')
        target.chmod(0o600)
        self.auth.unlink()
        self.auth.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'regular file|symlink'):
            omega.validate_account('omega01', paths=self.paths)
        self.auth.unlink()
        self.auth.mkdir(mode=0o700)
        with self.assertRaisesRegex(RuntimeError, 'regular file'):
            omega.validate_account('omega01', paths=self.paths)

    def test_auth_file_and_account_directory_must_be_private(self):
        for mode in (0o644, 0o660):
            self.auth.chmod(mode)
            with self.subTest(file_mode=oct(mode)), self.assertRaisesRegex(RuntimeError, 'private'):
                omega.validate_account('omega01', paths=self.paths)
        self.auth.chmod(0o600)
        self.account.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            omega.validate_account('omega01', paths=self.paths)

    def test_account_must_be_owned_by_current_user(self):
        wrong_uid = os.getuid() + 1
        with patch.object(omega.os, 'getuid', return_value=wrong_uid), \
             self.assertRaisesRegex(RuntimeError, 'owned by you'):
            omega.validate_account('omega01', paths=self.paths)


class OmegaConfigurationContractTest(OmegaFixture):
    def test_defaults_keep_omega_paths_separate_from_codex(self):
        args = controller.parse_args([])
        paths = omega.RunPaths(args.run_root, args.accounts_root)
        self.assertEqual(paths.root, ROOT)
        self.assertEqual(paths.resolve_config(args.run_config), ROOT / 'run_config_omega.yaml')
        self.assertEqual(paths.accounts_dir, ROOT / 'omega_accounts')
        self.assertEqual(shared.RunPaths().run_config, ROOT / 'run_config.yaml')
        self.assertEqual(shared.RunPaths().accounts_dir, ROOT / 'codex_accounts')

    def test_explicit_relative_paths_use_callers_cwd_independently(self):
        with patch('os.getcwd', return_value=str(self.directory)):
            args = controller.parse_args(['--run-root', 'results', '--run-config', 'chosen.yaml',
                                          '--omega-accounts-dir', 'existing accounts'])
            paths = omega.RunPaths(args.run_root, args.accounts_root)
            self.assertEqual(paths.root, self.directory / 'results')
            self.assertEqual(paths.accounts_dir, self.directory / 'existing accounts')
            self.assertEqual(paths.resolve_config(args.run_config), self.directory / 'chosen.yaml')

    def test_external_run_root_keeps_existing_account_in_place(self):
        before = self.auth.read_bytes()
        config = self.config()
        self.assertEqual(config.accounts, ['omega01'])
        self.assertEqual(config.paths.accounts_dir, self.source_accounts)
        self.assertEqual(config.paths.run_dir, self.paths.root / 'run')
        self.assertEqual(config.server_terminal_grace_seconds, 10)
        self.assertEqual(config.server_inactivity_timeout_seconds, 1800)
        self.assertEqual(config.parsed_run_config['reasoning_effort'], 'max')
        self.assertEqual(self.auth.read_bytes(), before)
        self.assertFalse(self.paths.root.exists())

    def test_duplicate_accounts_and_path_traversal_are_rejected(self):
        for accounts in (['omega01', 'omega01'], ['../omega01']):
            with self.subTest(accounts=accounts), self.assertRaises(RuntimeError):
                self.config(omega_accounts=accounts)

    def test_account_aliases_cannot_schedule_the_same_credentials_twice(self):
        (self.source_accounts / 'same-account').symlink_to(self.account, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'distinct account directories'):
            self.config(omega_accounts=['omega01', 'same-account'])

    def test_codex_agent_and_codex_only_account_config_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'omega_agent_v1'):
            self.config(agent='agent_v1')
        self.write_config()
        raw = yaml.safe_load(self.config_path.read_text())
        del raw['omega_accounts']
        raw['codex_accounts'] = ['omega01']
        self.config_path.write_text(yaml.safe_dump(raw), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'omega_accounts'):
            controller.parse_run_config(self.config_path, paths=self.paths)

    def test_model_and_reasoning_must_be_nonempty_strings(self):
        for field in ('model', 'reasoning_effort'):
            for value in (None, '', '   ', True, 12):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(RuntimeError, field):
                    self.config(**{field: value})

    def test_terminal_grace_rejects_invalid_values(self):
        for value in (0, -1, True, '10', float('inf')):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'server_terminal_grace_seconds'):
                self.config(server_terminal_grace_seconds=value)


class OmegaMemoryContractTest(unittest.TestCase):
    def test_default_native_runtime_uses_larger_container_and_process_limits(self):
        runner = omega.parse_runner_config({}, Path('fixture.yaml'))
        self.assertEqual(runner, omega.RunnerConfig())
        self.assertEqual(runner.container_memory_bytes, 16 * 1024**3)
        self.assertEqual(runner.process_data_soft_limit_bytes, 8 * 1024**3)
        self.assertEqual(runner.process_data_hard_limit_bytes, 8 * 1024**3)
        self.assertEqual(omega.agent_memory_args(runner),
                         ['--memory', str(16 * 1024**3), '--memory-swap', str(16 * 1024**3),
                          '--ulimit', f'data={8 * 1024**3}:{8 * 1024**3}'])
        codex = shared.parse_runner_config({}, Path('fixture.yaml'))
        self.assertEqual(codex.process_data_soft_limit_bytes, 2 * 1024**3)
        self.assertEqual(codex.process_data_hard_limit_bytes, 2 * 1024**3)
        self.assertIn('--ulimit', shared.agent_memory_args(codex))

    def test_container_only_override_preserves_default_process_limits(self):
        runner = omega.parse_runner_config({'runner': {'container_memory_gib': 12}}, Path('fixture.yaml'))
        self.assertEqual(runner.container_memory_bytes, 12 * 1024**3)
        self.assertIn(f'data={8 * 1024**3}:{8 * 1024**3}', omega.agent_memory_args(runner))

    def test_partial_process_overrides_use_omega_defaults_without_mutating_config(self):
        cases = [('process_data_soft_limit_gib', 6, 6, 8),
                 ('process_data_hard_limit_gib', 12, 8, 12)]
        for key, value, soft, hard in cases:
            with self.subTest(key=key):
                config = {'runner': {key: value}}
                runner = omega.parse_runner_config(config, Path('fixture.yaml'))
                self.assertEqual(runner.container_memory_bytes, 16 * 1024**3)
                self.assertEqual(runner.process_data_soft_limit_bytes, soft * 1024**3)
                self.assertEqual(runner.process_data_hard_limit_bytes, hard * 1024**3)
                self.assertEqual(config, {'runner': {key: value}})

    def test_explicit_process_limits_are_applied(self):
        runner = omega.parse_runner_config({'runner': {
            'container_memory_gib': 12, 'process_data_soft_limit_gib': 4,
            'process_data_hard_limit_gib': 6,
        }}, Path('fixture.yaml'))
        arguments = omega.agent_memory_args(runner)
        self.assertIn(f'data={4 * 1024**3}:{6 * 1024**3}', arguments)
        self.assertEqual(arguments[arguments.index('--memory') + 1], str(12 * 1024**3))

    def test_invalid_runner_settings_are_rejected(self):
        for settings in ({'container_memory_gib': 0}, {'container_memory_gib': True},
                         {'process_data_soft_limit_gib': 7, 'process_data_hard_limit_gib': 6},
                         {'process_data_hard_limit_gib': 6}, {'unknown_option': 1}, None, []):
            with self.subTest(settings=settings), self.assertRaises(RuntimeError):
                omega.parse_runner_config({'runner': settings}, Path('fixture.yaml'))


class OmegaLaunchContractTest(OmegaFixture):
    def test_agent_command_mounts_original_account_and_uses_native_runtime(self):
        preparation = self.preparation()
        command = controller.docker_agent_command('omega01', 'agent', 'internal', 'server', 'proxy',
            preparation, agent_image='selected-omega:run-fixture', paths=self.paths)
        self.assertIn('--init', command)
        self.assertIn('--pull=never', command)
        self.assertEqual(command[command.index('--network') + 1], 'internal')
        self.assertIn(omega.account_mount_args('omega01', paths=self.paths)[1], command)
        self.assertNotIn('/home/user/.codex', repr(command))
        self.assertNotIn('agent_runner.py', command)
        self.assertEqual(command[command.index('--memory') + 1], str(16 * 1024**3))
        self.assertEqual(command[command.index('--ulimit') + 1], f'data={8 * 1024**3}:{8 * 1024**3}')
        self.assertEqual(command[-len(preparation.command):], preparation.command)
        self.assertIn('selected-omega:run-fixture', command)
        environment = [command[i + 1] for i, value in enumerate(command) if value == '-e']
        self.assertIn('TINI_KILL_PROCESS_GROUP=1', environment)
        self.assertTrue(any(item.startswith('HTTPS_PROXY=') for item in environment))
        self.assertTrue(any(item.startswith('NO_PROXY=') and 'server' in item for item in environment))
        self.assertFalse(self.paths.run_dir.exists())

    def test_agent_console_is_persisted_in_mounted_run_directory(self):
        preparation = self.preparation()
        destination = self.paths.run_dir / 'alias/run'
        destination.mkdir(parents=True)
        process = Mock()

        def launch(command, **kwargs):
            self.assertEqual(kwargs['stderr'], subprocess.STDOUT)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            handle = kwargs['stdout']
            self.assertEqual(Path(handle.name), destination / 'omega_console.log')
            marker = b'fixture Omega console output\n'
            handle.write(marker if 'b' in handle.mode else marker.decode())
            handle.flush()
            return process

        with patch.object(controller.subprocess, 'Popen', side_effect=launch), \
             patch.object(shared, 'log_info'):
            result = controller.start_agent('omega01', 'alias', 'counter', 'agent', 'internal',
                'server', 'proxy', preparation, agent_image='selected-omega:run-fixture', paths=self.paths)
        self.assertIs(result.process, process)
        self.assertEqual((destination / 'omega_console.log').read_text(), 'fixture Omega console output\n')
        self.assertEqual(list(self.account.iterdir()), [self.auth])

    def test_account_preflight_uses_selected_run_image_and_original_mount(self):
        config = self.config()
        self.paths.run_dir.mkdir(parents=True)
        process = Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(controller.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(controller.subprocess, 'run', return_value=Mock(returncode=0)), \
             patch.object(shared, 'log_info'):
            controller.verify_omega_accounts(config, agent_image='selected-omega:run-fixture')
        command = launch.call_args.args[0]
        self.assertIn('selected-omega:run-fixture', command)
        self.assertIn(omega.account_mount_args('omega01', paths=self.paths)[1], command)
        self.assertIn('test-model', command)
        self.assertEqual(command[command.index('--memory') + 1], str(16 * 1024**3))
        self.assertEqual(command[command.index('--ulimit') + 1], f'data={8 * 1024**3}:{8 * 1024**3}')
        self.assertNotIn('omega-accounts-image', repr(command))
        self.assertTrue(Path(launch.call_args.kwargs['stdout'].name).is_relative_to(self.paths.run_dir))
        self.assertEqual(launch.call_args.kwargs['stderr'], subprocess.STDOUT)
        process.wait.assert_called_once()

    def test_failed_preflight_reports_log_and_removes_only_its_own_container(self):
        config = self.config()
        process = Mock()
        process.wait.return_value = 7
        process.poll.return_value = 7
        with patch.object(controller.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(controller.subprocess, 'run', return_value=Mock(returncode=0)) as cleanup, \
             patch.object(shared, 'log_info'), \
             self.assertRaisesRegex(RuntimeError, 'omega01.*exit code 7.*account_checks'):
            controller.verify_omega_accounts(config, agent_image='selected-omega:run-fixture')
        command = launch.call_args.args[0]
        container = command[command.index('--name') + 1]
        self.assertTrue(container.startswith('omega-account-check-'))
        self.assertEqual(cleanup.call_args.args[0], ['docker', 'rm', '-f', container])
        self.assertEqual(list(self.account.iterdir()), [self.auth])

    def test_timed_out_preflight_stops_and_removes_its_container(self):
        config = self.config()
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired('mocked Docker check', 150)
        process.poll.return_value = None
        with patch.object(controller.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(controller.subprocess, 'run', return_value=Mock(returncode=0)) as cleanup, \
             patch.object(shared, 'stop_container') as stop, \
             patch.object(shared, 'log_info'), \
             self.assertRaisesRegex(RuntimeError, 'timed out.*omega01'):
            controller.verify_omega_accounts(config, agent_image='selected-omega:run-fixture')
        command = launch.call_args.args[0]
        container = command[command.index('--name') + 1]
        self.assertEqual(stop.call_args.args[:2], (container, process))
        self.assertEqual(cleanup.call_args.args[0], ['docker', 'rm', '-f', container])

    def test_main_combines_external_outputs_and_original_accounts_without_copying(self):
        self.write_config()
        images = shared.RunImages.create()
        logger = logging.Logger('omega-controller-test')
        before = self.auth.read_bytes()
        try:
            with patch.object(shared, 'LOGGER', logger), \
                 patch.object(shared, 'verify_container_names_available'), \
                 patch.object(shared, 'verify_network_names_available'), \
                 patch.object(shared, 'built_run_images') as build, \
                 patch.object(controller, 'verify_omega_accounts') as preflight, \
                 patch.object(controller, 'run_scheduler', return_value=0), \
                 patch.object(shared, 'log_info'):
                build.return_value.__enter__.return_value = images
                code = controller.main(['--run-root', str(self.paths.root),
                                        '--run-config', str(self.config_path),
                                        '--omega-accounts-dir', str(self.source_accounts)])
            self.assertEqual(code, 0)
            passed_config = preflight.call_args.args[0]
            self.assertEqual(passed_config.paths, self.paths)
            self.assertEqual(preflight.call_args.kwargs['agent_image'], images.agent)
            self.assertEqual(build.call_args.args[:2], ('example', 'omega_agent_v1'))
            self.assertTrue(self.paths.runner_log.is_file())
            self.assertIn(str(self.source_accounts), self.paths.runner_log.read_text())
            self.assertEqual(self.auth.read_bytes(), before)
            self.assertFalse((self.paths.root / 'omega_accounts').exists())
            self.assertFalse(any(self.paths.run_dir.rglob('chatgpt.json')))
        finally:
            for handler in logger.handlers:
                handler.close()

    def test_scheduler_preserves_account_slots_and_terminal_grace(self):
        config = self.config()
        names = shared.build_container_names(config.games)
        images = shared.RunImages.create()
        preparation = self.preparation()
        with patch.object(shared, 'run_scheduler', return_value=0) as schedule, \
             patch.object(shared, 'prepare_agent_run', return_value=preparation) as prepare, \
             patch.object(controller, 'start_agent', return_value=Mock()) as launch:
            self.assertEqual(controller.run_scheduler(config, names, {'alias': 'counter'}, images=images), 0)
            options = schedule.call_args.kwargs
            self.assertEqual(options['slots'], ['omega01'])
            self.assertEqual(options['server_terminal_grace_seconds'], 10)
            self.assertEqual(options['server_inactivity_timeout_seconds'], 1800)
            self.assertEqual(options['paths'], self.paths)
            options['launch_agent']('omega01', 'alias', 'counter')
        self.assertEqual(prepare.call_args.args[0], 'omega_agent_v1')
        self.assertEqual(prepare.call_args.args[1], self.paths.run_dir / 'alias')
        self.assertEqual(launch.call_args.args[0], 'omega01')
        self.assertEqual(launch.call_args.kwargs['agent_image'], images.agent)
        self.assertEqual(launch.call_args.kwargs['paths'].accounts_dir, self.source_accounts)


if __name__ == '__main__':
    unittest.main()
