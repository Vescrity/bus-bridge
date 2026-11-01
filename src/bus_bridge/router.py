"""Message-level router between a private sandbox bus and the host bus.

The router owns a set of *exported* names on the private bus (host services
made visible to the sandbox) and a set of *imported* names on the host bus
(sandbox services made visible to the host).  Everything is forwarded at the
GDBusMessage level so that object paths, interfaces and bodies that the bridge
has never introspected still work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import Gio, GLib

from . import bus

log = logging.getLogger(__name__)


@dataclass
class Relay:
    """A forwarded call waiting for its reply."""

    conn: Gio.DBusConnection
    serial: int
    sender: str | None


@dataclass
class Callback:
    """An internal call waiting for its reply."""

    fn: Callable[[Gio.DBusMessage | None, str | None], None]
    timeout_id: int | None = None


@dataclass
class Import:
    """A sandbox service exported on the host bus."""

    kind: str
    host_name: str
    inner_name: str
    inner_owner: str | None
    channel: Gio.DBusConnection
    item_path: str | None = None
    meta: dict = field(default_factory=dict)
    matches: list[str] = field(default_factory=list)


class Router:
    def __init__(
        self,
        private: Gio.DBusConnection,
        host: Gio.DBusConnection,
        host_address: str,
        call_timeout: int = 25,
    ):
        self.private = private
        self.host = host
        self.host_address = host_address
        self.call_timeout = call_timeout
        self.pending: dict[tuple[Gio.DBusConnection, int], Relay | Callback] = {}
        self.exported: dict[str, object] = {}
        self.imports: dict[str, Import] = {}
        self.imports_by_conn: dict[Gio.DBusConnection, Import] = {}
        self.adapters: list[object] = []
        self._closed = False
        self.private.add_filter(self._private_filter, None)
        self.host.add_filter(self._host_filter, None)

    # ------------------------------------------------------------------ setup

    def add_adapter(self, adapter) -> None:
        adapter.router = self
        self.adapters.append(adapter)
        adapter.start()

    def defer(self, fn) -> None:
        """Run @fn from the main loop instead of the message filter.

        GDBus filters run inside a connection's dispatch; making synchronous
        calls there (connecting to another bus, RequestName, AddMatch) can
        deadlock because the nested loop never sees the reply.  Anything that
        needs a blocking call must be deferred to an idle source.
        """

        def run():
            try:
                fn()
            except Exception:
                log.exception("deferred task failed")
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run)

    def export(self, name: str, adapter) -> None:
        result = bus.request_name(self.private, name)
        if result not in (1, 4):
            raise RuntimeError(f"cannot own {name} on private bus (code {result})")
        self.exported[name] = adapter
        log.info("exported %s to sandbox", name)

    def import_service(
        self,
        kind: str,
        inner_name: str,
        inner_owner: str | None,
        preferred_host_name: str | None = None,
        item_path: str | None = None,
        meta: dict | None = None,
        matches: list[str] | None = None,
    ) -> Import | None:
        """Create a dedicated host connection owning a name for @inner_name.

        One connection per imported service is intentional: all signals from a
        single connection share one unique sender. If several sandbox services
        shared a connection, a host ``sender='org.foo'`` match would also match
        signals belonging to ``org.bar``.
        """
        candidate = preferred_host_name or inner_name
        channel = bus.connect(self.host_address)
        channel.add_filter(self._host_filter, None)
        host_name = self._claim_name(channel, candidate)
        if host_name is None:
            channel.close_sync(None)
            log.warning("cannot export %s: no free host name for %s", inner_name, candidate)
            return None
        imp = Import(
            kind=kind,
            host_name=host_name,
            inner_name=inner_name,
            inner_owner=inner_owner,
            channel=channel,
            item_path=item_path,
            meta=dict(meta or {}),
        )
        self.imports[host_name] = imp
        self.imports_by_conn[channel] = imp
        self.set_matches(imp, matches or [])
        log.info("imported %s as %s", inner_name, host_name)
        return imp

    def set_matches(self, imp: Import, rules: list[str]) -> None:
        """(Re)install the private-bus match rules for an import.

        Broadcast signals are only delivered to connections that asked for
        them, so relaying a sandbox service's signals requires a rule keyed on
        that service's current owner.
        """
        for rule in imp.matches:
            bus.remove_match(self.private, rule)
        imp.matches.clear()
        for rule in rules:
            try:
                bus.add_match(self.private, rule)
            except bus.BusError:
                log.warning("cannot add match %s", rule)
                continue
            imp.matches.append(rule)

    def _claim_name(self, channel: Gio.DBusConnection, candidate: str) -> str | None:
        try:
            if bus.request_name(channel, candidate) in (1, 4):
                return candidate
        except bus.BusError:
            pass
        for suffix in ("sandbox", "sandbox2", "sandbox3"):
            fallback = f"{candidate}.{suffix}"
            try:
                if bus.request_name(channel, fallback) in (1, 4):
                    return fallback
            except bus.BusError:
                continue
        return None

    def unimport(self, host_name: str) -> None:
        imp = self.imports.pop(host_name, None)
        if imp is None:
            return
        self.imports_by_conn.pop(imp.channel, None)
        for rule in imp.matches:
            bus.remove_match(self.private, rule)
        imp.matches.clear()
        try:
            bus.release_name(imp.channel, imp.host_name)
        except Exception:
            pass
        try:
            imp.channel.close_sync(None)
        except Exception:
            pass
        log.info("unimported %s", host_name)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for key in list(self.pending):
            value = self.pending.pop(key)
            if isinstance(value, Callback) and value.timeout_id:
                GLib.source_remove(value.timeout_id)
        for name in list(self.imports):
            self.unimport(name)
        for adapter in self.adapters:
            try:
                adapter.stop()
            except Exception:
                log.exception("adapter stop failed")
        for name, adapter in self.exported.items():
            try:
                bus.release_name(self.private, name)
            except Exception:
                pass

    # --------------------------------------------------------------- plumbing

    def request(
        self,
        conn: Gio.DBusConnection,
        call: Gio.DBusMessage,
        destination: str,
        callback: Callable[[Gio.DBusMessage | None, str | None], None],
        timeout: int | None = None,
    ) -> None:
        """Send @call on @conn and hand the reply to @callback."""
        out = bus.copy_for_send(call, destination=destination)
        ok, serial = conn.send_message(out, bus.SEND_NONE)
        if not ok:
            callback(None, "failed to send message")
            return
        key = (conn, serial)
        timeout_id = GLib.timeout_add_seconds(
            timeout or self.call_timeout, self._timeout_cb, key
        )
        self.pending[key] = Callback(callback, timeout_id)

    def forward_call(
        self,
        msg: Gio.DBusMessage,
        target: Gio.DBusConnection,
        destination: str,
        source: Gio.DBusConnection,
    ) -> bool:
        out = bus.copy_for_send(msg, destination=destination)
        ok, serial = target.send_message(out, bus.SEND_NONE)
        if not ok:
            log.warning("failed to forward call to %s", destination)
            return False
        if not (msg.get_flags() & bus.NO_REPLY_EXPECTED):
            self.pending[(target, serial)] = Relay(
                conn=source,
                serial=msg.get_serial(),
                sender=msg.get_sender(),
            )
        return True

    def relay_signal(
        self,
        msg: Gio.DBusMessage,
        target: Gio.DBusConnection,
        destination: str | None = None,
    ) -> None:
        out = bus.copy_for_send(msg, destination=destination)
        target.send_message(out, bus.SEND_NONE)

    def emit(
        self,
        conn: Gio.DBusConnection,
        path: str,
        iface: str,
        member: str,
        body: GLib.Variant | None = None,
        destination: str | None = None,
    ) -> None:
        bus.emit_signal(conn, path, iface, member, body, destination)

    def reply(self, conn: Gio.DBusConnection, call: Gio.DBusMessage, body=None) -> None:
        bus.make_reply(conn, call, body)

    def error(
        self,
        conn: Gio.DBusConnection,
        call: Gio.DBusMessage,
        name: str,
        message: str,
    ) -> None:
        bus.make_error(conn, call, name, message)

    # --------------------------------------------------------------- filters

    def _timeout_cb(self, key: tuple[Gio.DBusConnection, int]) -> bool:
        value = self.pending.pop(key, None)
        if isinstance(value, Callback):
            log.warning("timed out waiting for reply on %s", key[0].get_unique_name())
            value.fn(None, "timeout")
        return GLib.SOURCE_REMOVE

    def _consume_reply(self, conn: Gio.DBusConnection, msg: Gio.DBusMessage) -> bool:
        value = self.pending.pop((conn, msg.get_reply_serial()), None)
        if value is None:
            return False
        if isinstance(value, Callback):
            if value.timeout_id:
                GLib.source_remove(value.timeout_id)
            try:
                value.fn(msg, None)
            except Exception:
                log.exception("reply callback failed")
            return True
        out = bus.copy_for_send(
            msg, destination=value.sender, reply_serial=value.serial
        )
        value.conn.send_message(out, bus.SEND_NONE)
        return True

    def _adapter_for(self, msg: Gio.DBusMessage):
        destination = msg.get_destination()
        path = msg.get_path()
        adapter = self.exported.get(destination)
        if adapter is not None:
            if path == getattr(adapter, "path", None):
                return adapter
            return None
        # Clients that resolve a well-known name to its owner (libnotify does
        # this) address our unique name directly.  Dispatch those by object
        # path so they still reach the right adapter.
        if destination and destination == self.private.get_unique_name():
            for candidate in self.exported.values():
                candidate_path = getattr(candidate, "path", None)
                if candidate_path and path == candidate_path:
                    return candidate
        return None

    def _private_filter(self, conn, msg, incoming, _user_data):
        if not incoming:
            return msg
        try:
            mtype = msg.get_message_type()
            if mtype == bus.METHOD_CALL:
                adapter = self._adapter_for(msg)
                if adapter is not None:
                    adapter.on_private_call(msg)
                    return None
            elif mtype in (bus.METHOD_RETURN, bus.ERROR):
                if self._consume_reply(conn, msg):
                    return None
            elif mtype == bus.SIGNAL:
                for adapter in self.adapters:
                    try:
                        adapter.on_private_signal(msg)
                    except Exception:
                        log.exception("private signal handler failed")
        except Exception:
            log.exception("private filter failed")
        return msg

    def _host_filter(self, conn, msg, incoming, _user_data):
        if not incoming:
            return msg
        try:
            mtype = msg.get_message_type()
            if mtype == bus.METHOD_CALL:
                imp = self.imports.get(msg.get_destination())
                if imp is None and msg.get_destination() == conn.get_unique_name():
                    imp = self.imports_by_conn.get(conn)
                if imp is not None:
                    destination = imp.inner_owner or imp.inner_name
                    self.forward_call(msg, self.private, destination, conn)
                    return None
            elif mtype in (bus.METHOD_RETURN, bus.ERROR):
                if self._consume_reply(conn, msg):
                    return None
            elif mtype == bus.SIGNAL:
                for adapter in self.adapters:
                    try:
                        adapter.on_host_signal(msg)
                    except Exception:
                        log.exception("host signal handler failed")
        except Exception:
            log.exception("host filter failed")
        return msg
