import importlib
import sys
assert sys.prefix == sys.base_prefix == '/usr', (sys.prefix, sys.base_prefix)
modules = '''numpy scipy sympy mpmath networkx mpi4py.MPI jsonschema jsonpointer
jsonpatch pydantic pyrsistent attrs typeguard typing_extensions lark
google.protobuf yaml matplotlib contourpy cycler kiwisolver pyparsing dateutil
PIL pydot fontTools ufoLib2 olefile requests cv2 skimage imageio tifffile pandas
sklearn joblib tqdm rich z3 pycosat pulp shapely rtree pygraphviz statsmodels
numexpr h5py tables'''.split()
for name in modules:
    importlib.import_module(name)
for name in ('openai', 'jwt', 'yaml', 'torch', 'chromadb', 'sentence_transformers'):
    importlib.import_module(name)
import numpy as np
import scipy.linalg
import pandas as pd
import pygraphviz
assert int(np.arange(4).sum()) == 6
assert abs(scipy.linalg.det([[1, 2], [3, 4]]) + 2) < 1e-10
assert pd.DataFrame({'n': [2, 3]}).n.sum() == 5
graph = pygraphviz.AGraph()
graph.add_edge('a', 'b')
graph.layout(prog='dot')
assert graph.get_node('a').attr['pos']
import tempfile
from pathlib import Path
import tables
import h5py
with tempfile.TemporaryDirectory() as directory:
    filename = str(Path(directory) / 'native.h5')
    expected = np.arange(12).reshape(3, 4)
    with tables.open_file(filename, mode='w') as handle:
        handle.create_array('/', 'values', expected)
    with tables.open_file(filename) as handle:
        assert np.array_equal(handle.root.values.read(), expected)
    with h5py.File(filename) as handle:
        assert np.array_equal(handle['values'][:], expected)
print(f'Native interpreter {sys.executable}: {len(modules)} science modules + Omega imports + NumPy/SciPy/pandas/Graphviz/HDF5 computations succeeded', flush=True)
