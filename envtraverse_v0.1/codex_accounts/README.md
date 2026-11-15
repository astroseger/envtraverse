# Account tools

Requires Linux, Bash, Python 3.10+, Docker, and `flock`.

Account commands automatically build the shared account-tools image if it is
missing. To build it ahead of time (or do nothing if it already exists):

```bash
./codex_accounts/build_docker.sh --if-missing
```

It creates `codex-accounts-image-unified-agent-v1-0` using the host user's UID/GID.
This small image contains Codex and basic utilities, without the experiment's
scientific packages. Its Codex version is independent of experiment versions.
Existing images are reused, never automatically rebuilt. Concurrent first uses
share a build lock outside the experiment directories and recheck the image
after waiting, so only one builds it. Docker errors and build failures stop the
account command before it tries to use the image.

Account scripts call `build_docker.sh --if-missing` directly and capture its
output. On success, stdout contains only the image name. Build progress and
errors go to stderr; failures return a nonzero exit status without an image name.

From this directory:

```bash
./prepare_account.sh codex01
./check_account.sh codex01
./check_account_run.sh codex01
```

`check_codex_with_api_key.sh` uses the same image and reads `OPENAI_API_KEY`.
The account folders are mounted directly, so refreshed authentication remains
in the original folder. The Docker build has an empty filesystem context:
account folders and credentials are never sent to the builder.

Experiment controllers build and clean up their own three run-specific images;
they do not remove this account image. Their account checks may build it if
missing, just like standalone account commands. Run `build_docker.sh` without
arguments to rebuild it manually; manual builds use the same lock. Updating it
does not test or change the Codex version in an experiment image.

## Account locking

The controller and prepare/check/clean/interactive account commands take an
exclusive OS lock on the sibling `.<account>.codex.lock` file. An account already
in use fails immediately. The controller holds its locks across preflight,
cleanup, games, and shutdown; its helper processes inherit the same lock.
Locks are released when their owning processes close the descriptors or exit.
The lock files remain on disk and must not be removed to unlock an account.

Before using an account, the tools also check for active Docker containers
mounting that account or a file inside it. This catches containers left running
after a controller crash. Stop the identified container, then retry; Codex has
no persistent recovery marker to clear. These checks do not inspect credentials
or copy account data. Direct Codex/Docker commands must still coordinate account
use themselves because OS locks are advisory.

`clean_account.sh` preserves the existing `auth.json` and resets other account
state. The controller invokes it before each fresh game. Account movement uses
source and destination locks so a running account cannot be moved by
`find_and_move_account.sh`.
