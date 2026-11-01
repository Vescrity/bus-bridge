"""Packaging: metadata, the wheel layout and the installed tree must work."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from harness import ROOT

from bus_bridge import __version__
from bus_bridge.daemon import PrivateBus


def build(target: Path, command: str) -> Path:
    from setuptools import build_meta

    project = target / f"project-{command}"
    shutil.copytree(
        ROOT,
        project,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    out = target / f"dist-{command}"
    out.mkdir()
    with contextlib.chdir(project), contextlib.redirect_stdout(io.StringIO()):
        return out / getattr(build_meta, command)(str(out))


def check_artifacts(target: Path) -> None:
    wheel = build(target, "build_wheel")
    dist_info = f"bus_bridge-{__version__}.dist-info"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata = archive.read(f"{dist_info}/METADATA").decode("utf-8")
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
    assert "bus_bridge/cli.py" in names, names
    assert f"{dist_info}/licenses/LICENSE" in names, names
    assert "License-Expression: GPL-3.0-or-later\n" in metadata, metadata
    assert "bus-bridge = bus_bridge.cli:main" in entry_points, entry_points

    sdist = build(target, "build_sdist")
    prefix = f"bus_bridge-{__version__}/"
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
    for path in ("LICENSE", "MANIFEST.in", "README.md", "README.zh-CN.md"):
        assert prefix + path in names, path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bus-bridge-pack-")
    check_artifacts(Path(tmp))
    site = os.path.join(tmp, "site")
    os.makedirs(site)
    shutil.copytree(os.path.join(ROOT, "src", "bus_bridge"), os.path.join(site, "bus_bridge"))

    env = dict(os.environ)
    env["PYTHONPATH"] = site

    version = subprocess.run(
        [sys.executable, "-m", "bus_bridge", "--version"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert version.returncode == 0, version.stderr
    assert "bus-bridge" in version.stdout, version.stdout

    host = PrivateBus(state_dir=os.path.join(tmp, "host"))
    try:
        address = host.start()
        script = 'test -n "$DBUS_SESSION_BUS_ADDRESS" && echo installed-run-ok'
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "bus_bridge",
                "run",
                "--host",
                address,
                "--",
                "sh",
                "-c",
                script,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "installed-run-ok" in proc.stdout, proc.stdout
    finally:
        host.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print("packaging: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
