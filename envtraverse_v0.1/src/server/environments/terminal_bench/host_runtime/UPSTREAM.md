# Upstream references — v0.31 (including earlier v0.3 checks)

Checked through web retrieval on September 18, 2026. These checks establish source
layout and intended release selection, not a successful public Git clone or native
Docker execution in the build environment.

## Sources used by setup

- TB2.1: https://github.com/harbor-framework/terminal-bench-2-1
- TB3.0: https://github.com/harbor-framework/terminal-bench/tree/v3.0.0
- TB4.0: https://github.com/harbor-framework/terminal-bench/tree/v4.0.0

The two new releases use tags of the same `harbor-framework/terminal-bench`
repository. Raw files at both tags were retrievable. Setup resolves tags to commit
hashes on the user's host and records those hashes; no unseen commit hash was
invented or embedded. Some old README naming differs from current project naming;
setup targets these explicit Git tags, not README dataset aliases or `latest`.

## Default new native test task

- https://raw.githubusercontent.com/harbor-framework/terminal-bench/v3.0.0/tasks/interleaved-vigenere/task.toml
- https://raw.githubusercontent.com/harbor-framework/terminal-bench/v4.0.0/tasks/interleaved-vigenere/task.toml
- https://raw.githubusercontent.com/harbor-framework/terminal-bench/v4.0.0/tasks/interleaved-vigenere/environment/Dockerfile
- https://raw.githubusercontent.com/harbor-framework/terminal-bench/v4.0.0/tasks/interleaved-vigenere/tests/test.sh

Both task configs use a separate verifier and request no GPU. Their CPU/memory
and agent-time settings differ. The adapter must read the chosen release's actual
configuration rather than override those differences. The newer test script is
an unmodified lifecycle probe; no reference solution is included.

## Infrastructure and compatibility

- https://www.tbench.ai/news/terminal-bench-3-0
- https://www.tbench.ai/news/terminal-bench-4-0
- https://www.tbench.ai/run
- https://raw.githubusercontent.com/harbor-framework/harbor/v0.23.0/src/harbor/models/task/config.py
- https://raw.githubusercontent.com/harbor-framework/harbor/v0.23.0/src/harbor/trial/trial.py

The releases include richer environments/GPU requirements; full-suite execution
requires suitable resources and a compatible backend. Harbor 0.23.0 understands
separate verifier configurations and sidecar artifact fields in the checked task
schemas. This is source-level compatibility evidence, not an audit of every task
or a native test of all backend paths.

## Timeout fix in v0.31

The pinned trial/trial.py, trial/single_step.py and trial/hooks.py were rechecked
through web retrieval for this fix. No native dependency could be installed here.
See TIMEOUTS.md for exact attributes/hooks used and their limits. Default setup now
selects 2.1 only; the optional release pins above are unchanged.
