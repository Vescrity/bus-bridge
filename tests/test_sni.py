"""StatusNotifierItem / DBusMenu bridge test."""

from __future__ import annotations

import sys

from fakes import (
    MENU_PATH,
    SNI_ITEM_PATH,
    FakeStatusNotifierItem,
    FakeStatusNotifierWatcher,
)
from harness import Harness, wait_until

from gi.repository import GLib

from bus_bridge import bus

ITEM_IFACE = "org.kde.StatusNotifierItem"


def main() -> int:
    harness = Harness(
        ["--no-notifications", "--no-mpris"],
        required_names={"org.kde.StatusNotifierWatcher"},
    )
    try:
        watcher = harness.call(
            lambda: FakeStatusNotifierWatcher(harness.host.address)
        )
        item = harness.call(
            lambda: FakeStatusNotifierItem(harness.private.address)
        )
        private = harness.connect_private()
        watcher_signals: list[tuple[str, tuple]] = []

        def on_watcher_signal(conn, msg, incoming, _user_data):
            if (
                incoming
                and msg.get_message_type() == bus.SIGNAL
                and msg.get_interface() == "org.kde.StatusNotifierWatcher"
            ):
                watcher_signals.append((msg.get_member(), msg.get_body().unpack()))
            return msg

        harness.call(lambda: private.add_filter(on_watcher_signal, None))
        harness.call(
            lambda: bus.add_match(
                private,
                "type='signal',interface='org.kde.StatusNotifierWatcher'",
            )
        )
        harness.dbus_call(
            item.conn,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (item.name,)),
            None,
            0,
            5000,
            None,
        )
        assert wait_until(lambda: watcher.registered, 5), "watcher saw no item"
        assert wait_until(
            lambda: any(
                member == "StatusNotifierItemRegistered" and args == (item.name,)
                for member, args in watcher_signals
            ),
            3,
        ), watcher_signals

        host_name = watcher.registered[0]
        assert host_name != item.name, host_name
        assert host_name.startswith("org.kde.StatusNotifierItem-"), host_name

        host = harness.connect_host()
        assert harness.call(lambda: bus.name_has_owner(host, host_name)), host_name

        signals: list[tuple] = []

        def on_message(conn, msg, incoming, _user_data):
            if (
                incoming
                and msg.get_message_type() == bus.SIGNAL
                and msg.get_interface() == ITEM_IFACE
            ):
                signals.append((msg.get_sender(), msg.get_member()))
            return msg

        harness.call(lambda: host.add_filter(on_message, None))
        harness.call(
            lambda: bus.add_match(
                host, f"type='signal',interface='{ITEM_IFACE}',path='{SNI_ITEM_PATH}'"
            )
        )

        # 1. property read host -> sandbox item
        reply = harness.dbus_call(
            host,
            host_name,
            SNI_ITEM_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (ITEM_IFACE, "Category")),
            GLib.VariantType.new("(v)"),
            0,
            5000,
            None,
        )
        assert reply.unpack()[0] == "ApplicationStatus", reply.unpack()

        # 2. method call host -> sandbox item
        harness.dbus_call(
            host,
            host_name,
            SNI_ITEM_PATH,
            ITEM_IFACE,
            "Activate",
            GLib.Variant("(ii)", (0, 0)),
            None,
            0,
            5000,
            None,
        )
        assert ("Activate", (0, 0)) in item.calls, item.calls

        # 3. signal sandbox item -> host
        harness.call(lambda: item.emit_new_title("New Title"))
        assert wait_until(lambda: ("NewTitle" in [s[1] for s in signals]), 3), signals

        # 4. DBusMenu path forwarded verbatim
        reply = harness.dbus_call(
            host,
            host_name,
            MENU_PATH,
            "com.canonical.dbusmenu",
            "GetLayout",
            GLib.Variant("(iias)", (0, -1, [])),
            GLib.VariantType.new("(u(ia{sv}av))"),
            0,
            5000,
            None,
        )
        assert reply.unpack()[0] == 7, reply.unpack()

        # 5. the private watcher reports the local item reference
        props = harness.dbus_call(
            private,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems")),
            GLib.VariantType.new("(v)"),
            0,
            5000,
            None,
        )
        assert props.unpack()[0] == [item.name], props.unpack()

        # Clients may address the watcher through its unique name.
        owner = harness.call(
            lambda: bus.get_name_owner(
                private, "org.kde.StatusNotifierWatcher"
            )
        )
        assert owner and owner.startswith(":"), owner
        props = harness.dbus_call(
            private,
            owner,
            "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.kde.StatusNotifierWatcher", "ProtocolVersion")),
            GLib.VariantType.new("(v)"),
            0,
            5000,
            None,
        )
        assert props.unpack()[0] == 0, props.unpack()

        # 6. A second app on the same private bus, registering by object path.
        second = harness.call(
            lambda: FakeStatusNotifierItem(
                harness.private.address,
                name="org.kde.StatusNotifierItem-test2",
                title="Second Item",
            )
        )
        harness.dbus_call(
            second.conn,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (SNI_ITEM_PATH,)),
            None,
            0,
            5000,
            None,
        )
        assert wait_until(lambda: len(watcher.registered) == 2, 5), watcher.registered
        second_host = watcher.registered[1]
        assert second_host != host_name, watcher.registered

        reply = harness.dbus_call(
            host,
            second_host,
            SNI_ITEM_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (ITEM_IFACE, "Title")),
            GLib.VariantType.new("(v)"),
            0,
            5000,
            None,
        )
        assert reply.unpack()[0] == "Second Item", reply.unpack()

        harness.dbus_call(
            host,
            second_host,
            SNI_ITEM_PATH,
            ITEM_IFACE,
            "Activate",
            GLib.Variant("(ii)", (0, 0)),
            None,
            0,
            5000,
            None,
        )
        assert ("Activate", (0, 0)) in second.calls, second.calls
        assert ("Activate", (0, 0)) not in item.calls[1:], item.calls

        props = harness.dbus_call(
            private,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems")),
            GLib.VariantType.new("(v)"),
            0,
            5000,
            None,
        )
        assert props.unpack()[0] == [item.name, SNI_ITEM_PATH], props.unpack()

        # 7. closing an item's connection removes its host name
        harness.call(lambda: second.conn.close_sync(None))
        assert wait_until(
            lambda: any(
                member == "StatusNotifierItemUnregistered"
                and args == (SNI_ITEM_PATH,)
                for member, args in watcher_signals
            ),
            5,
        ), watcher_signals
        assert wait_until(
            lambda: not harness.call(lambda: bus.name_has_owner(host, second_host)), 5
        ), f"{second_host} still present after the item exited"

        print("PASS sni")
        return 0
    finally:
        print("--- bridge log ---")
        print(harness.logs())
        harness.stop()


if __name__ == "__main__":
    sys.exit(main())
