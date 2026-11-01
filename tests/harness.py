"""Shared test harness: two private buses, one bridge process, one GLib loop.

All GDBus objects (fake services, test clients) live in a single background
thread that owns the default main context.  The test body runs in the main
thread and talks to them through `Harness.call()` / `Harness.dbus_call()`,
so blocking calls never fight another thread for the main context.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from gi.repository import GLib  # noqa: E402

from bus_bridge import bus  # noqa: E402
from bus_bridge.daemon import PrivateBus  # noqa: E402


class LoopThread:
    def __init__(self) -> None:
        self.loop = GLib.MainLoop.new(GLib.MainContext.default(), False)
        self.thread = threading.Thread(target=self.loop.run, daemon=True)
        self.thread.start()

    def call(self, fn, timeout: float = 10.0):
        box: dict = {}
        event = threading.Event()

        def idle() -> bool:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                event.set()
            return False

        GLib.idle_add(idle)
        if not event.wait(timeout):
            raise TimeoutError("main loop did not run the callback")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def stop(self) -> None:
        self.loop.quit()
        self.thread.join(timeout=2)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


class Harness:
    def __init__(
        self,
        bridge_args: list[str] | None = None,
        log_level: str = "DEBUG",
        required_names: set[str] | None = None,
    ):
        self.required_names = (
            required_names
            if required_names is not None
            else {
                "org.freedesktop.Notifications",
                "org.kde.StatusNotifierWatcher",
            }
        )
        base = tempfile.mkdtemp(prefix="bus-bridge-test-")
        self.base = base
        self.host = PrivateBus(state_dir=os.path.join(base, "host"))
        self.private = PrivateBus(state_dir=os.path.join(base, "private"))
        self.host.start()
        self.private.start()
        self.loop = LoopThread()
        self.log_path = os.path.join(base, "bridge.log")
        self.log = open(self.log_path, "w", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src")
        self.bridge = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bus_bridge",
                "bridge",
                "--private",
                self.private.address,
                "--host",
                self.host.address,
                "--log-level",
                log_level,
                *(bridge_args or []),
            ],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        if not self._wait_ready():
            raise RuntimeError(f"bridge did not become ready; log:\n{self.logs()}")

    def _wait_ready(self) -> bool:
        def ready() -> bool:
            conn = bus.connect(self.private.address)
            try:
                names = set(bus.list_names(conn))
            finally:
                conn.close_sync(None)
            return self.required_names <= names

        return wait_until(ready, 5.0)

    def call(self, fn):
        """Run fn() in the main-loop thread."""
        return self.loop.call(fn)

    def dbus_call(self, conn, *args, timeout: float = 10.0):
        """Async call executed in the loop thread, awaited by the test thread.

        Using call_sync here would run a nested main loop that does not
        dispatch the fake services' own callbacks, so the test would deadlock
        against itself.
        """
        box: dict = {}
        event = threading.Event()

        def start() -> None:
            def done(c, res, _user_data=None):
                try:
                    box["value"] = c.call_finish(res)
                except BaseException as exc:  # noqa: BLE001
                    box["error"] = exc
                finally:
                    event.set()

            conn.call(*args, done, None)

        self.call(start)
        if not event.wait(timeout):
            raise TimeoutError("D-Bus call timed out")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def connect_host(self):
        return self.call(lambda: bus.connect(self.host.address))

    def connect_private(self):
        return self.call(lambda: bus.connect(self.private.address))

    def logs(self) -> str:
        self.log.flush()
        with open(self.log_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def stop(self) -> None:
        self.bridge.terminate()
        try:
            self.bridge.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.bridge.kill()
            self.bridge.wait(timeout=3)
        self.loop.stop()
        self.host.stop()
        self.private.stop()
        self.log.close()
        shutil.rmtree(self.base, ignore_errors=True)
