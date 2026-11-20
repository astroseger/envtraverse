"""Prepare reusable, versioned Minecraft assets while building the client image.

Only the assets, libraries, and version files are copied into the image's data
directory. Credentials, options, saves, logs, and launcher caches stay out.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


MINECRAFT_VERSION = "1.21.1"
PORTABLEMC_VERSION = "5.0.6"
STAMP_NAME = ".minecraft-realtime-assets.json"
STAMP = {"schema_version": 1, "minecraft_version": MINECRAFT_VERSION,
         "portablemc_version": PORTABLEMC_VERSION}


def complete_layout(root: Path) -> bool:
    version = root / "versions" / MINECRAFT_VERSION
    return (all((root / name).is_dir() for name in ("assets", "libraries", "versions"))
            and (root / "assets/indexes").is_dir()
            and any((root / "assets/indexes").glob("*.json"))
            and (version / f"{MINECRAFT_VERSION}.json").is_file()
            and (version / f"{MINECRAFT_VERSION}.jar").is_file())


def cached_assets_ready(root: Path) -> bool:
    """Recognize the factory-prepared asset snapshot without network requests."""
    try:
        return json.loads((root / STAMP_NAME).read_text()) == STAMP and complete_layout(root)
    except (OSError, ValueError, TypeError):
        return False


def install(destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="minecraft-realtime-assets-") as temporary:
        source = Path(temporary)
        command = ["portablemc", "--main-dir", str(source), "start", MINECRAFT_VERSION,
                   "--dry", "--jvm", "/opt/java/openjdk/bin/java", "--username", "AgentPlayer"]
        for attempt in range(1, 4):
            print(f"Preparing image assets: Minecraft {MINECRAFT_VERSION}, attempt {attempt}/3", flush=True)
            result = subprocess.run(command, check=False)
            if result.returncode == 0:
                break
            if attempt == 3:
                raise RuntimeError(f"Minecraft image asset preparation failed after 3 attempts (exit {result.returncode})")
            time.sleep(attempt)
        if not complete_layout(source):
            raise RuntimeError("Minecraft installer returned success without the expected complete asset layout")
        destination.mkdir(parents=True, exist_ok=True)
        # Invalidate an earlier marker before copying, so an interrupted update
        # cannot leave a ready stamp describing partially replaced assets.
        (destination / STAMP_NAME).unlink(missing_ok=True)
        for name in ("assets", "libraries", "versions"):
            shutil.copytree(source / name, destination / name, dirs_exist_ok=True)
        temporary_stamp = destination / (STAMP_NAME + ".tmp")
        temporary_stamp.write_text(json.dumps(STAMP, sort_keys=True) + "\n")
        os.replace(temporary_stamp, destination / STAMP_NAME)
    print("Minecraft image assets ready; fresh sessions reuse this installed snapshot.", flush=True)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("/data/client/minecraft"))
    args = parser.parse_args(argv)
    install(args.destination)


if __name__ == "__main__":
    main()
