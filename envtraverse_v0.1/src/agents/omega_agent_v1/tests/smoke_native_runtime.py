"""Verify the shared scientific/Omega Python stack through Python, Janus, and MeTTa.

Build the agent image first, then run this file with Python. Override the default
omega-agent-v1:<uid>-<gid> tag with OMEGA_SMOKE_IMAGE if needed. Containers have no
network or account mounts. Temporary logs and probe files remain for inspection.
"""
import os
from pathlib import Path
import subprocess
import tempfile


PROBE = '''import importlib
import json
from pathlib import Path
import sys
import tempfile


def check():
    assert sys.prefix == sys.base_prefix == '/usr', (sys.prefix, sys.base_prefix)
    assert not Path('/opt/agent-venv').exists()
    modules = "numpy scipy sympy networkx mpi4py.MPI pandas sklearn cv2 skimage z3 pulp shapely rtree pygraphviz statsmodels numexpr h5py tables blosc2 openai jwt yaml torch chromadb sentence_transformers".split()
    for name in modules:
        importlib.import_module(name)
    import numpy as np
    from scipy.linalg import solve
    import sympy
    import z3
    import pygraphviz
    import blosc2
    import h5py
    import tables
    with tempfile.TemporaryDirectory(prefix='omega-native-hdf5-') as directory:
        path = str(Path(directory) / 'roundtrip.h5')
        values = np.arange(12, dtype=np.int64).reshape(3, 4)
        with tables.open_file(path, mode='w') as handle:
            handle.create_array('/', 'values', values)
        with h5py.File(path, 'r') as handle:
            assert np.array_equal(handle['values'][:], values)
    payload = b'Native Omega shared Python compression smoke' * 32
    assert blosc2.decompress(blosc2.compress(payload, typesize=1)) == payload
    graph = pygraphviz.AGraph()
    graph.add_edge('a', 'b')
    graph.layout(prog='dot')
    assert graph.get_node('a').attr['pos']
    assert np.allclose(solve([[3., 1.], [1., 2.]], [9., 8.]), [2., 3.])
    x = sympy.Symbol('x')
    assert sympy.factor(x**2 - 1) == (x-1)*(x+1)
    x = z3.Int('x')
    solver = z3.Solver()
    solver.add(x + 2 == 5)
    assert solver.check() == z3.sat and solver.model()[x].as_long() == 3
    print(json.dumps({'native_runtime': 'ok', 'python': sys.version.split()[0], 'prefix': sys.prefix, 'module_count': len(modules)}, sort_keys=True), flush=True)
    return 'NATIVE_RUNTIME_OK'


if __name__ == '__main__':
    check()
'''


def run_probe(image, workspace, name, arguments, expected=()):
    container = f'omega-native-smoke-{os.getpid()}-{name}'
    command = [
        'docker', 'run', '--rm', '--pull', 'never', '--init', '--name', container,
        '--network', 'none', '--workdir', '/workspace',
        '--mount', f'type=bind,source={workspace},target=/workspace',
        '-e', 'HF_HUB_OFFLINE=1', '-e', 'TRANSFORMERS_OFFLINE=1',
        '-e', 'ANONYMIZED_TELEMETRY=False', image, *arguments,
    ]
    log = workspace / (name + '.log')
    try:
        with log.open('w') as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=90)
        assert result.returncode == 0, (name, result.returncode, log)
        output = log.read_text()
        for marker in expected:
            assert marker in output, (name, marker, log)
        print(f'PASS: {name}; log={log}', flush=True)
    finally:
        # Killing a timed-out Docker CLI alone does not stop its container.
        subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)


def main():
    image = os.environ.get('OMEGA_SMOKE_IMAGE', f'omega-agent-v1:{os.getuid()}-{os.getgid()}')
    subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                   check=True, timeout=30)
    workspace = Path(tempfile.mkdtemp(prefix='omega-native-runtime-smoke-'))
    (workspace / 'native_probe.py').write_text(PROBE)
    (workspace / 'native_probe.metta').write_text(
        '!(import! &self ./native_probe.py)\n'
        '!(py-call (native_probe.check))\n'
        '!(+ 1 1)\n'
    )
    try:
        run_probe(image, workspace, 'python3-shared-packages',
                  ['python3', 'native_probe.py'], ['"native_runtime": "ok"'])
        run_probe(image, workspace, 'python-alias', ['python', '-c',
                  "import sys; assert sys.prefix == sys.base_prefix == '/usr'; print(sys.version)"])
        run_probe(image, workspace, 'prolog-to-python', ['swipl', '-q', '-g',
                  "use_module(library(janus)),py_add_lib_dir('/workspace'),"
                  "py_call(native_probe:check(),R),R=='NATIVE_RUNTIME_OK',halt"],
                  ['"native_runtime": "ok"'])
        run_probe(image, workspace, 'metta-to-python', ['metta', 'native_probe.metta'],
                  ['"native_runtime": "ok"', 'NATIVE_RUNTIME_OK'])
    finally:
        print(f'Native runtime smoke artifacts: {workspace}', flush=True)


if __name__ == '__main__':
    main()
