"""sender-pid hint rewriting (opt-in) must not deadlock the filter."""

from __future__ import annotations

import os
import sys

from fakes import FakeNotificationServer
from harness import Harness

from gi.repository import GLib

from bus_bridge import bus

IFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


def main() -> int:
    harness = Harness(
        ["--no-sni", "--no-mpris", "--pid-hint"],
        required_names={"org.freedesktop.Notifications"},
    )
    try:
        server = harness.call(lambda: FakeNotificationServer(harness.host.address))
        client = harness.connect_private()
        reply = harness.dbus_call(
            client,
            IFACE,
            PATH,
            IFACE,
            "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                ("pid-app", 0, "", "s", "b", [], {"urgency": GLib.Variant("y", 1)}, 1000),
            ),
            GLib.VariantType.new("(u)"),
            0,
            5000,
            None,
        )
        assert reply.unpack()[0] == 1, reply.unpack()
        assert len(server.notifications) == 1
        hints = server.notifications[0][1][6]
        assert hints["sender-pid"] == os.getpid(), hints
        assert hints["urgency"] == 1, hints
        print("PASS pid-hint")
        return 0
    finally:
        print("--- bridge log ---")
        print(harness.logs())
        harness.stop()


if __name__ == "__main__":
    sys.exit(main())
