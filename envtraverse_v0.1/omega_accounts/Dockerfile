# syntax=docker/dockerfile:1.7
# build_docker.sh builds Omega's own Dockerfile from a clean branch checkout.
ARG OMEGA_BASE_IMAGE
FROM ${OMEGA_BASE_IMAGE}

USER root
ARG USER_ID=1000
ARG GROUP_ID=1000
ARG OMEGA_REVISION

LABEL org.opencontainers.image.source="https://github.com/astroseger/Omega" \
      org.opencontainers.image.revision="${OMEGA_REVISION}" \
      io.omega.accounts.uid="${USER_ID}" \
      io.omega.accounts.gid="${GROUP_ID}"

# Match the host UID: the OAuth helper requires private files owned by its user.
RUN if ! getent group "${GROUP_ID}" >/dev/null; then groupadd -g "${GROUP_ID}" omega; fi \
 && if ! getent passwd "${USER_ID}" >/dev/null; then \
      useradd -m -d /home/user -u "${USER_ID}" -g "${GROUP_ID}" -s /bin/bash omega; \
    fi \
 && mkdir -p /home/user/.omega_auth \
 && chmod 0700 /home/user/.omega_auth \
 && chown -R "${USER_ID}:${GROUP_ID}" /home/user \
      /PeTTa/repos/Omega/memory /memory-transfer \
      /opt/huggingface /opt/sentence_transformers \
 && test -f /PeTTa/repos/Omega/src/check_account.py \
 && ln -s /PeTTa/repos/Omega/scripts/chatgpt-auth /usr/local/bin/omega-chatgpt-auth

# Account checks do not start the agent; retain a usable policy for full Omega.
RUN python3 - <<'PY'
from pathlib import Path
import yaml
path = Path('/PeTTa/repos/Omega/profile/policy.yaml')
policy = yaml.safe_load(path.read_text())
writable = policy['filesystem_policy']['read_write']
if '/home/user/.omega_auth' not in writable:
    writable.append('/home/user/.omega_auth')
path.write_text(yaml.safe_dump(policy, sort_keys=False))
PY

ENV HOME=/home/user \
    OMEGA_CHATGPT_AUTH_FILE=/home/user/.omega_auth/chatgpt.json
USER ${USER_ID}:${GROUP_ID}
WORKDIR /PeTTa

RUN python3 /PeTTa/repos/Omega/scripts/chatgpt-auth check --help >/dev/null

# Do not start Omega's API-key gateway or switch to its default nobody user.
ENTRYPOINT []
CMD ["/bin/bash"]
