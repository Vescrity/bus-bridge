"""StatusNotifierWatcher / StatusNotifierItem adapter.

Tray items live on the sandbox bus.  The bridge owns the watcher name there,
rewrites the item's service name to a host-owned name, and forwards the
property/method traffic (including DBusMenu) in both directions.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Gio, GLib

from .. import bus
from .base import ExportedService

log = logging.getLogger(__name__)

IFACE = "org.kde.StatusNotifierWatcher"
PATH = "/StatusNotifierWatcher"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"

METHODS = """
    <method name="RegisterStatusNotifierItem">
      <arg type="s" direction="in"/>
    </method>
    <method name="RegisterStatusNotifierHost">
      <arg type="s" direction="in"/>
    </method>
    <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
    <signal name="StatusNotifierItemUnregistered"><arg type="s"/></signal>
    <signal name="StatusNotifierHostRegistered"/>
"""


class Sni(ExportedService):
    name = IFACE
    path = PATH
    methods = {"RegisterStatusNotifierItem": METHODS}

    def __init__(self, router):
        super().__init__(router)
        self._counter = 0
        self.host_watcher_available = True

    def start(self) -> None:
        super().start()
        bus.add_match(
            self.router.private,
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged'",
        )

    def stop(self) -> None:
        for host_name, imp in list(self.router.imports.items()):
            if imp.kind == "sni":
                self.router.unimport(host_name)

    # ------------------------------------------------------------- helpers

    def _items(self) -> list:
        return [imp for imp in self.router.imports.values() if imp.kind == "sni"]

    def _next_host_name(self) -> str:
        self._counter += 1
        return f"org.kde.StatusNotifierItem-{os.getpid()}-{self._counter}"

    def _item_for_sender(self, sender: str) -> list:
        return [
            imp
            for imp in self._items()
            if imp.inner_owner == sender or imp.inner_name == sender
        ]

    def _matches(self, owner: str | None) -> list[str]:
        # Tray items emit properties, NewTitle/NewIcon and DBusMenu traffic
        # from their own connection; a sender-scoped rule covers all paths.
        return [f"type='signal',sender='{owner}'"] if owner else []

    # ------------------------------------------------------------- methods

    def handle_RegisterStatusNotifierItem(self, msg: Gio.DBusMessage) -> None:
        (service,) = self.body(msg)
        sender = msg.get_sender() or ""
        call = msg.copy()
        if service.startswith("/"):
            inner_name = sender
            item_path = service
        else:
            inner_name = service
            item_path = "/StatusNotifierItem"
        # The sender is the connection that owns the item (and therefore the
        # one whose signals are relevant).  Resolving a well-known name would
        # require a synchronous call, which is not safe inside a filter.
        owner = sender

        def work() -> None:
            imp = next(
                (
                    candidate
                    for candidate in self._items()
                    if candidate.inner_name == inner_name
                    and candidate.item_path == item_path
                    and candidate.inner_owner == owner
                ),
                None,
            )
            created = imp is None
            if imp is None:
                imp = self.router.import_service(
                    "sni",
                    inner_name,
                    owner,
                    preferred_host_name=self._next_host_name(),
                    item_path=item_path,
                    matches=self._matches(owner),
                )
            if imp is None:
                self.router.error(
                    self.router.private,
                    call,
                    "org.freedesktop.DBus.Error.LimitsExceeded",
                    "cannot allocate a host tray item name",
                )
                return

            request = Gio.DBusMessage.new_method_call(
                IFACE, PATH, IFACE, "RegisterStatusNotifierItem"
            )
            request.set_body(GLib.Variant("(s)", (imp.host_name,)))
            if created:
                self.router.emit(
                    self.router.private,
                    PATH,
                    IFACE,
                    "StatusNotifierItemRegistered",
                    GLib.Variant("(s)", (self._inner_ref(imp),)),
                )

            def on_reply(reply, error):
                if error or reply is None or reply.get_message_type() == bus.ERROR:
                    self.host_watcher_available = False
                    log.warning(
                        "host StatusNotifierWatcher rejected %s: %s",
                        imp.host_name,
                        error or reply,
                    )
                    self.router.reply(self.router.private, call)
                    return
                self.host_watcher_available = True
                log.info("registered tray item %s -> %s", inner_name, imp.host_name)
                self.router.reply(self.router.private, call)

            self.router.request(self.router.host, request, IFACE, on_reply)

        self.router.defer(work)

    def handle_RegisterStatusNotifierHost(self, msg: Gio.DBusMessage) -> None:
        self.router.reply(self.router.private, msg)

    def handle_Get(self, msg: Gio.DBusMessage) -> None:
        _interface, prop = self.body(msg)
        value = self._property(prop)
        if value is None:
            self.router.error(
                self.router.private,
                msg,
                "org.freedesktop.DBus.Error.UnknownProperty",
                f"Unknown property {prop}",
            )
            return
        self.router.reply(
            self.router.private, msg, GLib.Variant("(v)", (value,))
        )

    def handle_GetAll(self, msg: Gio.DBusMessage) -> None:
        props = {name: self._property(name) for name in self._properties()}
        self.router.reply(
            self.router.private,
            msg,
            GLib.Variant("(a{sv})", (props,)),
        )

    def handle_Set(self, msg: Gio.DBusMessage) -> None:
        self.router.error(
            self.router.private,
            msg,
            "org.freedesktop.DBus.Error.PropertyReadOnly",
            "StatusNotifierWatcher properties are read-only",
        )

    # ----------------------------------------------------------- properties

    def _properties(self) -> tuple[str, str, str]:
        return (
            "RegisteredStatusNotifierItems",
            "IsStatusNotifierHostRegistered",
            "ProtocolVersion",
        )

    def _inner_ref(self, imp) -> str:
        """The name a sandbox client can use for this item.

        Path-registered items are identified by their object path; items that
        registered a well-known name by that name.
        """
        if imp.inner_name.startswith(":"):
            return imp.item_path or imp.inner_name
        return imp.inner_name

    def _property(self, name: str) -> GLib.Variant | None:
        if name == "RegisteredStatusNotifierItems":
            return GLib.Variant("as", [self._inner_ref(imp) for imp in self._items()])
        if name == "IsStatusNotifierHostRegistered":
            return GLib.Variant("b", True)
        if name == "ProtocolVersion":
            return GLib.Variant("i", 0)
        return None

    # -------------------------------------------------------------- signals

    def on_private_signal(self, msg: Gio.DBusMessage) -> None:
        interface = msg.get_interface()
        if interface == "org.freedesktop.DBus" and msg.get_member() == "NameOwnerChanged":
            name, _old, new = self.body(msg)
            for imp in list(self._items()):
                if imp.inner_name != name:
                    continue
                if new:
                    imp.inner_owner = new
                    self.router.defer(
                        lambda imp=imp, new=new: self.router.set_matches(
                            imp, self._matches(new)
                        )
                    )
                else:
                    self.router.emit(
                        self.router.private,
                        PATH,
                        IFACE,
                        "StatusNotifierItemUnregistered",
                        GLib.Variant("(s)", (self._inner_ref(imp),)),
                    )
                    self.router.defer(
                        lambda host_name=imp.host_name: self.router.unimport(host_name)
                    )
            return

        sender = msg.get_sender()
        if not sender:
            return
        candidates = self._item_for_sender(sender)
        if not candidates:
            return
        path = msg.get_path() or ""
        iface = interface or ""
        relevant = (
            path.startswith("/StatusNotifierItem")
            or iface in (ITEM_IFACE, MENU_IFACE)
            or (iface == "org.freedesktop.DBus.Properties" and path.startswith("/StatusNotifierItem"))
        )
        if not relevant:
            return
        # Multiple items may share one connection. Prefer a path match, then
        # fall back to all items (same connection, so the payload is theirs).
        matched = [imp for imp in candidates if imp.item_path and path.startswith(imp.item_path)]
        for imp in matched or candidates:
            self.router.relay_signal(msg, imp.channel)

    def on_host_signal(self, msg: Gio.DBusMessage) -> None:
        pass
