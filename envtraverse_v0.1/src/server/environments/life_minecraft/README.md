# Life Minecraft

Real-time Minecraft Java 1.21.1 survival for EnvTraverse. Each `games` entry selects
one task from a declarative, versioned 20-task catalog. Each attempt gets a fresh
world, its own graphical client and server, and a private evaluator. The world
continues running between actions and while the agent thinks.

The agent receives pixels and controls. Inventory, kill/crafting counters, task
evaluation, Docker access, and recordings remain outside the agent workspace.

## Run an experiment

Requirements: Linux, Python 3.10+, Docker with Compose, and enough CPU/RAM for
graphical Minecraft. The existing client uses software rendering. The Python
host runtime uses the standard library. The ordinary EnvTraverse launcher also
requires its normal dependencies and configured model account/backend.

From the EnvTraverse source directory:

```bash
cp run_config_examples/run_config_life_minecraft.yaml /path/to/experiment/run_config.yaml
# Edit the model, account, tasks, and environment parameters.
python3 run_controller_mc.py --run-root /path/to/experiment --accept-eula
```

Use `--accept-eula` only after accepting the [Minecraft EULA](https://www.minecraft.net/eula).
`EULA=true` is also accepted. The wrapper prepares the two Minecraft images only
when missing or from the earlier unprepared format. Normal runs reuse those images
without invoking Docker build or reinstalling assets. It starts the external runtime,
runs the ordinary controller, and drains/cleans up its own Minecraft sessions.
`--skip-build` requires prepared images and is normally unnecessary.

Minecraft 1.21.1, PortableMC 5.0.6, Fabric versions, Python client dependencies,
and image base digests are pinned. Both client assets and server libraries are
installed in reusable Docker layers during image preparation. These layers are
before application-source copies, so changes to our observer or control code do
not redownload the large assets. Each new session copies the prepared files locally
into fresh volumes; worlds, player state, options, and authentication are separate.
The client checks a versioned asset marker and skips its installation phase.
A custom/incomplete client image can still use three bounded installation retries
before the single game launch; gameplay is never automatically restarted.

The host runtime manages the `life-minecraft-server` and `life-minecraft-client`
images separately from EnvTraverse's agent/server/proxy images. Minecraft asset
preparation and reuse require no changes to `src/build_dockers.sh`; the ordinary
EnvTraverse image build and cleanup behavior stays unchanged.

Rebuild intentionally after changing the graphical client or observer code:

```bash
python3 run_controller_mc.py --run-root /path/to/new-experiment \
  --accounts-dir /path/to/accounts --accept-eula --rebuild-images
```

This rebuilds the two Minecraft images while retaining Docker's dependency/asset
layers; it does not use `--no-cache` or upgrade pinned versions. Changing a version
is a deliberate source/lock-file edit. Each runtime resolves its Minecraft image tags to
immutable IDs once and records the lock in `run_mc/runtime.json`, so rebuilding
a tag elsewhere cannot change the images used by later tasks in that runtime.
Existing `run/` or `run_mc/` outputs are never overwritten; choose a new run root.
The runtime uses a private offline-mode server, with no published Minecraft port.
Microsoft account authentication is not configured by this first integration.

List task IDs and instructions:

```bash
bash src/server/environments/life_minecraft/host_runtime/list_tasks.sh
```

To run the host service separately:

```bash
bash src/server/environments/life_minecraft/host_runtime/setup.sh
bash src/server/environments/life_minecraft/host_runtime/run.sh \
  --run-root /path/to/experiment --accept-eula --spectator-port 8766
# In another terminal, after runtime readiness:
python3 run_controller.py --run-root /path/to/experiment
```

The environment server mounts only `run_mc/socket` at `/run/life-minecraft`.
Its adapter talks to `/run/life-minecraft/runtime.sock`; Docker stays on the host.
The controller assigns a private UUID to each attempt. Do not set `session_id`
yourself in experiment configuration.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `minecraft_task_timeout` | `1800` | Task budget in seconds of wall time after readiness |
| `difficulty` | `normal` | `peaceful`, `easy`, `normal`, or `hard` |
| `hardcore` | `false` | Requires hard difficulty; normal death/respawn otherwise allowed |
| `seed` | `123456` | Signed 64-bit world seed; recorded with the attempt |
| `width`, `height` | `960`, `540` | Frame dimensions and mouse-coordinate bounds |
| `capture_fps` | `30` | Target frame publication rate |
| `render_fps` | `60` | Client rendering limit |
| `record_fps` | `1` | JPEG archive rate; `0` disables frame archival |
| `startup_timeout_seconds` | `600` | Separate budget for Minecraft startup/downloads |

Frame rates are targets, not simulation steps. Minecraft normally simulates at
20 ticks/second independently. Rendering/capture throughput depends on host load;
the model reads frames at its own pace. Image preparation happens before the runtime starts. Game loading and any fallback
asset installation do not consume `minecraft_task_timeout`.

**Timeout semantics:** the evaluator records a `timeout` event and permanently
latches failure at the deadline. Minecraft, frame publication, recording, and
existing held controls continue. A goal reached afterwards cannot change that
failed result. The next valid action returns a terminal observation without
applying the requested input; held controls are then released. This does not
interrupt a thinking agent or implement an EnvTraverse/global server timeout.
Success and invalid episodes are also reported through the next action.

Exiting/disconnecting or restarting a game invalidates the attempt. Escape is
available for ordinary menus and respawning, so disconnects are detected rather
than prevented by hiding the game's controls. Runtime failures are recorded
separately from ordinary task failure.

## Agent interface

The generated environment description contains the task text, time budget, and
control reference. The current image is always at:

```text
/home/user/run/agent_run/latest_frame.jpg
```

The host writes a complete temporary JPEG alongside that file, then replaces it
with `os.replace`. Concurrent readers see a complete old or new image. The real
inventory/menu cursor is included in the pixels. The image and its temporary
files are ignored by the agent's workspace Git snapshots.

Actions return a small `action.json` acknowledgement, not a screenshot. The
response does not wait for a key hold to finish. State-changing requests must
not be blindly retried after an ambiguous transport failure; use EnvTraverse's
last-response recovery.

| Action | Example | Behavior |
| --- | --- | --- |
| `key_down` | `{"id":"key_down","key":"w"}` | Hold until explicitly released |
| `key_up` | `{"id":"key_up","key":"w"}` | Release that key |
| `key_press` | `{"id":"key_press","key":"w","duration_ms":250}` | Press and automatically release after 1–10000 ms; default 250 |
| `mouse_move` | `{"id":"mouse_move","dx":12,"dy":-5}` | Relative camera motion |
| `mouse_move` | `{"id":"mouse_move","x":400,"y":250}` | Absolute GUI motion, origin top-left |
| `mouse_down` | `{"id":"mouse_down","button":"left"}` | Hold a button, e.g. mining |
| `mouse_up` | `{"id":"mouse_up","button":"left"}` | Release that button |
| `mouse_click` | `{"id":"mouse_click","button":"left","x":400,"y":250}` | Move, press, and release; coordinates optional |
| `mouse_scroll` | `{"id":"mouse_scroll","amount":1}` | Scroll the wheel, -10 through 10 |
| `release_all` | `{"id":"release_all"}` | Release all held controls |

Keys: `w a s d e q f 1 2 3 4 5 6 7 8 9 space shift ctrl tab escape`.
Buttons: `left middle right`. Absolute and relative coordinates cannot be mixed.
Actions affect only the specified controls: moving the mouse leaves W held.
Holding W while thinking for ten seconds walks for ten seconds. Timed presses
are useful when that is unwanted. After Minecraft grabs the mouse again, its
first relative movement may be discarded by the vanilla client.

## Observe and review a run

Open the spectator URL printed by the launcher, or read `run_mc/runtime.json`.
The viewer lists sessions and serves the same canonical JPEGs as the workspace.
It has no control endpoint and does not send key releases. The viewer binds to
loopback by default; use SSH forwarding to view a remote runtime.

```text
run_mc/
  runtime.json
  runtime.log
  socket/runtime.sock
  sessions/<session UUID>/
    session.json
    backend.json
    events.jsonl
    actions.jsonl
    result.json
    verdict_state.json
    frames.jsonl
    frames/*.jpg
    docker.log
    containers.log
```

The formal task definition, settings, seed, task progress/events and final result
are private artifacts. Input records include the applied controls and automatic
release events. The frame manifest records capture sequence/timestamps. The
first version records JPEGs; it does not encode video on the fly. Sparse frames
miss short events and cannot establish exactly which frame a model read.
Actions plus the seed do not provide deterministic replay of Minecraft.

## Parallel games

Set `--max-sessions N` on the launcher/runtime and configure the ordinary runner
for at most N active games. For the API-key backend this is `max_parallel_games`;
account-backed runs are limited by their configured accounts. Each game uses
separate Docker networks/volumes and a dynamically allocated loopback bridge
port. The spectator service serves all sessions on one port. Start conservatively
with one or two games when using software rendering.

Every catalog task starts independently. Later goals do not inherit inventory or
world changes from earlier goals. EnvTraverse currently requires unique task IDs
within one `games` list; repeat an experiment in a separate run root for another
seed or attempt. The run seed is reproducible terrain configuration, not a promise
of identical real-time entity behavior.

## Validation

```bash
python3 -m pytest src/server/environments/life_minecraft/tests tests/test_life_minecraft_launcher.py
```

The unit tests cover action semantics, formal predicates, latched timeout,
terminal reporting on the next action, atomic publishing, session isolation,
spectator permissions, and cleanup. The live smoke entry point exercises the unchanged EnvTraverse HTTP server,
checks frames and held controls across an idle timeout, and verifies that the
next action returns terminal status. It requires the built images and Flask:

```bash
bash src/server/environments/life_minecraft/host_runtime/smoke.sh \
  --run-root /tmp/my-minecraft-smoke --accept-eula
```

To validate actual crafting, start a separate runtime and use:

```bash
PYTHONPATH=src/server/environments python3 -m life_minecraft.tests.live_crafting \
  --run-root /path/to/experiment
```

That helper creates its own disposable session, uses operator commands only to
prepare the crafting fixture, then crafts two wooden pickaxes using normal GUI
actions. It verifies that the recipe preview earns no credit and shift-click
crafting credits both outputs. It closes its session afterwards.

Validation includes a fresh installation with empty asset storage, actual GUI
crafting, two simultaneous games, and the complete timeout/spectator smoke. Frame
publication measured 26–30 FPS in short local samples. Prepared client assets and
server libraries also passed validation with networking disabled; a fresh-session
smoke test reused those assets without installation or downloads.


If startup fails, inspect `run_mc/sessions/<session>/client-logs/launcher.log`
(the launcher prints its exact path). Exhausted installer failures and stopped
containers are detected promptly. Preserve failed-run artifacts and retry in a
new `--run-root`; the launcher deliberately refuses to overwrite existing runs.
