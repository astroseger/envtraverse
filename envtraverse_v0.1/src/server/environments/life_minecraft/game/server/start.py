"""Configure the pinned dedicated survival server without resetting its saved world."""
import os
import re
import shutil
from pathlib import Path


def main():
    if os.environ.get("EULA", "false").lower() != "true":
        raise SystemExit("Set EULA=true after accepting https://aka.ms/MinecraftEULA")
    difficulty = os.environ.get("DIFFICULTY", "normal").lower()
    if difficulty not in {"peaceful", "easy", "normal", "hard"}:
        raise SystemExit("DIFFICULTY must be peaceful, easy, normal, or hard")
    name = os.environ.get("PLAYER_NAME", "AgentPlayer")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
        raise SystemExit("PLAYER_NAME must be a valid Minecraft username")
    hardcore = boolean("HARDCORE", "false")
    online = boolean("ONLINE_MODE", "false")
    if hardcore == "true" and difficulty != "hard":
        raise SystemExit("HARDCORE=true requires DIFFICULTY=hard")
    data = Path("/data")
    data.mkdir(parents=True, exist_ok=True)
    os.chdir(data)
    Path("eula.txt").write_text("eula=true\n")
    Path("mods").mkdir(exist_ok=True)
    for source in Path("/opt/server/mods").glob("*.jar"):
        shutil.copy2(source, Path("mods") / source.name)
    props = {
        "gamemode": "survival", "force-gamemode": "true", "difficulty": difficulty,
        "hardcore": hardcore, "online-mode": online, "enforce-secure-profile": "false",
        "level-name": "world", "level-seed": os.environ.get("SEED", ""),
        "spawn-monsters": "true", "spawn-animals": "true", "spawn-npcs": "true",
        "allow-nether": "true", "pvp": "true", "enable-command-block": "false",
        "enable-rcon": "false", "enable-query": "false", "max-players": "1",
        "spawn-protection": "0", "view-distance": "8", "simulation-distance": "6",
        "player-idle-timeout": "0", "server-port": "25565", "motd": "LifeMC real-time survival",
    }
    # Properties are not shell code; reject line breaks to prevent injected settings.
    if any("\n" in str(v) or "\r" in str(v) for v in props.values()):
        raise SystemExit("Configuration values cannot contain newlines")
    Path("server.properties").write_text("".join(f"{k}={v}\n" for k, v in props.items()))
    # No operator accounts: gameplay API intentionally provides no console or RCON.
    Path("ops.json").write_text("[]\n")
    memory = os.environ.get("SERVER_MEMORY", "2G")
    if not re.fullmatch(r"[1-9][0-9]*[MG]", memory):
        raise SystemExit("SERVER_MEMORY must be an integer followed by M or G")
    os.execvp("java", ["java", "-Xms512M", f"-Xmx{memory}", "-jar", "/opt/server/fabric-server-launch.jar", "nogui"])


def boolean(name, default):
    value = os.environ.get(name, default).lower()
    if value not in {"true", "false"}:
        raise SystemExit(f"{name} must be true or false")
    return value


if __name__ == "__main__":
    main()
