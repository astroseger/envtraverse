"""A local Omega channel that delivers one task prompt and prints agent messages."""

import json
from pathlib import Path
import sys

# PeTTa imports Python files directly; register the installed Omega module paths
# before importing its plugin interface from this external adapter.
_OMEGA_ROOT = Path("/PeTTa/repos/Omega")
for _location in (_OMEGA_ROOT, _OMEGA_ROOT / "profile", _OMEGA_ROOT / "providers", _OMEGA_ROOT / "src"):
    if str(_location) not in sys.path:
        sys.path.insert(0, str(_location))

from channels import CommChannel, registerCommChannel
from config import config_get_by_key


class PromptChannel(CommChannel):
    def __init__(self):
        self._pending = ""

    def start(self):
        path = config_get_by_key("taskPromptPath")
        if not path:
            raise RuntimeError("taskPromptPath is required for the envtraverse channel")
        self._pending = Path(path).read_text(encoding="utf-8").strip()
        if not self._pending:
            raise RuntimeError("The task prompt is empty")

    def receive(self):
        prompt, self._pending = self._pending, ""
        return prompt

    def send(self, message):
        print(json.dumps({"type": "omega_message", "message": message}, ensure_ascii=False), flush=True)

    def stop(self):
        self._pending = ""


def loadOmegaPlugin():
    registerCommChannel("envtraverse", PromptChannel())
