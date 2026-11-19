FROM eclipse-temurin:21-jre-jammy@sha256:dff6eb46527bd293a3c06bbbe9e64e8c9f91f01905b5abbdad31761c03059d2c

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    WIDTH=960 HEIGHT=540 CAPTURE_FPS=10 \
    MINECRAFT_VERSION=1.21.1 \
    PLAYER_NAME=AgentPlayer AUTH_MODE=offline \
    SERVER_HOST=server SERVER_PORT=25565

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates xvfb xauth x11-utils xdotool \
    openbox libgl1-mesa-dri libglx-mesa0 libglu1-mesa libasound2 \
    libxrandr2 libxinerama1 libxcursor1 libxi6 libxtst6 libgtk-3-0 \
    fonts-dejavu-core procps \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && useradd --create-home --uid 1000 minecraft \
    && mkdir -p /app /data/client /run/client \
    && chown -R minecraft:minecraft /data/client /run/client

WORKDIR /app
COPY bridge/requirements.txt /app/bridge/requirements.txt
COPY client/requirements.lock.txt /opt/lifemc/requirements.lock.txt
RUN pip install --no-cache-dir -c /opt/lifemc/requirements.lock.txt portablemc==5.0.6 -r /app/bridge/requirements.txt

# This layer depends on the pinned installer, not on capture/runtime source.
# Docker populates each new client data volume from this pristine snapshot.
COPY client/install_assets.py /opt/lifemc/install_assets.py
USER minecraft
RUN python /opt/lifemc/install_assets.py

COPY client/ /app/client/
COPY bridge/ /app/bridge/

LABEL io.envtraverse.life_minecraft.prepared="1"
EXPOSE 8000
ENTRYPOINT ["python", "-m", "client.runtime"]
