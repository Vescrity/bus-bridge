"""Fake D-Bus services used by the integration tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gi.repository import Gio, GLib  # noqa: E402

from bus_bridge import bus  # noqa: E402

NOTIFY_XML = """
<node>
  <interface name="org.freedesktop.Notifications">
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
  </interface>
</node>
"""


class FakeNotificationServer:
    def __init__(self, address: str):
        self.conn = bus.connect(address)
        bus.request_name(self.conn, "org.freedesktop.Notifications")
        node = Gio.DBusNodeInfo.new_for_xml(NOTIFY_XML)
        self.iface = node.lookup_interface("org.freedesktop.Notifications")
        self.conn.register_object(
            "/org/freedesktop/Notifications", self.iface, self._call, None, None
        )
        self.notifications = []
        self.closed = []
        self.next_id = 42

    def _call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "Notify":
            host_id = self.next_id
            self.next_id += 1
            self.notifications.append((host_id, args))
            invocation.return_value(GLib.Variant("(u)", (host_id,)))
        elif method == "CloseNotification":
            self.closed.append(args[0])
            invocation.return_value(None)
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["body", "actions"],)))
        elif method == "GetServerInformation":
            invocation.return_value(
                GLib.Variant("(ssss)", ("fake", "bus-bridge", "1.0", "1.2"))
            )
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def action(self, host_id: int, action: str) -> None:
        self.conn.emit_signal(
            None,
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "ActionInvoked",
            GLib.Variant("(us)", (host_id, action)),
        )

    def close(self, host_id: int, reason: int = 2) -> None:
        self.conn.emit_signal(
            None,
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "NotificationClosed",
            GLib.Variant("(uu)", (host_id, reason)),
        )


MPRIS_PLAYER_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Play"/>
    <method name="Pause"/>
    <method name="Stop"/>
    <signal name="Seeked"><arg type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
  </interface>
</node>
"""

MPRIS_ROOT_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <property name="Identity" type="s" access="read"/>
  </interface>
</node>
"""

PROPERTIES_XML = """
<node>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg type="s" direction="in"/>
      <arg type="a{sv}" direction="out"/>
    </method>
    <method name="Set">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="v" direction="in"/>
    </method>
    <signal name="PropertiesChanged">
      <arg type="s"/><arg type="a{sv}"/><arg type="as"/>
    </signal>
  </interface>
</node>
"""

ROOT = "/org/mpris/MediaPlayer2"


class FakeMprisPlayer:
    """A minimal MPRIS player living on the *private* bus."""

    def __init__(self, address: str, name: str = "org.mpris.MediaPlayer2.fakeplayer"):
        self.conn = bus.connect(address)
        self.name = name
        bus.request_name(self.conn, name)
        self.calls: list[str] = []
        self.properties = {
            "PlaybackStatus": GLib.Variant("s", "Playing"),
            "CanPlay": GLib.Variant("b", True),
            "Identity": GLib.Variant("s", "Fake Player"),
        }

        self.player_iface = Gio.DBusNodeInfo.new_for_xml(
            MPRIS_PLAYER_XML
        ).lookup_interface("org.mpris.MediaPlayer2.Player")
        self.root_iface = Gio.DBusNodeInfo.new_for_xml(
            MPRIS_ROOT_XML
        ).lookup_interface("org.mpris.MediaPlayer2")
        self.props_iface = Gio.DBusNodeInfo.new_for_xml(
            PROPERTIES_XML
        ).lookup_interface("org.freedesktop.DBus.Properties")

        self.conn.register_object(ROOT, self.player_iface, self._player_call, None, None)
        self.conn.register_object(ROOT, self.root_iface, self._root_call, None, None)
        self.conn.register_object(ROOT, self.props_iface, self._props_call, None, None)

    def _player_call(self, conn, sender, path, iface, method, params, invocation):
        self.calls.append(method)
        invocation.return_value(None)

    def _root_call(self, conn, sender, path, iface, method, params, invocation):
        invocation.return_value(None)

    def _props_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "Get":
            _iface, prop = args
            value = self.properties.get(prop)
            if value is None:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownProperty", prop
                )
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "GetAll":
            invocation.return_value(GLib.Variant("(a{sv})", (self.properties,)))
        elif method == "Set":
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.PropertyReadOnly", "read-only"
            )
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )

    def set_property(self, prop: str, value: GLib.Variant) -> None:
        self.properties[prop] = value
        self.conn.emit_signal(
            None,
            ROOT,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", {prop: value}, [])),
        )

    def emit_seeked(self, position: int) -> None:
        self.conn.emit_signal(
            None,
            ROOT,
            "org.mpris.MediaPlayer2.Player",
            "Seeked",
            GLib.Variant("(x)", (position,)),
        )


SNI_WATCHER_XML = """
<node>
  <interface name="org.kde.StatusNotifierWatcher">
    <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
    <method name="RegisterStatusNotifierHost"><arg type="s" direction="in"/></method>
    <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
    <signal name="StatusNotifierItemUnregistered"><arg type="s"/></signal>
    <signal name="StatusNotifierHostRegistered"/>
  </interface>
