FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends tmux bash coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# No solution/ or tests/ files are copied into the solving environment.
