"""Notification bridge test."""

from __future__ import annotations

import sys

from harness import Harness, wait_until
from fakes import FakeNotificationServer

from gi.repository import GLib

from bus_bridge import bus

IFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"


def notify(harness, client, args):
    return harness.dbus_call(
        client,
        IFACE,
        PATH,
        IFACE,
        "Notify",
        GLib.Variant("(susssasa{sv}i)", args),
        GLib.VariantType.new("(u)"),
        0,
        5000,
        None,
    )


def main() -> int:
    harness = Harness(
        ["--no-sni", "--no-mpris"],
        required_names={"org.freedesktop.Notifications"},
    )
    try:
        server = harness.call(lambda: FakeNotificationServer(harness.host.address))
        client = harness.connect_private()
        events: list[tuple] = []

        def on_message(conn, msg, incoming, _user_data):
            if (
                incoming
                and msg.get_message_type() == bus.SIGNAL
                and msg.get_interface() == IFACE
            ):
                events.append(
                    (
                        msg.get_sender(),
                        msg.get_path(),
                        msg.get_interface(),
                        msg.get_member(),
                        msg.get_body().unpack(),
                    )
                )
            return msg

        harness.call(lambda: client.add_filter(on_message, None))
        harness.call(
            lambda: bus.add_match(
                client, "type='signal',interface='org.freedesktop.Notifications'"
            )
        )

        reply = notify(
            harness,
            client,
            (
                "test-app",
                0,
                "dialog-information",
                "hello",
                "from the sandbox",
                ["default", "Open"],
                {"urgency": GLib.Variant("y", 1), "sender-pid": GLib.Variant("x", 1234)},
                5000,
            ),
        )
        inner_id = reply.unpack()[0]
        assert inner_id == 1, f"expected inner id 1, got {inner_id}"
        assert len(server.notifications) == 1, server.notifications
        host_id, args = server.notifications[0]
        assert host_id == 42, host_id
        assert args[0] == "test-app", args
        assert args[3] == "hello", args
        assert args[5] == ["default", "Open"], args
        assert args[7] == 5000, args
        assert args[6]["urgency"] == 1, args[6]
        assert args[6]["sender-pid"] == 1234, args[6]

        caps = harness.dbus_call(
            client, IFACE, PATH, IFACE, "GetCapabilities", None,
            GLib.VariantType.new("(as)"), 0, 5000, None,
        ).unpack()[0]
        assert caps == ["body", "actions"], caps

        # libnotify resolves the name and calls the owner's unique name
        # directly, so that path must be handled as well.
        owner = harness.call(lambda: bus.get_name_owner(client, IFACE))
        assert owner and owner.startswith(":"), owner
        info = harness.dbus_call(
            client, owner, PATH, IFACE, "GetServerInformation", None,
            GLib.VariantType.new("(ssss)"), 0, 5000, None,
        ).unpack()
        assert info == ("fake", "bus-bridge", "1.0", "1.2"), info

        server.action(host_id, "default")
        assert wait_until(lambda: events, 3), "no ActionInvoked"
        sender, path, iface, signal, params = events[-1]
        assert signal == "ActionInvoked", events
        assert params == (inner_id, "default"), params
        assert sender.startswith(":"), sender
        assert path == PATH and iface == IFACE

        # A replacement must reuse the inner id and map to the previous host id.
        reply = notify(harness, client, ("test-app", inner_id, "", "hello2", "", [], {}, 5000))
        assert reply.unpack()[0] == inner_id, reply.unpack()
        assert len(server.notifications) == 2
        assert server.notifications[1][1][1] == host_id, server.notifications[1]
        new_host_id = server.notifications[1][0]

        harness.dbus_call(
            client, IFACE, PATH, IFACE, "CloseNotification",
            GLib.Variant("(u)", (inner_id,)), None, 0, 5000, None,
        )
        assert wait_until(lambda: server.closed == [new_host_id], 3), server.closed

        server.close(new_host_id, 2)
        assert wait_until(lambda: len(events) >= 2, 3), "no NotificationClosed"
        assert events[-1][4] == (inner_id, 2), events[-1]

        print("PASS notifications")
        return 0
    finally:
        print("--- bridge log ---")
        print(harness.logs())
        harness.stop()


if __name__ == "__main__":
    sys.exit(main())
