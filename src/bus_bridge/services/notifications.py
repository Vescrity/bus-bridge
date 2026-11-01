"""org.freedesktop.Notifications adapter.

The sandbox sees a normal notification server; the bridge is the actual client
of the host server and keeps the two id spaces apart.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

from .. import bus
from .base import ExportedService

log = logging.getLogger(__name__)

IFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"

METHODS = """
    <method name="Notify">
      <arg type="s" direction="in"/><arg type="u" direction="in"/>
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="as" direction="in"/>
      <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
      <arg type="u" direction="out"/>
    </method>
    <method name="CloseNotification"><arg type="u" direction="in"/></method>
    <method name="GetCapabilities"><arg type="as" direction="out"/></method>
    <method name="GetServerInformation">
      <arg type="s" direction="out"/><arg type="s" direction="out"/>
      <arg type="s" direction="out"/><arg type="s" direction="out"/>
    </method>
    <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
    <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
"""


class Notifications(ExportedService):
    name = IFACE
    path = PATH
    methods = {"Notify": METHODS}

    def __init__(self, router, rewrite_pid_hint: bool = True):
        super().__init__(router)
        self.rewrite_pid_hint = rewrite_pid_hint
        self.by_host: dict[int, tuple[str, int]] = {}
        self.by_inner: dict[tuple[str, int], int] = {}
        self.pid_cache: dict[str, int] = {}
        self._next_id = 1

    def start(self) -> None:
        super().start()
        bus.add_match(
            self.router.host,
            "type='signal',sender='org.freedesktop.Notifications',"
            "interface='org.freedesktop.Notifications'",
        )
        bus.add_match(
            self.router.private,
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged'",
        )

    def _alloc_id(self) -> int:
        while True:
            value = self._next_id
            self._next_id = 1 if value >= 0xFFFFFFFF else value + 1
            if all(inner != value for (_sender, inner) in self.by_inner):
                return value

    def _path(self) -> str:
        return PATH

    def _call(self, member: str, signature: str, args: tuple) -> Gio.DBusMessage:
        call = Gio.DBusMessage.new_method_call(IFACE, self._path(), IFACE, member)
        call.set_body(GLib.Variant(signature, args))
        return call

    # ------------------------------------------------------------- methods

    @staticmethod
    def _children(msg: Gio.DBusMessage) -> list[GLib.Variant]:
        """Return the body's top-level children as variants.

        Unpacking the whole body would recursively unpack variants inside
        ``a{sv}`` hints into plain Python values, which cannot be put back
        into a variant without losing their type.  Keeping the children as
        variants preserves signatures (and Unix fds) exactly.
        """
        body = msg.get_body()
        if body is None:
            return []
        return [body.get_child_value(index) for index in range(body.n_children())]

    @staticmethod
    def _add_hint(hints: GLib.Variant, key: str, value: GLib.Variant) -> GLib.Variant:
        entries = [hints.get_child_value(i) for i in range(hints.n_children())]
        entries = [
            entry
            for entry in entries
            if entry.get_child_value(0).get_string() != key
        ]
        entries.append(
            GLib.Variant.new_dict_entry(
                GLib.Variant("s", key), GLib.Variant.new_variant(value)
            )
        )
        return GLib.Variant.new_array(GLib.VariantType.new("{sv}"), entries)

    def handle_Notify(self, msg: Gio.DBusMessage) -> None:
        if self.rewrite_pid_hint:
            # Looking up the sender's pid is a synchronous D-Bus call, which
            # must not happen inside a filter.  Defer the whole call so the
            # lookup runs from an idle source.
            call = msg.copy()
            self.router.defer(lambda: self._notify(call))
        else:
            self._notify(msg)

    def _notify(self, msg: Gio.DBusMessage) -> None:
        children = self._children(msg)
        (
            app_name_v,
            replaces_id_v,
            app_icon_v,
            summary_v,
            body_v,
            actions_v,
            hints_v,
            timeout_v,
        ) = children
        app_name = app_name_v.get_string()
        replaces_id = replaces_id_v.get_uint32()
        app_icon = app_icon_v.get_string()
        summary = summary_v.get_string()
        body = body_v.get_string()
        actions = actions_v.unpack()
        timeout = timeout_v.get_int32()

        sender = msg.get_sender() or ""
        if self.rewrite_pid_hint:
            pid = self.pid_cache.get(sender)
            if pid is None:
                pid = bus.get_connection_pid(self.router.private, sender)
                if pid is not None:
                    self.pid_cache[sender] = pid
            if pid is not None:
                # Notification servers use this hint to associate the icon
                # with the real application, not with the proxy.
                hints_v = self._add_hint(hints_v, "sender-pid", GLib.Variant("i", pid))

        host_replaces = self.by_inner.get((sender, replaces_id), 0) if replaces_id else 0
        inner_id = replaces_id if host_replaces else self._alloc_id()

        call = Gio.DBusMessage.new_method_call(IFACE, self._path(), IFACE, "Notify")
        call.set_body(
            GLib.Variant.new_tuple(
                app_name_v,
                GLib.Variant("u", host_replaces),
                app_icon_v,
                summary_v,
                body_v,
                actions_v,
                hints_v,
                timeout_v,
            )
        )

        def on_reply(reply, error):
            if error or reply is None or reply.get_message_type() == bus.ERROR:
                log.warning("host Notify failed: %s", error or reply)
                self.relay_error(msg, reply)
                return
            host_id = reply.get_body().get_child_value(0).get_uint32()
            if host_replaces:
                self.by_host.pop(host_replaces, None)
            self.by_host[host_id] = (sender, inner_id)
            self.by_inner[(sender, inner_id)] = host_id
            log.debug("notify %s -> host %s", inner_id, host_id)
            self.router.reply(self.router.private, msg, GLib.Variant("(u)", (inner_id,)))

        self.router.request(self.router.host, call, IFACE, on_reply)

    def handle_CloseNotification(self, msg: Gio.DBusMessage) -> None:
        inner_id = self._children(msg)[0].get_uint32()
        sender = msg.get_sender() or ""
        host_id = self.by_inner.get((sender, inner_id))
        if host_id is not None:
            call = self._call("CloseNotification", "(u)", (host_id,))
            self.router.request(
                self.router.host,
                call,
                IFACE,
                lambda reply, error: log.debug("close %s: %s", host_id, error),
            )
        self.router.reply(self.router.private, msg)

    def _forward_simple(self, msg: Gio.DBusMessage) -> None:
        def on_reply(reply, error):
            if error or reply is None or reply.get_message_type() == bus.ERROR:
                self.relay_error(msg, reply)
            else:
                self.router.reply(self.router.private, msg, reply.get_body())

        self.router.request(self.router.host, msg, IFACE, on_reply)

    def handle_GetCapabilities(self, msg: Gio.DBusMessage) -> None:
        self._forward_simple(msg)

    def handle_GetServerInformation(self, msg: Gio.DBusMessage) -> None:
        self._forward_simple(msg)

    # Properties are not part of the spec, but forwarding them is harmless.
    def handle_Get(self, msg: Gio.DBusMessage) -> None:
        self._forward_simple(msg)

    def handle_GetAll(self, msg: Gio.DBusMessage) -> None:
        self._forward_simple(msg)

    def handle_Set(self, msg: Gio.DBusMessage) -> None:
        self._forward_simple(msg)

    # ------------------------------------------------------------- signals

    def on_host_signal(self, msg: Gio.DBusMessage) -> None:
        if msg.get_interface() != IFACE:
            return
        member = msg.get_member()
        body = self.body(msg)
        if member == "ActionInvoked":
            host_id, action = body
            entry = self.by_host.get(host_id)
            if entry is None:
                return
            inner_id = entry[1]
            self.router.emit(
                self.router.private,
                PATH,
                IFACE,
                "ActionInvoked",
                GLib.Variant("(us)", (inner_id, action)),
            )
        elif member == "NotificationClosed":
            host_id, reason = body
            entry = self.by_host.pop(host_id, None)
            if entry is None:
                return
            sender, inner_id = entry
            self.by_inner.pop((sender, inner_id), None)
            self.router.emit(
                self.router.private,
                PATH,
                IFACE,
                "NotificationClosed",
                GLib.Variant("(uu)", (inner_id, reason)),
            )

    def on_private_signal(self, msg: Gio.DBusMessage) -> None:
        if msg.get_member() != "NameOwnerChanged":
            return
        if msg.get_interface() != "org.freedesktop.DBus":
            return
        name, old_owner, new_owner = self.body(msg)
        if not name.startswith(":") or new_owner:
            return
        stale = [
            host_id
            for host_id, (sender, _inner) in self.by_host.items()
            if sender == name
        ]
        for host_id in stale:
            call = self._call("CloseNotification", "(u)", (host_id,))
            self.router.request(
                self.router.host,
                call,
                IFACE,
                lambda reply, error: None,
            )
            entry = self.by_host.pop(host_id, None)
            if entry:
                self.by_inner.pop((entry[0], entry[1]), None)
