"""Common plumbing for a host service exported on the sandbox bus."""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

from .. import bus

log = logging.getLogger(__name__)

PEER_XML = """
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
    <method name="GetMachineId">
      <arg type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg type="s" direction="in"/>
      <arg type="s" direction="in"/>
      <arg type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg type="s" direction="in"/>
      <arg type="a{sv}" direction="out"/>
    </method>
    <method name="Set">
      <arg type="s" direction="in"/>
      <arg type="s" direction="in"/>
      <arg type="v" direction="in"/>
    </method>
  </interface>
"""


class ExportedService:
    """A service name owned on the private bus.

    Calls are handled in a GDBus filter, so arbitrary paths and members can be
    served without registering GObjects.
    """

    name: str = ""
    path: str = "/"
    methods: dict[str, str] = {}

    def __init__(self, router):
        self.router = router

    def start(self) -> None:
        self.router.export(self.name, self)

    def stop(self) -> None:
        pass

    # ------------------------------------------------------------- dispatch

    def introspection_xml(self) -> str:
        methods = "".join(self.methods.values())
        return f'<node><interface name="{self.name}">{methods}</interface>{PEER_XML}</node>'

    def on_private_call(self, msg: Gio.DBusMessage) -> None:
        iface = msg.get_interface()
        member = msg.get_member() or ""
        if iface == "org.freedesktop.DBus.Peer":
            if member == "Ping":
                self.router.reply(self.router.private, msg)
            elif member == "GetMachineId":
                self.router.reply(
                    self.router.private, msg, GLib.Variant("(s)", (machine_id(),))
                )
            else:
                self.router.error(
                    self.router.private,
                    msg,
                    "org.freedesktop.DBus.Error.UnknownMethod",
                    f"Unknown method {member}",
                )
            return
        if iface == "org.freedesktop.DBus.Introspectable" and member == "Introspect":
            self.router.reply(
                self.router.private,
                msg,
                GLib.Variant("(s)", (self.introspection_xml(),)),
            )
            return
        handler = getattr(self, f"handle_{member}", None)
        if handler is None:
            self.router.error(
                self.router.private,
                msg,
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method {iface}.{member}",
            )
            return
        handler(msg)

    # ------------------------------------------------------------ callbacks

    def on_private_signal(self, msg: Gio.DBusMessage) -> None:
        pass

    def on_host_signal(self, msg: Gio.DBusMessage) -> None:
        pass

    # -------------------------------------------------------------- helpers

    def body(self, msg: Gio.DBusMessage):
        value = msg.get_body()
        return value.unpack() if value is not None else ()

    def relay_reply(self, inner_call: Gio.DBusMessage, reply: GLib.Variant) -> None:
        self.router.reply(self.router.private, inner_call, reply)

    def relay_error(
        self, inner_call: Gio.DBusMessage, reply: Gio.DBusMessage | None
    ) -> None:
        if reply is not None and reply.get_message_type() == bus.ERROR:
            self.router.error(
                self.router.private,
                inner_call,
                reply.get_error_name() or "org.freedesktop.DBus.Error.Failed",
                (reply.get_body().unpack()[0] if reply.get_body() else "failed"),
            )
        else:
            self.router.error(
                self.router.private,
                inner_call,
                "org.freedesktop.DBus.Error.Failed",
                "upstream service unavailable",
            )


def machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="ascii") as handle:
                value = handle.read().strip()
            if value:
                return value
        except OSError:
            continue
    return "00000000000000000000000000000000"
