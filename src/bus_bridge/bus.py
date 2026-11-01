"""Low-level D-Bus helpers shared by the bridge.

The bridge deliberately works at the GDBusMessage level: it must be able to
forward calls to object paths it has never introspected, and it must keep the
original message body/signature/fd list intact.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"
BUS_IFACE = "org.freedesktop.DBus"

CONNECTION_FLAGS = (
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
)

METHOD_CALL = Gio.DBusMessageType.METHOD_CALL
METHOD_RETURN = Gio.DBusMessageType.METHOD_RETURN
ERROR = Gio.DBusMessageType.ERROR
SIGNAL = Gio.DBusMessageType.SIGNAL

NO_REPLY_EXPECTED = Gio.DBusMessageFlags.NO_REPLY_EXPECTED
SEND_NONE = Gio.DBusSendMessageFlags.NONE


class BusError(RuntimeError):
    pass


def connect(address: str) -> Gio.DBusConnection:
    """Connect to a message bus, performing the SASL handshake and Hello."""
    if not address:
        raise BusError("empty bus address")
    try:
        conn = Gio.DBusConnection.new_for_address_sync(
            address, CONNECTION_FLAGS, None, None
        )
    except GLib.Error as exc:
        raise BusError(f"cannot connect to {address}: {exc.message}") from exc
    if conn is None:
        raise BusError(f"cannot connect to {address}")
    log.debug("connected to %s as %s", address, conn.get_unique_name())
    return conn


def call_sync(
    conn: Gio.DBusConnection,
    name: str,
    path: str,
    iface: str,
    method: str,
    params: GLib.Variant | None = None,
    reply_type: GLib.VariantType | None = None,
    timeout: int = -1,
) -> GLib.Variant:
    try:
        return conn.call_sync(
            name,
            path,
            iface,
            method,
            params,
            reply_type,
            Gio.DBusCallFlags.NONE,
            timeout,
            None,
        )
    except GLib.Error as exc:
        raise BusError(f"{iface}.{method} failed: {exc.message}") from exc


def request_name(conn: Gio.DBusConnection, name: str, flags: int = 4) -> int:
    """Request a well-known name.

    flags=4 (DO_NOT_QUEUE) is the default: an import must fail fast instead of
    silently queueing behind an unrelated process.
    """
    reply = call_sync(
        conn,
        BUS_NAME,
        BUS_PATH,
        BUS_IFACE,
        "RequestName",
        GLib.Variant("(su)", (name, flags)),
        GLib.VariantType.new("(u)"),
    )
    return reply.unpack()[0]


def release_name(conn: Gio.DBusConnection, name: str) -> int:
    reply = call_sync(
        conn,
        BUS_NAME,
        BUS_PATH,
        BUS_IFACE,
        "ReleaseName",
        GLib.Variant("(s)", (name,)),
        GLib.VariantType.new("(u)"),
    )
    return reply.unpack()[0]


def list_names(conn: Gio.DBusConnection) -> list[str]:
    reply = call_sync(
        conn,
        BUS_NAME,
        BUS_PATH,
        BUS_IFACE,
        "ListNames",
        None,
        GLib.VariantType.new("(as)"),
    )
    return list(reply.unpack()[0])


def get_name_owner(conn: Gio.DBusConnection, name: str) -> str | None:
    try:
        reply = conn.call_sync(
            BUS_NAME,
            BUS_PATH,
            BUS_IFACE,
            "GetNameOwner",
            GLib.Variant("(s)", (name,)),
            GLib.VariantType.new("(s)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
    except GLib.Error as exc:
        if "NameHasNoOwner" in exc.message or "not provided" in exc.message:
            return None
        raise BusError(f"GetNameOwner({name}) failed: {exc.message}") from exc
    return reply.unpack()[0]


def name_has_owner(conn: Gio.DBusConnection, name: str) -> bool:
    reply = call_sync(
        conn,
        BUS_NAME,
        BUS_PATH,
        BUS_IFACE,
        "NameHasOwner",
        GLib.Variant("(s)", (name,)),
        GLib.VariantType.new("(b)"),
    )
    return bool(reply.unpack()[0])


def add_match(conn: Gio.DBusConnection, rule: str) -> None:
    call_sync(
        conn,
        BUS_NAME,
        BUS_PATH,
        BUS_IFACE,
        "AddMatch",
        GLib.Variant("(s)", (rule,)),
    )


def remove_match(conn: Gio.DBusConnection, rule: str) -> None:
    try:
        call_sync(
            conn,
            BUS_NAME,
            BUS_PATH,
            BUS_IFACE,
            "RemoveMatch",
            GLib.Variant("(s)", (rule,)),
        )
    except BusError:
        pass


def get_connection_credentials(
    conn: Gio.DBusConnection, sender: str
) -> dict[str, GLib.Variant]:
    try:
        reply = conn.call_sync(
            BUS_NAME,
            BUS_PATH,
            BUS_IFACE,
            "GetConnectionCredentials",
            GLib.Variant("(s)", (sender,)),
            GLib.VariantType.new("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
    except GLib.Error:
        return {}
    return dict(reply.unpack()[0])


def get_connection_pid(conn: Gio.DBusConnection, sender: str) -> int | None:
    for key in ("UnixProcessID", "ProcessID"):
        value = get_connection_credentials(conn, sender).get(key)
        if value is not None:
            # unpack() recursively unpacks variants, so the value is usually
            # already a plain int here.
            return int(value.unpack() if hasattr(value, "unpack") else value)
    return None


def make_reply(
    conn: Gio.DBusConnection,
    call: Gio.DBusMessage,
    body: GLib.Variant | None = None,
) -> None:
    """Reply to @call, preserving the serial/sender bookkeeping."""
    reply = Gio.DBusMessage.new_method_reply(call)
    if body is not None:
        reply.set_body(body)
    conn.send_message(reply, SEND_NONE)


def make_error(
    conn: Gio.DBusConnection,
    call: Gio.DBusMessage,
    name: str,
    message: str,
) -> None:
    err = Gio.DBusMessage.new_method_error_literal(call, name, message)
    conn.send_message(err, SEND_NONE)


def emit_signal(
    conn: Gio.DBusConnection,
    path: str,
    iface: str,
    member: str,
    body: GLib.Variant | None = None,
    destination: str | None = None,
) -> None:
    sig = Gio.DBusMessage.new_signal(path, iface, member)
    if destination:
        sig.set_destination(destination)
    if body is not None:
        sig.set_body(body)
    conn.send_message(sig, SEND_NONE)


def copy_for_send(
    msg: Gio.DBusMessage,
    destination: str | None = None,
    reply_serial: int | None = None,
) -> Gio.DBusMessage:
    """Copy an incoming message so it can be sent on another connection.

    The sender field is cleared: the bus daemon owns it. GDBus allocates a new
    serial on send, which is why callers always use the serial returned by
    send_message() for reply correlation.
    """
    out = msg.copy()
    out.set_sender(None)
    out.set_destination(destination)
    if reply_serial is not None:
        out.set_reply_serial(reply_serial)
    return out
