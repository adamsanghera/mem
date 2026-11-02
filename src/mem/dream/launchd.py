"""The per-user launchd job that runs `mem dream run` on a schedule.

launchd starts jobs with a bare environment, so the plist pins absolute
paths: the mem executable, the install-time PATH (where `agent`, `git`, and
`uv` live), and MEM_ROOT.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

MINUTES_PER_DAY = 24 * 60


def plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def mem_executable() -> str:
    found = shutil.which("mem")
    return found or str(Path(sys.argv[0]).resolve())


def schedule(cfg: dict) -> list[dict]:
    """Calendar entries for a run every `interval_minutes`, aligned to the
    local time `run_at` (HH:MM). Not StartInterval: launchd drops an
    interval that comes due while the Mac sleeps, so on a laptop a daily
    interval may never fire, while a missed calendar time runs on wake."""
    interval = int(cfg["interval_minutes"])
    if interval <= 0 or MINUTES_PER_DAY % interval:
        raise RuntimeError(f"interval_minutes must divide a day ({MINUTES_PER_DAY}); got {interval}")
    try:
        hour, minute = (int(part) for part in str(cfg["run_at"]).split(":"))
    except ValueError:
        raise RuntimeError(f"run_at must be HH:MM; got {cfg['run_at']!r}") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise RuntimeError(f"run_at must be HH:MM; got {cfg['run_at']!r}")
    first = (hour * 60 + minute) % interval
    return [{"Hour": t // 60, "Minute": t % 60} for t in range(first, MINUTES_PER_DAY, interval)]


def render(r: Path, cfg: dict, mem_path: str, path_env: str) -> bytes:
    log = config.state_dir(r) / "launchd.log"
    return plistlib.dumps(
        {
            "Label": cfg["launchd_label"],
            "ProgramArguments": [mem_path, "dream", "run"],
            "StartCalendarInterval": schedule(cfg),
            "RunAtLoad": False,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "EnvironmentVariables": {
                "PATH": path_env,
                "MEM_ROOT": str(r),
                "HOME": str(Path.home()),
                # stdout is a file here, so Python would hold progress lines
                # until the run exits
                "PYTHONUNBUFFERED": "1",
            },
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
            "WorkingDirectory": str(r),
        }
    )


def install(r: Path, cfg: dict) -> Path:
    path = plist_path(cfg["launchd_label"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render(r, cfg, mem_executable(), os.environ.get("PATH", "")))
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{cfg['launchd_label']}"], capture_output=True)
    done = subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip()}")
    return path


def uninstall(cfg: dict) -> bool:
    path = plist_path(cfg["launchd_label"])
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{cfg['launchd_label']}"], capture_output=True)
    if path.exists():
        path.unlink()
        return True
    return False


def loaded(cfg: dict) -> bool:
    out = subprocess.run(
        ["launchctl", "print", f"{_domain()}/{cfg['launchd_label']}"], capture_output=True
    )
    return out.returncode == 0
