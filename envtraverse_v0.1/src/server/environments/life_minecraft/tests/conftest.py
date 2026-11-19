"""Run the environment suite both standalone and from EnvTraverse's root."""
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parent))
# Installed layout: src/server/environments/life_minecraft.
SERVER = PACKAGE.parent.parent
if (SERVER / "environment_api.py").is_file():
    sys.path.insert(0, str(SERVER))
