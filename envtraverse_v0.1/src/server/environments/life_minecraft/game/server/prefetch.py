"""Build-time server installation, stopping at EULA refusal before any world exists.

Only immutable game/runtime directories survive in /data. Docker copies these
image files into each fresh world volume; user acceptance and all game state
are created separately by start.py when a real session starts.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import time
import zipfile


CACHE_DIRECTORIES = frozenset({".fabric", "libraries", "versions"})
EULA_REFUSAL = "You need to agree to the EULA in order to run the server"


def validate_cache(directory: Path, minecraft: str, loader: str) -> None:
    required = (
        directory / ".fabric/server" / f"{minecraft}-server.jar",
        directory / ".fabric/server" / f"fabric-loader-server-{loader}-minecraft-{minecraft}.jar",
        directory / "versions" / minecraft / f"server-{minecraft}.jar",
        directory / "libraries/net/fabricmc/fabric-loader" / loader / f"fabric-loader-{loader}.jar",
    )
    if any(not path.is_file() or not zipfile.is_zipfile(path) for path in required):
        raise RuntimeError("Server prefetch did not produce the required Minecraft/Fabric JARs")
    for name in CACHE_DIRECTORIES:
        if not (directory / name).is_dir():
            raise RuntimeError("Server installation cache is incomplete")
        for path in (directory / name).rglob("*.jar"):
            if not zipfile.is_zipfile(path):
                raise RuntimeError(f"Invalid downloaded JAR: {path.relative_to(directory)}")


def prepare(directory: Path, minecraft: str, loader: str, *, attempts: int = 3,
            run=subprocess.run, sleep=time.sleep) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "world").exists():
        raise RuntimeError("Refusing to prefetch into an existing game world")
    for attempt in range(1, attempts + 1):
        # The installer can leave a partial download after a failed request.
        # Remove only invalid JARs in the dedicated code-cache directories.
        for name in CACHE_DIRECTORIES:
            for path in (directory / name).rglob("*.jar"):
                if not zipfile.is_zipfile(path):
                    path.unlink()
        (directory / "eula.txt").write_text("eula=false\n")
        (directory / "server.properties").write_text("online-mode=false\n")
        print(f"Prefetching Minecraft {minecraft}/Fabric {loader} runtime (attempt {attempt}/{attempts})", flush=True)
        try:
            result = run(["java", "-Xms256M", "-Xmx1G", "-jar", "/opt/server/fabric-server-launch.jar", "nogui"],
                         cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, timeout=300)
            if (directory / "world").exists():
                raise RuntimeError("Server prefetch unexpectedly created a world")
            if result.returncode != 0 or EULA_REFUSAL not in result.stdout:
                print(result.stdout[-8000:], flush=True)
                raise RuntimeError("Server prefetch did not reach the expected EULA refusal")
            validate_cache(directory, minecraft, loader)
            break
        except (subprocess.TimeoutExpired, RuntimeError) as error:
            if (directory / "world").exists() or attempt == attempts:
                raise
            print(f"Server installation failed: {error}; retrying before gameplay", flush=True)
            sleep(2 ** attempt)
    # Keep only code/runtime caches. No EULA acceptance, world, properties,
    # player data, logs, or startup mods are baked into a reusable data volume.
    for path in directory.iterdir():
        if path.name not in CACHE_DIRECTORIES:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    print("Minecraft server runtime cached; EULA remains unaccepted and no world was created.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/data"))
    parser.add_argument("--minecraft", required=True)
    parser.add_argument("--loader", required=True)
    args = parser.parse_args()
    prepare(args.directory, args.minecraft, args.loader)


if __name__ == "__main__":
    main()
