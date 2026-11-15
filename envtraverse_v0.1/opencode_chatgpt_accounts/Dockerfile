FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=2.3.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@opencode/cli@${OPENCODE_VERSION}" \
    && npm cache clean --force \
    && opencode --version

ARG USER_ID=1000
ARG GROUP_ID=1000
RUN userdel -r node \
    && if ! getent group "${GROUP_ID}" >/dev/null; then groupadd -g "${GROUP_ID}" user; fi \
    && if ! getent passwd "${USER_ID}" >/dev/null; then \
         useradd -m -u "${USER_ID}" -g "${GROUP_ID}" -s /bin/bash user; \
       fi \
    && mkdir -p /home/user/.local/share/opencode /home/user/.local/state \
         /home/user/.config /home/user/.cache /home/user/run \
    && chown -R "${USER_ID}:${GROUP_ID}" /home/user

ENV HOME=/home/user \
    XDG_DATA_HOME=/home/user/.local/share \
    XDG_STATE_HOME=/home/user/.local/state \
    XDG_CONFIG_HOME=/home/user/.config \
    XDG_CACHE_HOME=/home/user/.cache \
    OPENCODE_DB=opencode.db

USER ${USER_ID}:${GROUP_ID}
WORKDIR /home/user/run
CMD ["/bin/bash"]
