"""CLI test: `bus-bridge run` gives the payload a working private bus."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from harness import ROOT

from bus_bridge.daemon import PrivateBus

SCRIPT = """
import os, sys
sys.path.insert(0, os.environ["BUS_BRIDGE_SRC"])
from bus_bridge import bus
conn = bus.connect(os.environ["DBUS_SESSION_BUS_ADDRESS"])
names = bus.list_names(conn)
print("names", sorted(n for n in names if not n.startswith(":")))
"""


def main() -> int:
    host = PrivateBus(state_dir=tempfile.mkdtemp(prefix="bus-bridge-cli-"))
    try:
        host_address = host.start()
        env = dict(os.environ)
        env["DBUS_SESSION_BUS_ADDRESS"] = host_address
        env["BUS_BRIDGE_SRC"] = os.path.join(ROOT, "src")
        env["PYTHONPATH"] = os.path.join(ROOT, "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "bus_bridge",
                "run",
                "--host",
                host_address,
                "--log-level",
                "WARNING",
                "--",
                sys.executable,
                "-c",
                SCRIPT,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        line = [l for l in proc.stdout.splitlines() if l.startswith("names")]
        assert line, proc.stdout
        names = line[0]
        for name in (
            "org.freedesktop.Notifications",
            "org.kde.StatusNotifierWatcher",
        ):
            assert name in names, names
        for leaked in ("com.example.HostOnly", "org.freedesktop.portal.Desktop"):
            assert leaked not in names, names
        print("PASS cli")
        return 0
    finally:
        host.stop()
        shutil.rmtree(host.state_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
