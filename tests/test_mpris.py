"""MPRIS import test: sandbox player visible on the host bus."""

from __future__ import annotations

import sys

from fakes import FakeMprisPlayer, ROOT
from harness import Harness, wait_until

from gi.repository import GLib

from bus_bridge import bus

PLAYER = "org.mpris.MediaPlayer2.fakeplayer"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


def main() -> int:
    harness = Harness(["--no-notifications", "--no-sni"], required_names=set())
    try:
        player = harness.call(lambda: FakeMprisPlayer(harness.private.address))
        host = harness.connect_host()

        def host_owns_name() -> bool:
            return harness.call(lambda: bus.name_has_owner(host, PLAYER))

        assert wait_until(host_owns_name, 5), "player name did not appear on the host bus"

        signals: list[tuple] = []

        def on_message(conn, msg, incoming, _user_data):
            if (
                incoming
                and msg.get_message_type() == bus.SIGNAL
                and (msg.get_sender() or "").startswith(":")
            ):
                signals.append(
                    (
                        msg.get_sender(),
                        msg.get_path(),
                        msg.get_interface(),
                        msg.get_member(),
                        msg.get_body().unpack() if msg.get_body() is not None else None,
                    )
                )
            return msg

        harness.call(lambda: host.add_filter(on_message, None))
        harness.call(
            lambda: bus.add_match(host, "type='signal',interface='org.freedesktop.DBus.Properties'")
        )
        harness.call(
            lambda: bus.add_match(host, f"type='signal',interface='{PLAYER_IFACE}'")
        )

        # 1. method call host -> sandbox
        harness.dbus_call(
            host,
            PLAYER,
            ROOT,
            PLAYER_IFACE,
            "Play",
            None,
            None,
            0,
            5000,
            None,
        )
        assert player.calls == ["Play"], player.calls

        # 2. property read host -> sandbox
        reply = harness.dbus_call(
            host,
            PLAYER,
            ROOT,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (PLAYER_IFACE, "PlaybackStatus")),
            GLib.VariantType.new("(v)"),
            0,
            5000,
            None,
        )
        assert reply.unpack()[0] == "Playing", reply.unpack()

        # 3. PropertiesChanged sandbox -> host
        player.set_property("PlaybackStatus", GLib.Variant("s", "Paused"))
        assert wait_until(
            lambda: any(
                s[3] == "PropertiesChanged" and s[4][0] == PLAYER_IFACE for s in signals
            ),
            3,
        ), signals
        changed = [
            s
            for s in signals
            if s[3] == "PropertiesChanged" and s[4][0] == PLAYER_IFACE
        ][-1]
        assert changed[4][1]["PlaybackStatus"] == "Paused", changed

        # 4. a plain MPRIS signal is relayed too
        player.emit_seeked(1234)
        assert wait_until(
            lambda: any(s[3] == "Seeked" and s[4] == (1234,) for s in signals), 3
        ), signals

        # 5. the host name is owned by the bridge, not by the sandbox connection
        owner = harness.call(lambda: bus.get_name_owner(host, PLAYER))
        assert owner and owner != player.conn.get_unique_name(), owner
        assert owner.startswith(":"), owner

        # 6. when the sandbox player exits, the host name goes away
        harness.call(lambda: player.conn.close_sync(None))
        assert wait_until(
            lambda: not harness.call(lambda: bus.name_has_owner(host, PLAYER)), 5
        ), "player name still present after the sandbox player exited"

        print("PASS mpris")
        return 0
    finally:
        print("--- bridge log ---")
        print(harness.logs())
        harness.stop()


if __name__ == "__main__":
    sys.exit(main())
