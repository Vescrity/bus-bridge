"""MPRIS import: sandbox players become visible on the host bus."""

from __future__ import annotations

import logging

from gi.repository import Gio

from .. import bus

log = logging.getLogger(__name__)

PREFIX = "org.mpris.MediaPlayer2."
ROOT = "/org/mpris/MediaPlayer2"


class Mpris:
    def __init__(self, router):
        self.router = router

    def start(self) -> None:
        bus.add_match(
            self.router.private,
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
            f"arg0namespace='{PREFIX.rstrip('.')}'",
        )
        for name in bus.list_names(self.router.private):
            if name.startswith(PREFIX) and not name.startswith(":"):
                owner = bus.get_name_owner(self.router.private, name)
                if owner:
                    self.import_player(name, owner)

    def stop(self) -> None:
        for host_name, imp in list(self.router.imports.items()):
            if imp.kind == "mpris":
                self.router.unimport(host_name)

    def _matches(self, owner: str) -> list[str]:
        # MPRIS signals are emitted from ROOT (PropertiesChanged included).
        return [f"type='signal',sender='{owner}',path='{ROOT}'"]

    def import_player(self, name: str, owner: str) -> None:
        for imp in self.router.imports.values():
            if imp.kind == "mpris" and imp.inner_name == name:
                if imp.inner_owner == owner:
                    return
                imp.inner_owner = owner
                self.router.set_matches(imp, self._matches(owner))
                return
        self.router.import_service(
            "mpris",
            name,
            owner,
            preferred_host_name=name,
            matches=self._matches(owner),
        )

    def unimport_player(self, name: str) -> None:
        for host_name, imp in list(self.router.imports.items()):
            if imp.kind == "mpris" and imp.inner_name == name:
                self.router.unimport(host_name)

    def on_private_signal(self, msg: Gio.DBusMessage) -> None:
        interface = msg.get_interface()
        member = msg.get_member()
        if interface == "org.freedesktop.DBus" and member == "NameOwnerChanged":
            name, _old, new = msg.get_body().unpack()
            if not name.startswith(PREFIX):
                return
            if new:
                self.router.defer(lambda: self.import_player(name, new))
            else:
                self.router.defer(lambda: self.unimport_player(name))
            return

        sender = msg.get_sender()
        if not sender:
            return
        matching = [
            imp
            for imp in self.router.imports.values()
            if imp.kind == "mpris" and imp.inner_owner == sender
        ]
        if not matching:
            return
        path = msg.get_path() or ""
        if not (
            path == ROOT
            or path.startswith(ROOT + "/")
            or (interface or "").startswith("org.mpris.MediaPlayer2")
        ):
            return
        for imp in matching:
            self.router.relay_signal(msg, imp.channel)

    def on_host_signal(self, msg: Gio.DBusMessage) -> None:
        pass
