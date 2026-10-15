# Example environment

A counter game with no dependencies beyond Python's standard library. Use game
ID `counter` and optionally set the positive integer parameter `target`
(default 3). Actions are `{"id":"INC"}` and `{"id":"RESET"}`.

Copy this package to create an environment. The manager validates configuration
in its constructor and owns one counter. `start_environment()` returns
`StartResult(environment_description=..., observation=...)`. Call `move()`
on the manager to advance the counter and `stop()` to close it.

Only one start attempt is allowed per instance. `stop()` is idempotent, and
moves before startup or after stop are rejected. The `observation()` method
is an implementation helper, not part of the shared contract.

No separate game class, registry, inheritance, HTTP code, or artifact encoding
is needed. See `../../README.md` for startup commands and the interface.
