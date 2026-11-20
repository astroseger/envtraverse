"""Operator integration test: creates and destroys its own fresh game session.

Run against an already-running minecraft_realtime host runtime:
  python -m minecraft_realtime.tests.live_crafting --run-root /absolute/experiment

This test gives ingredients and places a crafting table through the private
Docker server console, then crafts through ordinary environment input actions.
Fixture commands are deliberately absent from the agent interface. Existing
sessions are never selected or changed. Screenshots/report remain in run_mc.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from ..protocol import RuntimeClient, decode_observation
from ..settings import Settings


def run(run_root: Path) -> Path:
    run_root = run_root.resolve()
    alias, owner = uuid4().hex, uuid4().hex
    workspace = run_root / 'run' / alias / 'run' / 'agent_run'
    workspace.mkdir(parents=True)
    directory = run_root / 'run_mc' / 'sessions' / alias
    client = RuntimeClient(run_root / 'run_mc/socket/runtime.sock', timeout=620)
    identity = {'session_id': alias, 'owner_token': owner}
    start_sent = False
    try:
        start_sent = True
        client.call('start', **identity, game_id='craft_wooden_pickaxe',
                    settings=Settings(minecraft_task_timeout_seconds=600).to_dict())
        output = directory / 'craft-validation'
        output.mkdir()

        def console(command):
            subprocess.run(['docker', 'exec', 'minecraft-realtime-' + alias + '-server-1',
                            'python3', '-c',
                            "import sys; open('/proc/1/fd/0','w').write(sys.argv[1]+'\\n')", command],
                           check=True, capture_output=True, timeout=10)

        def act(action, expect_terminal=False):
            response = client.call('action', **identity, action=action)
            files, terminal = decode_observation(response['observation'])
            confirmation = json.loads(files['action.json'])
            assert terminal == expect_terminal, confirmation
            assert confirmation['accepted'] == (not expect_terminal), confirmation
            time.sleep(.15)
            return confirmation

        def click(x, y, button='left'):
            return act({'id': 'mouse_click', 'button': button, 'x': x, 'y': y})

        def snapshot(name):
            (output / name).write_bytes((workspace / 'latest_frame.jpg').read_bytes())

        for command in [
            'gamerule doMobSpawning false', 'time set day', 'weather clear',
            'fill -4 98 -4 4 98 4 minecraft:stone',
            'fill -4 99 -4 4 103 4 minecraft:air',
            'tp AgentPlayer 0.5 99 0.5 0 30',
            'setblock 0 99 2 minecraft:crafting_table',
            'clear AgentPlayer', 'give AgentPlayer minecraft:oak_planks 6',
            'give AgentPlayer minecraft:stick 4',
        ]:
            console(command)
        time.sleep(1)
        act({'id': 'release_all'})
        act({'id': 'mouse_click', 'button': 'right'})
        time.sleep(.5)
        snapshot('crafting-open.jpg')
        # Pinned 960x540 and vanilla auto GUI scale=2, recipe book initially closed.
        click(336, 404)
        for x in (380, 416, 452):
            click(x, 154, 'right')
            click(x, 154, 'right')
        click(372, 404)
        for y in (190, 226):
            click(416, y, 'right')
            click(416, y, 'right')
        snapshot('crafting-recipe.jpg')
        before = json.loads((directory / 'telemetry/state.json').read_text())
        assert before['counters']['crafted'].get('minecraft:wooden_pickaxe', 0) == 0
        act({'id': 'key_down', 'key': 'shift'})
        click(568, 190)
        # Events can complete the goal before the observer's periodic state file.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            after = json.loads((directory / 'telemetry/state.json').read_text())
            if ((directory / 'result.json').exists()
                    and after['counters']['crafted'].get('minecraft:wooden_pickaxe', 0) == 2):
                break
            time.sleep(.1)
        snapshot('crafting-complete.jpg')
        assert after['counters']['crafted'].get('minecraft:wooden_pickaxe', 0) == 2, after['counters']
        result = json.loads((directory / 'result.json').read_text())
        assert result['status'] == 'succeeded', result
        confirmation = act({'id': 'key_up', 'key': 'shift'}, expect_terminal=True)
        events = [json.loads(line) for line in (directory / 'telemetry/events.jsonl').read_text().splitlines()]
        crafts = [event for event in events if event['type'] == 'craft']
        assert sum(event['data']['count'] for event in crafts
                   if event['data']['item'] == 'minecraft:wooden_pickaxe') == 2
        report = {'result': result, 'terminal_confirmation': confirmation,
                  'counters': after['counters'], 'craft_events': crafts}
        report_path = output / 'report.json'
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        return report_path
    finally:
        if start_sent:
            RuntimeClient(client.socket_path, timeout=10).call('close', **identity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    args = parser.parse_args()
    print(run(args.run_root))


if __name__ == '__main__':
    main()
