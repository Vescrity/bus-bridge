"""Private session bus lifecycle."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time

from gi.repository import GLib

log = logging.getLogger(__name__)


class PrivateBusError(RuntimeError):
    pass


def runtime_root() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "bus-bridge")


class PrivateBus:
    """A dbus-daemon instance that only the sandbox can see."""

    def __init__(self, state_dir: str | None = None, keep: bool = False):
        self._keep = keep
        self._proc: subprocess.Popen[str] | None = None
        self._owns_dir = state_dir is None
        if state_dir is None:
            os.makedirs(runtime_root(), mode=0o700, exist_ok=True)
            state_dir = tempfile.mkdtemp(prefix="sb-", dir=runtime_root())
        self.state_dir = state_dir
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.socket_path = os.path.join(self.state_dir, "bus")
        self.address = f"unix:path={self.socket_path}"

    def _write_config(self) -> str:
        """Write a minimal bus config.

        Using the stock session.conf would expose the host's activatable
        service list (and allow starting those services) inside the sandbox.
        A private bus must start with no service directories at all.
        """
        config_path = os.path.join(self.state_dir, "bus.conf")
        config = f"""<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>{self.address}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(config)
        return config_path

    def start(self) -> str:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        config_path = self._write_config()
        # --nofork keeps the daemon a child of the supervisor, which makes
        # cleanup deterministic even if the bridge or the payload crashes.
        self._proc = subprocess.Popen(
            [
                "dbus-daemon",
                "--config-file",
                config_path,
                "--nofork",
                "--print-address=1",
                "--print-pid=1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
        assert self._proc.stdout is not None
        deadline = time.monotonic() + 5.0
        printed = None
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                if self._proc.poll() is not None:
                    err = ""
                    if self._proc.stderr is not None:
                        err = self._proc.stderr.read()
                    raise PrivateBusError(
                        f"dbus-daemon exited early: {err.strip()}"
                    )
                continue
            line = line.strip()
            if line.startswith("unix:"):
                printed = line
                break
        if not printed:
            self.stop()
            raise PrivateBusError("dbus-daemon did not print an address")
        if printed != self.address:
            # Should not happen; trust the daemon.  The printed address can
            # carry a ",guid=..." suffix, which is not part of the path.
            self.address = printed
            if printed.startswith("unix:path="):
                self.socket_path = printed[len("unix:path=") :].split(",")[0]
        # Read the pid line, if any; it is informational only.
        if self._proc.stdout is not None:
            try:
                self._proc.stdout.readline()
            except Exception:
                pass
        return self.address

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
        self._proc = None
        if not self._keep:
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
            if self._owns_dir:
                shutil.rmtree(self.state_dir, ignore_errors=True)

    def __enter__(self) -> "PrivateBus":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def host_bus_address() -> str:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        return address
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidate = os.path.join(runtime, "bus")
        if os.path.exists(candidate):
            return f"unix:path={candidate}"
    raise PrivateBusError(
        "no host session bus: set DBUS_SESSION_BUS_ADDRESS or XDG_RUNTIME_DIR"
    )


class SignalTrap:
    """Turn SIGINT/SIGTERM into a Python callback without leaving the loop."""

    def __init__(self, callback):
        self._callback = callback
        self._sources: list[int] = []

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._sources.append(
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._handle, sig)
            )

    def _handle(self, sig: int) -> bool:
        log.info("received signal %s, shutting down", sig)
        self._callback()
        return GLib.SOURCE_REMOVE
