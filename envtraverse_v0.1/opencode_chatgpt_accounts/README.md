# OpenCode ChatGPT accounts

Manage isolated ChatGPT logins for **OpenCode 2.3.0**, using the `@opencode/cli`
package. Each account directory is mounted directly at
`/home/user/.local/share/opencode` inside Docker. OpenCode v2 stores credentials
and conversation history in `opencode.db`; this tooling uses its credential
export/import commands instead of editing SQLite.

Requires Linux, Bash, Python 3.10+, Docker, and `flock`. No host OpenCode or Node
installation is needed. The shell entry points share `account_tools.py`.

## Usage

From this directory:

```bash
./build_docker.sh
./prepare_account.sh ./account01
./check_account.sh ./account01
./clean_account.sh ./account01
```

Paths can be absolute or relative to your current working directory. Account
folders created here are ignored by Git.

`prepare_account.sh` creates a private directory and starts OpenAI's ChatGPT
device-code login (`chatgpt-headless`). Follow the URL/code it displays, using
your ChatGPT account. It then verifies that an active ChatGPT OAuth credential
was stored. No browser callback port or host networking is required. If login
is interrupted or fails, the directory is retained; retry with:

```bash
./prepare_account.sh ./account01 --resume
```

`check_account.sh` first verifies the active stored credential is ChatGPT OAuth,
then makes a **live model request**, requiring a successful exit and exactly
`PING` in OpenCode's JSON text events. An API key or a login-status message does
not count as success. The check uses subscription quota, creates conversation
history, and persists refreshed credentials in the mounted account directory.
Host API keys and host OpenCode configuration are not passed into the container.

The default model is `openai/gpt-6.1-sol`. Override it if your account needs a
different OpenAI model:

```bash
./check_account.sh ./account01 --model openai/gpt-6-luna --timeout 180
# OPENCODE_MODEL also sets the default for check_account.sh.
```

Each command supports `--timeout SECONDS`, applied separately to each container.
Defaults are 900 seconds for preparation and 120 for checking/cleaning. Image
builds have no time limit. Timed-out/interrupted containers are explicitly
stopped; an error identifies the container if Docker cannot stop it.

## Cleaning an account

Stop any other processes or containers using the account first. Cleaning removes
all account data except stored credentials, including conversations, snapshots,
logs, and other files in this directory. It preserves **all exported credentials**,
including other providers, inactive entries, labels, metadata, and active flags.
An active ChatGPT OAuth credential is required before proceeding.

The cleanup uses two containers, both with networking disabled:

1. Export credentials into a private temporary directory beside the account.
2. Import them into a fresh directory and re-export them. Compare every exported
   field, ignoring only the order of entries.

Only after that comparison succeeds and both containers exit does the script
move the original directory aside, put the fresh directory in its place, and
delete the old data. Failed exports/imports leave the original account in place;
a failed replacement attempts to restore the original. Cleanup does not make a
model request, so it does not recreate conversation history. The result contains
a fresh database and OpenCode's normal initialization files, not an empty folder.

Temporary directories use mode `700`, credential exports use `600`, and exports
are never printed. If rollback or old-directory removal fails, the original is
retained at the recovery path printed by the script. A machine crash can likewise
leave a sibling `.account01.clean-*/original` directory. Keep it until recovery
is complete; if `account01` is missing, restore `original` to that path while no
process uses the account. Temporary credential exports are for offline cleanup
only. Do not use them to run an online account clone or restore an older export
over credentials that have since refreshed.

The OpenCode experiment controller calls `clean_account.sh` before every fresh
game, reusing its held account lock and built agent image. The verified fresh
database replaces the data at the selected account path, and the old state is
deleted before the online game starts. Cleanup therefore removes previous games
and the live preflight check from account history. A cleanup failure prevents
the game from starting. Online requests mount that same path read/write so token
refreshes stay current; recovery attempts within a game do not clean its account
and retain the session history needed to resume.

After each game container stops, the controller saves a usage-only snapshot to
`<game>/run/opencode_usage.json` using an offline collection. This preserves all
recorded session counters, including child and recovery sessions, title
generation, compaction, and known failed-step usage, before cleanup removes the
database history. Credentials and conversation contents are excluded. Provider
usage that was never reported or persisted cannot be recovered.

The sibling `.<account>.opencode.pending-usage.json` marker remains until a
snapshot has been saved successfully. A collection failure or controller crash
therefore blocks account commands and subsequent controller preflight/cleanup.
From the repository root, recover with:

```bash
python3 opencode_usage.py ACCOUNT
# Optionally reuse an existing compatible image with --image SAME_VERSION_IMAGE.
```

Recovery verifies that the previous game container has stopped, collects usage
from the same account path, and clears the marker only after saving the snapshot.
Do not remove the marker to bypass collection or clean the account manually.

## Locking and image reuse

All three account commands take an exclusive, nonblocking lock on
`<parent>/.<account-name>.opencode.lock`. A busy account fails immediately. The
lock is outside the account directory so it remains valid across cleanup; do
not delete lock files. The shared controller's OpenCode backend holds these locks
across cleanup and all game containers, passing its existing lock to cleanup. Other experiment
launchers must hold the same lock for the lifetime of their container, for example:

```bash
flock -n ./".account01.opencode.lock" docker run --rm \
  --mount "type=bind,source=$(realpath ./account01),target=/home/user/.local/share/opencode" \
  your-experiment-image
```

Other programs do not automatically honor this lock. These scripts refuse
symlink account directories and require an existing v2 `opencode.db` for checks
and cleanup. They do not migrate v1 `auth.json` account folders.

The image tag includes the pinned OpenCode version and your UID/GID, so files
remain owned by the invoking user. `build_docker.sh --if-missing` reuses that
image; without the flag it rebuilds it. On success, stdout contains only the
image name. Builds share a lock across repository copies and send Docker only
the Dockerfile, never credential-containing build context.

Every OpenCode command uses `--standalone`; its private server ends with the
command/container. Configuration, cache, and state outside the data directory
are ephemeral. When adding experiment integration, use the same OpenCode version
and database path as this image: opening a shared database with another version
may run migrations.

Account locking is shared with the Codex account tools. The native runners and
credential-preserving cleanup remain specific to each backend.

## Verification

Run the credential-free regression tests from the repository root:

```bash
python3 -B -m unittest discover -s tests -p 'test_opencode_chatgpt_accounts.py' -v
```

They exercise the shell entry points with a Docker stub, including failed
exports/imports, credential preservation, lock contention, timeouts, and rollback.
An optional test builds/reuses the real image, imports synthetic credentials,
creates an offline conversation, and verifies both credential preservation and
removal of the database's conversation rows. Its containers have no network:

```bash
OPENCODE_ACCOUNTS_DOCKER_TESTS=1 python3 -B -m unittest discover \
  -s tests -p 'test_opencode_chatgpt_accounts.py' -v
```

For a real account, the full manual cycle is prepare → check → clean → check.
The final check deliberately creates a new conversation; clean again if you
need a pristine account for an experiment.

Upstream references: [CLI and paths](https://opencode.ai/docs/cli/),
[authentication commands](https://opencode.ai/docs/cli/commands/),
[published package](https://www.npmjs.com/package/@opencode/cli/v/2.3.0).