</node>
"""

SNI_ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="Activate">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
    </method>
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
      <arg type="as" direction="in"/>
      <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" direction="in"/><arg type="s" direction="in"/>
      <arg type="v" direction="in"/><arg type="u" direction="in"/>
    </method>
    <signal name="LayoutUpdated">
      <arg type="u"/><arg type="i"/>
    </signal>
  </interface>
</node>
"""

SNI_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"
SNI_ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"


class FakeStatusNotifierWatcher:
    """A host-side watcher that records registrations."""

    def __init__(self, address: str):
        self.conn = bus.connect(address)
        bus.request_name(self.conn, SNI_WATCHER_NAME)
        node = Gio.DBusNodeInfo.new_for_xml(SNI_WATCHER_XML)
        iface = node.lookup_interface(SNI_WATCHER_NAME)
        self.registered: list[str] = []
        self.conn.register_object(
            SNI_WATCHER_PATH, iface, self._call, None, None
        )

    def _call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "RegisterStatusNotifierItem":
            self.registered.append(args[0])
            invocation.return_value(None)
        elif method == "RegisterStatusNotifierHost":
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )


class FakeStatusNotifierItem:
    """A sandbox tray item, optionally registering under a well-known name."""

    def __init__(
        self,
        address: str,
        name: str = "org.kde.StatusNotifierItem-test",
        title: str = "Fake Item",
    ):
        self.conn = bus.connect(address)
        self.name = name
        bus.request_name(self.conn, name)
        self.calls: list[tuple] = []
        self.title = title
        self.properties = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "fake-item"),
            "Title": GLib.Variant("s", title),
            "Menu": GLib.Variant("o", MENU_PATH),
        }

        item_node = Gio.DBusNodeInfo.new_for_xml(SNI_ITEM_XML)
        self.conn.register_object(
            SNI_ITEM_PATH,
            item_node.lookup_interface("org.kde.StatusNotifierItem"),
            self._item_call,
            None,
            None,
        )
        props_node = Gio.DBusNodeInfo.new_for_xml(PROPERTIES_XML)
        self.conn.register_object(
            SNI_ITEM_PATH,
            props_node.lookup_interface("org.freedesktop.DBus.Properties"),
            self._props_call,
            None,
            None,
        )
        menu_node = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        self.conn.register_object(
            MENU_PATH,
            menu_node.lookup_interface("com.canonical.dbusmenu"),
            self._menu_call,
            None,
            None,
        )
        menu_props = Gio.DBusNodeInfo.new_for_xml(PROPERTIES_XML).lookup_interface(
            "org.freedesktop.DBus.Properties"
        )
        self.conn.register_object(MENU_PATH, menu_props, self._menu_props_call, None, None)

    def _item_call(self, conn, sender, path, iface, method, params, invocation):
        self.calls.append((method, params.unpack() if params is not None else ()))
        invocation.return_value(None)

    def _props_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "Get":
            _iface, prop = args
            value = self.properties.get(prop)
            if value is None:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownProperty", prop
                )
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "GetAll":
            invocation.return_value(GLib.Variant("(a{sv})", (self.properties,)))
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        self.calls.append((method, params.unpack() if params is not None else ()))
        if method == "GetLayout":
            invocation.return_value(
                GLib.Variant("(u(ia{sv}av))", (7, (0, {}, [])))
            )
        elif method == "Event":
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )

    def _menu_props_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "GetAll":
            invocation.return_value(GLib.Variant("(a{sv})", ({},)))
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )

    def emit_new_title(self, title: str) -> None:
        self.title = title
        self.properties["Title"] = GLib.Variant("s", title)
        self.conn.emit_signal(
            None, SNI_ITEM_PATH, "org.kde.StatusNotifierItem", "NewTitle", None
        )
