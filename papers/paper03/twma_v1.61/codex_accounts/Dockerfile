FROM ubuntu:24.04

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

# Account tooling is independent of the Codex version used by experiments.
RUN npm i -g @openai/codex@0.153.4

ARG USER_ID=1000
ARG GROUP_ID=1000

RUN if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu || true; fi \
    && if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu || true; fi \
    && if getent group ${GROUP_ID} >/dev/null 2>&1; then \
         existing_group="$(getent group ${GROUP_ID} | cut -d: -f1)"; \
       else \
         groupadd -g ${GROUP_ID} user; \
         existing_group="user"; \
       fi \
    && useradd -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash user \
    && mkdir -p /home/user/.codex /home/user/run \
    && chown -R ${USER_ID}:${GROUP_ID} /home/user/.codex /home/user/run

USER user
WORKDIR /home/user/run
ENV CODEX_HOME=/home/user/.codex

CMD ["/bin/bash"]
