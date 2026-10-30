# Upstream references — v0.31 (including earlier v0.3 checks)

Checked through web retrieval on September 18, 2026. These checks establish source
layout and intended release selection, not a successful public Git clone or native
Docker execution in the build environment.

## Sources used by setup

- TB2.1: https://github.com/harbor-framework/terminal-bench-2-1
- TB3.0: https://github.com/harbor-framework/terminal-bench/tree/v3.0.0
- TB4.0: https://github.com/harbor-framework/terminal-bench/tree/v4.0.0
- Science 0.1: https://github.com/harbor-framework/terminal-bench-science/tree/v0.1.0

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

## Science 0.1 support in v0.32

Checked on October 29, 2026 using the public `v0.1.0` Git file index and raw
configuration files:

- https://github.com/harbor-framework/terminal-bench-science/releases/tag/v0.1.0
- https://github.com/harbor-framework/terminal-bench-science/blob/v0.1.0/tasks/dataset.toml

The tag resolves to `f81afac4f11048e77a15dfc8fb1dbfb897fea0ce`. Its manifest lists
70 tasks, matching the 70 `tasks/<domain>/<field>/<slug>/task.toml` files. All 70
configurations and their artifact declarations passed validation with the locally
installed Harbor 0.23.0. They are single-stage Linux tasks using separate verifiers,
public agent networking, and no-network verifier networking. None requests a GPU.
Five tasks provide Docker Compose environments; three collect sidecar artifacts.

All tasks allow 28,800 seconds for solving. Verifier budgets reach 12,000 seconds,
and build budgets reach 7,200 seconds. The adapter no longer imposes a separate
finalization deadline over Harbor's verifier timeout. Startup transport limits
remain independently configurable; see the environment README for client limits.

These are metadata/schema checks, not successful builds or executions of the
official Science tasks. Setup resolves the release tag on the operator's host and
records the actual checkout commit through the existing lock mechanism.
