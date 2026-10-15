# Introduction

Before doing anything else, read `/observations/environment_description.md`. It describes the environment and the goal you must accomplish. Your current task is to accomplish the goal described in `/observations/environment_description.md`.


# Client

You interact with the real environment through `/client/client.py`.

Send an environment action as JSON:

```bash
python3 /client/client.py move '<action-json>'
```
The resulting observation is written to `/observations/`.

Do not edit, delete, or otherwise modify files in `/observations/`.

# Workspace

You start in `/home/user/run/agent_run`, which is your workspace.

Use your workspace however you find most effective. You may create and modify files and directories, and write any scripts or utilities you need.

If useful, simplify access to the observation data by creating helper index files, access scripts, or a separate directory inside your workspace containing copies of observations. You may automate copying new observations into that directory and modify those copies as needed.

# Attempts

If the game is in the `GAME_OVER` state, you should report it, and I will give you another attempt on the same level. You are forbidden to call RESET by yourself.
