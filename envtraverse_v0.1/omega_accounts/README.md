# Omega account tools

These commands manage separate ChatGPT sign-ins for Omega. Each account directory
is mounted read/write at `/home/user/.omega_auth` inside a disposable container,
so token refresh updates the original host files. They use Omega's own
`scripts/chatgpt-auth` implementation, including its one-request account check.

Requirements: Linux, Docker, Git, Bash, and `flock` (from util-linux). Run the
scripts as the user who owns the account directories. Login requires a browser
on the Docker host; it uses browser OAuth with a localhost callback.

From the envtraverse directory:

```bash
./omega_accounts/build_docker.sh --if-missing
./omega_accounts/prepare_account.sh ./omega_accounts/omega01
./omega_accounts/check_account.sh ./omega_accounts/omega01
```

The build is automatic if the image is missing. `prepare_account.sh` prints a
login URL to open in your browser. It uses Linux host networking so the browser
can reach the container's localhost callback. It does not use device login.

New account folders are private (mode `0700`). The OAuth helper saves
`chatgpt.json` with mode `0600`, along with its lock file. If login fails or is
interrupted, retain that directory and retry explicitly:

```bash
./omega_accounts/prepare_account.sh --resume ./omega_accounts/omega01
```

This preserves Omega's app registration and host identity. Existing directories
are otherwise refused. To use an existing Omega login, supply the directory
containing its `chatgpt.json`, such as `~/.omega_auth`, to `check_account.sh`.
Do not supply Codex's `auth.json`; the two applications use different credentials.
The account directory must be writable and owned by the invoking user. Paths
containing commas, double quotes, or newlines are rejected because of Docker mount syntax.

## Checking an account

```bash
./omega_accounts/check_account.sh ./omega_accounts/omega01 --timeout 120
./omega_accounts/check_account.sh ./omega_accounts/omega01 --model MODEL_ID
```

The command calls `chatgpt-auth check` in the installed Omega checkout. It makes
one inference request, validates a dedicated test tool call without executing
it, and prints `Account OK` on success. This consumes ChatGPT plan allowance;
it does not report remaining balance or start the autonomous agent loop.

The default 120-second deadline covers the check worker's imports, token refresh,
model discovery, and inference. Image builds and Docker startup are separate.
Exit codes are `0` for success, `1` for failed validation or account checks,
`2` for invalid arguments, `124` for an expired check deadline, and `130` for
cancellation. Docker failures retain Docker's exit code. Interrupted requests
may already have consumed allowance. The check does not retry inference.

## Image installation and updates

The shared image is `omega-accounts-image-unified-agent-v1-0:UID-GID`, matching the
host user's identity. Different host users get different tags so private token
files retain the correct ownership. The installed Omega source comes from
`https://github.com/astroseger/Omega.git`, branch `chatgpt-subscription`.

`build_docker.sh` clones that branch into a temporary directory and builds its
existing Dockerfile. This installs the full SWI-Prolog/PeTTa/FAISS runtime,
Chroma bridge, Python dependencies, CPU PyTorch, and embedding model. The first
build downloads and compiles substantial dependencies. Docker caches its layers.
The base image is tagged `omega-subscription-base:COMMIT`; the account image also
records the source commit in `org.opencontainers.image.revision`.

The small wrapper layer sets the host UID/GID, home and authentication path,
gives that user access to runtime memory/cache directories, and includes the
authentication directory in Omega's filesystem policy. It replaces the stock
entrypoint, so account commands run directly without the API-key gateway.

Existing images are reused without fetching the branch. After pushing an Omega
change, explicitly rebuild to install it:

```bash
./omega_accounts/build_docker.sh
docker image inspect "omega-accounts-image-unified-agent-v1-0:$(id -u)-$(id -g)" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

Concurrent builds for the same UID/GID share a lock and recheck image existence
after acquiring it. Successful builds print only the image reference to stdout;
progress and failures go to stderr. Builds use a clean public source checkout
and an empty wrapper context, so local account directories are never sent to
Docker. Temporary checkouts are removed after the build.

Account directories created under this directory are Git-ignored. No cleanup,
account-migration, or extra run-check scripts are needed. These tools are
independent of envtraverse's experiment controllers; those still use their
existing Codex account configuration.

## Offline tests

```bash
python3 -m unittest discover -s omega_accounts/tests -v
```

The tests use temporary fake Docker/Git commands and do not log in or spend plan
allowance.
