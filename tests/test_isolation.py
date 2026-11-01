"""Isolation test: nothing crosses the bridge except the three adapters."""

from __future__ import annotations

import sys

from harness import Harness

from gi.repository import GLib

from bus_bridge import bus


def main() -> int:
    harness = Harness()
    try:
        host = harness.connect_host()
        private = harness.connect_private()

        # A sandbox-owned name must stay inside the sandbox.
        harness.call(lambda: bus.request_name(private, "com.example.SandboxOnly"))
        names = harness.call(lambda: bus.list_names(private))
        assert "com.example.SandboxOnly" in names, names
        host_names = harness.call(lambda: bus.list_names(host))
        assert "com.example.SandboxOnly" not in host_names, host_names

        # A host-owned name must not leak into the sandbox.
        harness.call(lambda: bus.request_name(host, "com.example.HostOnly"))
        private_names = harness.call(lambda: bus.list_names(private))
        assert "com.example.HostOnly" not in private_names, private_names

        # And the sandbox cannot reach it either.
        try:
            harness.dbus_call(
                private,
                "com.example.HostOnly",
                "/com/example/HostOnly",
                "com.example.HostOnly",
                "Ping",
                None,
                None,
                0,
                1000,
                None,
            )
        except GLib.Error as exc:
            assert "ServiceUnknown" in exc.message or "not provided" in exc.message, exc.message
        else:
            raise AssertionError("host-only service is reachable from the sandbox")

        # The private bus has no activatable services at all: the host's
        # service files must not be reachable through it.
        activatable = harness.dbus_call(
            private,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListActivatableNames",
            None,
            GLib.VariantType.new("(as)"),
            0,
            5000,
            None,
        ).unpack()[0]
        assert set(activatable) <= {"org.freedesktop.DBus"}, activatable

        # The sandbox's notification/tray names are served by the bridge, not
        # by the host (a sandbox must not be able to take them over).
        for name in ("org.freedesktop.Notifications", "org.kde.StatusNotifierWatcher"):
            owner = harness.call(lambda name=name: bus.get_name_owner(private, name))
            assert owner is not None, name
            assert owner != harness.call(lambda: bus.get_name_owner(host, name)), name

        print("PASS isolation")
        return 0
    finally:
        print("--- bridge log ---")
        print(harness.logs())
        harness.stop()


if __name__ == "__main__":
    sys.exit(main())
