"""Run unchanged Omega and the game client against offline HTTP/model doubles.

Build the agent image first, then run this file with Python. Override the default
omega-agent-v1:<uid>-<gid> tag with OMEGA_SMOKE_IMAGE if needed. Uses no account
credentials or real inference. Temporary artifacts are retained for inspection.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

PROJECT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(PROJECT), str(PROJECT / 'src')]
from agents.omega_agent_v1.prepare_run import prepare_run


MODEL_STUB = r'''import json
from pathlib import Path
import sys
import shlex

root = Path('/PeTTa/repos/Omega')
for location in (root, root / 'providers', root / 'src'):
    sys.path.insert(0, str(location))

import chatgpt_auth
import httpx
import openai

counter = 0
original_client = openai.OpenAI

def transport(request):
    global counter
    counter += 1
    body = json.loads(request.content)
    with Path('/home/user/run/smoke-requests.jsonl').open('a') as output:
        output.write(json.dumps(body) + '\n')
    if counter == 1:
        name = 'shell'
        native_check = "import sys; assert sys.prefix == sys.base_prefix == '/usr', (sys.prefix, sys.base_prefix); print(sys.prefix)"
        arguments = {'cmd': 'pwd > smoke-cwd.txt; python3 -c ' + shlex.quote(native_check) + ' > smoke-native-python.txt && python3 /client/client.py move ' + shlex.quote(json.dumps({'action': 'smoke'}))}
    elif counter == 2:
        name = 'nop'
        arguments = {}
    else:
        raise AssertionError('Unexpected third inference request')
    response = {
        'id': f'resp_smoke_{counter}', 'object': 'response', 'created_at': 1,
        'status': 'completed', 'model': 'smoke-model',
        'output': [{'type': 'function_call', 'id': f'fc_{counter}',
                    'call_id': f'call_{counter}', 'namespace': 'omega',
                    'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}],
        'error': None, 'incomplete_details': None,
        'usage': {
            'input_tokens': 1000 * counter,
            'input_tokens_details': {'cached_tokens': 256 * counter},
            'output_tokens': 120 * counter,
            'output_tokens_details': {'reasoning_tokens': 80 * counter},
            'total_tokens': 1120 * counter,
        },
    }
    event = {'type': 'response.completed', 'sequence_number': 0, 'response': response}
    return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                          content='event: response.completed\ndata: ' + json.dumps(event) + '\n\n')

def make_client(**kwargs):
    return original_client(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport)))

def install():
    chatgpt_auth.list_models = lambda: [{'slug': 'smoke-model'}]
    chatgpt_auth.get_access_token = lambda: 'offline-smoke-token'
    openai.OpenAI = make_client
'''


def main():
    image = os.environ.get('OMEGA_SMOKE_IMAGE', f'omega-agent-v1:{os.getuid()}-{os.getgid()}')
    subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL, check=True, timeout=30)
    state = {'step': 0, 'action': None, 'events': []}


    def payload():
        result = {
            'environment_description': 'OMEGA_SMOKE_GOAL: send one smoke action, then do nothing.',
            'session': {'token': 'local-smoke-token', 'terminal': False},
            'step': {'step_index': state['step'], 'artifacts': [{
                'name': 'observation.txt',
                'content_base64': base64.b64encode(f"Smoke step {state['step']}".encode()).decode(),
            }]},
        }
        if state['action'] is not None:
            result['action'] = state['action']
        return result


    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data, status=200):
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['events'].append(self.path)
            if self.path == '/game/start':
                assert body['game_id'] == 'smoke'
                self.respond(payload())
            elif self.path == '/game/action':
                state['step'] += 1
                state['action'] = body['action']
                self.respond(payload())
            else:
                self.respond({'error': 'Unexpected route'}, 404)

        def do_GET(self):
            state['events'].append(self.path)
            if self.path == '/game/last-step':
                self.respond(payload())
            else:
                self.respond({'error': 'Unexpected route'}, 404)


    directory = Path(tempfile.mkdtemp(prefix='omega-full-agent-smoke-'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    container = 'omega-agent-smoke-' + str(os.getpid()) + '-' + directory.name.rsplit('-', 1)[-1]
    process = None
    try:
        preparation = prepare_run(
            directory, f'http://127.0.0.1:{server.server_port}', 'smoke',
            {'model': 'smoke-model', 'reasoning_effort': 'medium', 'max_new_input_loops': 2},
        )
        run = directory / 'run'
        (run / 'smoke_model.py').write_text(MODEL_STUB)
        task = run / 'run_task.metta'
        task.write_text(task.read_text().replace('!(omega)',
            '!(import! &self ./smoke_model.py)\n!(py-call (smoke_model.install))\n!(omega)'))
        auth = directory / 'fake-auth'
        auth.mkdir(mode=0o700)
        (auth / 'chatgpt.json').write_text('{}')
        (auth / 'chatgpt.json').chmod(0o600)
        command = ['docker', 'run', '--rm', '--pull', 'never', '--init', '--name', container, '--network', 'host',
                   '--workdir', '/home/user/run', '-e', 'HF_HUB_OFFLINE=1', '-e', 'TRANSFORMERS_OFFLINE=1',
                   '-e', 'ANONYMIZED_TELEMETRY=False',
                   '--mount', f'type=bind,source={auth},target=/home/user/.omega_auth']
        for mount in preparation.mounts:
            command += ['--mount', f'type=bind,source={mount.source},target={mount.target}']
        for key, value in preparation.environment.items():
            command += ['-e', f'{key}={value}']
        command += [image, *preparation.command]
        logpath = directory / 'container.log'
        with logpath.open('w') as logfile:
            process = subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f'Container stopped unexpectedly: {process.returncode}; see {logpath}')
                requests_path = run / 'smoke-requests.jsonl'
                if requests_path.exists() and len(requests_path.read_text().splitlines()) == 2:
                    break
                time.sleep(1)
            else:
                raise AssertionError(f'Omega did not complete two mocked cycles; see {logpath}')
            time.sleep(3)
            requests = [json.loads(line) for line in requests_path.read_text().splitlines()]
            assert len(requests) == 2, len(requests)
            first_users = [item['content'] for item in requests[0]['input'] if item.get('role') == 'user']
            second_users = [item['content'] for item in requests[1]['input'] if item.get('role') == 'user']
            assert len(first_users) == 1 and 'initial output of the game client' in first_users[0]
            assert len(second_users) == 1 and 'NO NEW USER INPUT' in second_users[0]
            client_state = json.loads((directory / 'client/.client_session.json').read_text())
            assert client_state['step_index'] == 1, client_state
            assert (directory / 'observations/step_0001_observation.txt').read_text() == 'Smoke step 1'
            assert (run / 'agent_run/smoke-cwd.txt').read_text().strip() == '/home/user/run/agent_run'
            assert (run / 'agent_run/smoke-native-python.txt').read_text().strip() == '/usr'
            assert state['events'] == ['/game/start', '/game/last-step', '/game/action'], state['events']
            assert (directory / 'omega_memory/history.metta').stat().st_size > 0
            # Verify the mounted file, not only Docker stdout: these usage
            # records must survive the controller stopping/removing Omega.
            usage_lines = [line.split('[LLM_USAGE] ', 1)[1]
                           for line in (run / 'agent.log').read_text().splitlines()
                           if '[LLM_USAGE] ' in line]
            assert len(usage_lines) == 2, usage_lines
            for index, line in enumerate(usage_lines, start=1):
                fields = dict(field.split('=', 1) for field in line.split())
                expected = {
                    'provider': 'ChatGPT', 'model': 'smoke-model', 'status': 'completed',
                    'input_tokens': str(1000 * index), 'cached_tokens': str(256 * index),
                    'output_tokens': str(120 * index), 'reasoning_tokens': str(80 * index),
                }
                assert all(fields.get(key) == value for key, value in expected.items()), fields
            assert process.poll() is None, 'Stock Omega should be idle after its cycle budget'
            print('PASS: full unchanged Omega init, native system Python client action, workspace cwd, one prompt, two model cycles, persisted per-response usage, policy writes, history persistence, and idle behavior')
    finally:
        try:
            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=False)
        finally:
            if process is not None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            server.shutdown()
            server.server_close()
            print(f'Smoke artifacts: {directory}', flush=True)


if __name__ == '__main__':
    main()
