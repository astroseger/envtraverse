# syntax=docker/dockerfile:1.7
# Native Ubuntu/Python installation, matching agent_v1 scientific packages.
FROM ubuntu:24.04 AS native-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    git \
    bash \
    sudo \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-numpy \
    python3-scipy \
    python3-sympy \
    python3-mpmath \
    python3-networkx \
    python3-mpi4py \
    python3-jsonschema \
    python3-json-pointer \
    python3-jsonpatch \
    python3-pydantic \
    python3-pyrsistent \
    python3-attr \
    python3-typeguard \
    python3-typing-extensions \
    python3-lark \
    python3-protobuf \
    python3-yaml \
    python3-matplotlib \
    python-matplotlib-data \
    python3-contourpy \
    python3-cycler \
    python3-kiwisolver \
    python3-pyparsing \
    python3-dateutil \
    python3-pil \
    python3-pydot \
    python3-fonttools \
    python3-ufolib2 \
    python3-olefile \
    python3-requests \
    python3-opencv \
    python3-skimage \
    python3-imageio \
    python3-tifffile \
    python3-pandas \
    python3-sklearn \
    python3-joblib \
    python3-tqdm \
    python3-rich \
    python3-z3 \
    python3-pycosat \
    python3-pulp \
    python3-shapely \
    python3-rtree \
    python3-pygraphviz \
    graphviz \
    python3-statsmodels \
    python3-numexpr \
    python3-h5py \
    python3-tables \
    && rm -rf /var/lib/apt/lists/*


ENV HF_HOME=/opt/huggingface SENTENCE_TRANSFORMERS_HOME=/opt/sentence_transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build pkg-config python3-dev python3-pip python-is-python3 \
    libarchive-dev libgmp-dev libossp-uuid-dev libreadline-dev libssl-dev libpcre2-dev zlib1g-dev \
    libopenblas-dev libblas-dev liblapack-dev gfortran libgflags-dev libgraphviz-dev \
    libopenmpi-dev openmpi-bin coinor-cbc poppler-utils util-linux \
    && rm -rf /var/lib/apt/lists/*

FROM native-base AS swipl
ARG SWIPL_VERSION=10.0.2
RUN curl -fsSL "https://www.swi-prolog.org/download/stable/src/swipl-${SWIPL_VERSION}.tar.gz" -o /tmp/swipl.tar.gz \
 && echo 'e42cc098f7b8a6051c4f79a99b55162d467098aba60f69649bdc7583f0734b57  /tmp/swipl.tar.gz' | sha256sum -c - \
 && mkdir /tmp/swipl-src && tar -xzf /tmp/swipl.tar.gz --strip-components=1 -C /tmp/swipl-src \
 && cmake -S /tmp/swipl-src -B /tmp/swipl-build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
      -DSWIPL_PACKAGES_X=OFF -DSWIPL_PACKAGES_JAVA=OFF \
      -DPython_EXECUTABLE=/usr/bin/python3 '-DCPYTHON_VERSION=3.12;EXACT' \
 && cmake --build /tmp/swipl-build --parallel 4 \
 && cmake --install /tmp/swipl-build \
 && swipl -q -g "use_module(library(janus)),py_call(sys:version,V),writeln(V),py_call(numpy:sum([1,2,3]),N),py_call(builtins:int(N),6),halt" \
 && rm -rf /tmp/swipl-src /tmp/swipl-build /tmp/swipl.tar.gz


FROM swipl AS runtime
ARG PETTA_REF=v1.0.4
ARG FAISS_REF=v1.8.0
ARG CHROMADB_REF=master

RUN git clone --depth 1 --branch "${PETTA_REF}" https://github.com/trueagi-io/PeTTa.git /PeTTa \
 && git clone --depth 1 --branch "${FAISS_REF}" https://github.com/facebookresearch/faiss.git /tmp/faiss \
 && cmake -S /tmp/faiss -B /tmp/faiss/build -DFAISS_ENABLE_GPU=OFF -DFAISS_ENABLE_PYTHON=OFF -DBUILD_SHARED_LIBS=OFF \
 && cmake --build /tmp/faiss/build --config Release --parallel 4 \
 && cmake --install /tmp/faiss/build \
 && rm -rf /tmp/faiss

# MORK is optional in PeTTa and absent from Omega's existing working image.
# Build and check the FAISS bridge explicitly; upstream build.sh masks failures.
RUN git clone --depth 1 https://github.com/patham9/faiss_ffi.git /PeTTa/faiss_ffi \
 && cd /PeTTa/faiss_ffi && sh build.sh && test -s faisslib.so \
 && git clone --depth 1 --branch "${CHROMADB_REF}" https://github.com/patham9/petta_lib_chromadb.git /PeTTa/repos/petta_lib_chromadb

ARG OMEGA_REVISION=be9d2dd67c1ab878395ac6f44bf845a230c640ff
LABEL org.opencontainers.image.source="https://github.com/astroseger/Omega" \
      org.opencontainers.image.revision="${OMEGA_REVISION}"

RUN git init /PeTTa/repos/Omega \
 && git -C /PeTTa/repos/Omega remote add origin https://github.com/astroseger/Omega.git \
 && git -C /PeTTa/repos/Omega fetch --depth 1 origin "${OMEGA_REVISION}" \
 && git -C /PeTTa/repos/Omega checkout --detach FETCH_HEAD \
 && cd /PeTTa/repos/Omega \
 && python3 -c 'from src.helper import omega_version; print(omega_version())' > /tmp/omega-version \
 && mv /tmp/omega-version version && chmod 0444 version \
 && rm -rf .git

# NumPy 1.26 preserves the ABI of Ubuntu's scientific extension modules.
# Pip may update other dependencies required by Omega; all use system Python.
COPY agents/omega_agent_v1/constraints.txt /opt/omega-python-constraints.txt
# Resolve each phase against the installed packages, then overlay only planned
# upgrades globally. Apt packages can lack pip's uninstall metadata; keep their
# files in place and install the selected wheels in /usr/local instead.
RUN --mount=type=cache,target=/root/.cache/pip python3 - <<'PYTHON'
import json
import subprocess
import sys
from pathlib import Path
pip = [sys.executable, '-m', 'pip']

def install_overlay(*requirements):
    subprocess.run([*pip, 'install', '--break-system-packages', '--dry-run',
                    '--report', '/tmp/omega-install.json',
                    '-c', '/opt/omega-python-constraints.txt',
                    *requirements], check=True)
    plan = json.loads(Path('/tmp/omega-install.json').read_text())
    urls = [item['download_info']['url'] for item in plan['install']]
    if urls:
        subprocess.run([*pip, 'install', '--break-system-packages',
                        '--ignore-installed', '--no-deps', *urls], check=True)

install_overlay('--index-url', 'https://download.pytorch.org/whl/cpu',
                '--extra-index-url', 'https://pypi.org/simple/', 'torch==2.12.1')
# Older Ubuntu PyTables invokes a Python subprocess at import time, which
# fails inside Janus. Newer PyTables also installs the required blosc2 package.
install_overlay('-r', '/PeTTa/repos/Omega/requirements.txt', 'tables>=3.11,<4')
subprocess.run([*pip, 'check'], check=True)
with Path('/opt/agent-requirements-resolved.txt').open('w') as output:
    subprocess.run([*pip, 'freeze'], stdout=output, check=True)
PYTHON


ARG EMBEDDING_MODEL=intfloat/e5-large-v2
# Keep model downloads cached across dependency/source rebuilds, then copy the
# cache into the image so normal runtime needs no external model download.
RUN --mount=type=cache,id=omega-agent-embedding-model,target=/tmp/omega-model-cache \
    mkdir -p "${HF_HOME}" "${SENTENCE_TRANSFORMERS_HOME}" \
 && python3 -c 'import sys; from sentence_transformers import SentenceTransformer; SentenceTransformer(sys.argv[1], cache_folder="/tmp/omega-model-cache")' "${EMBEDDING_MODEL}" \
 && cp -a /tmp/omega-model-cache/. "${SENTENCE_TRANSFORMERS_HOME}/"

ARG USER_ID=1000
ARG GROUP_ID=1000
RUN if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
 && if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu; fi \
 && if ! getent group "${GROUP_ID}" >/dev/null; then groupadd -g "${GROUP_ID}" user; fi \
 && useradd -m -u "${USER_ID}" -g "${GROUP_ID}" -s /bin/bash user \
 && printf 'user ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/omega-agent \
 && chmod 0440 /etc/sudoers.d/omega-agent \
 && mkdir -p /home/user/run /home/user/.omega_auth /opt/omega-memory-template \
 && chmod 0700 /home/user/.omega_auth \
 && cp /PeTTa/repos/Omega/memory/*.txt /opt/omega-memory-template/ \
 && chmod -R a+rX /opt/omega-memory-template \
 && cp /PeTTa/repos/Omega/run.metta /PeTTa/run.metta \
 && mkdir -p /PeTTa/repos/Omega/memory/chroma_db \
 && ln -s /PeTTa/repos/Omega/memory/chroma_db /PeTTa/chroma_db \
 && chown -R "${USER_ID}:${GROUP_ID}" /home/user /PeTTa/repos/Omega/memory /opt/huggingface /opt/sentence_transformers \
 && printf '#!/bin/sh\nexec /bin/sh /PeTTa/run.sh "$@"\n' > /usr/local/bin/metta \
 && chmod 0755 /usr/local/bin/metta

ENV OMEGA_PYTHON=/usr/bin/python3 AGENT_PYTHON=/usr/bin/python3 \
    OMEGA_DIR=/PeTTa/repos/Omega MEMORY_DIR=/PeTTa/repos/Omega/memory \
    OMEGA_CHATGPT_AUTH_FILE=/home/user/.omega_auth/chatgpt.json IMPORT_KB_ON_START=0 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY agents/omega_agent_v1/tests/verify_native.py /opt/omega-agent/verify_native.py
USER ${USER_ID}:${GROUP_ID}
WORKDIR /home/user/run
# Exercise the same libraries through Python and SWI-Prolog's embedded Python.
RUN python3 /opt/omega-agent/verify_native.py \
 && swipl -q -g "use_module(library(janus)),py_call(runpy:run_path('/opt/omega-agent/verify_native.py'),_,[py_object(true)]),halt"

ENTRYPOINT []
CMD ["/bin/bash"]
