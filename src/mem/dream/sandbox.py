"""Confine a headless dream to the paths and hosts its job needs.

Dreams read transcripts, and transcripts carry text from outside: web pages,
PR comments, tool output. A dream that obeys instructions planted there
should be able to damage no more than the corpus and skill folders it was
handed. So by default each run gets the agent CLI's sandbox:

- reads: the workspace, the corpus, the skill roots, the transcript's
  folder, and the `mem` install, nothing else in the home directory;
- writes: the workspace, the corpus, and the skill roots;
- network: the embedding server only.

The agent CLI ignores `--sandbox enabled` while its approval mode is "Run
Everything" (`--force`, or the user's own `approvalMode: unrestricted`), and
a workspace's `.cursor/cli.json` cannot change the approval mode. So the run
gets a private config dir (`CURSOR_CONFIG_DIR`) with allowlist approvals:
in print mode nobody can approve anything, so whatever the allowlist does
not grant is refused. That dir must sit outside every writable root, or the
agent could rewrite its own approval mode.

The path rules go in the workspace's `.cursor/sandbox.json`, which the
sandbox already protects from shell writes; a deny rule protects it from the
file tools too. `sandbox: false` in the dream config skips all of this and
runs with full access.
"""

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .. import embed
from . import config

LOOPBACK = ["127.0.0.1", "localhost", "::1"]


@dataclass
class Launch:
    """How to start one agent run: extra CLI flags, extra environment, and
    the directories to pass as `--add-dir`."""

    flags: list[str]
    env: dict[str, str] = field(default_factory=dict)
    add_dirs: list[Path] = field(default_factory=list)


def _inside(path: Path, roots: list[Path]) -> bool:
    path = path.resolve()
    return any(path == root.resolve() or path.is_relative_to(root.resolve()) for root in roots)


def _venv_python_home(venv: Path) -> Path | None:
    """The Python install a virtualenv runs on, from its pyvenv.cfg `home`."""
    try:
        lines = (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == "home" and value.strip():
            return Path(value.strip()).resolve().parent
    return None


def mem_install_paths() -> list[Path]:
    """What a sandboxed shell must read to run `mem`: this install's
    virtualenv, the Python behind it, and its source (outside the virtualenv
    when installed editable). The `mem` on PATH may be a different install,
    so its directory, virtualenv, and Python too."""
    paths = {Path(sys.prefix), Path(sys.base_prefix), Path(__file__).resolve().parents[1]}
    found = shutil.which("mem")
    if found:
        venv = Path(found).resolve().parents[1]
        paths |= {Path(found).parent, venv}
        home = _venv_python_home(venv)
        if home:
            paths.add(home)
    return sorted(paths)


def allowed_hosts() -> list[str]:
    host = urlparse(embed.base_url()).hostname
    return LOOPBACK + ([host] if host and host not in LOOPBACK else [])


def config_dir(cfg: dict) -> Path:
    return config.expand(cfg["sandbox_config_dir"])


def prepare(cfg: dict, workspace: Path, *, writable: list[Path], readable: list[Path]) -> Launch:
    """Write the run's sandbox files and return how to launch it. `writable`
    roots are also readable; `readable` paths are read-only. Raises
    RuntimeError if the private config dir sits inside a writable root."""
    if not cfg["sandbox"]:
        return Launch(["--force", "--sandbox", "disabled"], add_dirs=[*writable, *readable])

    cfg_dir = config_dir(cfg)
    if _inside(cfg_dir, [workspace, *writable]):
        raise RuntimeError(
            f"sandbox_config_dir {cfg_dir} is inside a root the dream can write, "
            "so a dream could loosen its own sandbox; move it"
        )
    cfg_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy_dir = workspace / ".cursor"
    policy_dir.mkdir(parents=True, exist_ok=True)

    roots = [str(p) for p in writable]
    cli_config = {
        "version": 1,
        "approvalMode": "allowlist",
        "sandbox": {"mode": "enabled", "networkAccess": "user_config_only", "readBoundary": "workspace"},
        "permissions": {
            "allow": [f"Write({root}/**)" for root in roots],
            "deny": [f"Write({policy_dir}/**)"],
        },
    }
    policy = {
        "readBoundary": "workspace",
        "additionalReadPaths": roots + [str(p) for p in [*readable, *mem_install_paths()]],
        "additionalReadwritePaths": roots,
        "networkPolicy": {"default": "deny", "allow": allowed_hosts()},
    }
    _write_json(cfg_dir / "cli-config.json", cli_config)
    _write_json(policy_dir / "sandbox.json", policy)
    return Launch(
        ["--sandbox", "enabled"],
        env={"CURSOR_CONFIG_DIR": str(cfg_dir)},
        add_dirs=list(writable),
    )


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
