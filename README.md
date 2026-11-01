# bus-bridge

> Project status：vibe-coding, not fully reviewed

Private D-Bus session for sandboxes, forwarding only the three desktop channels
that apps actually need:

- notifications (`org.freedesktop.Notifications`)
- tray (`org.kde.StatusNotifierWatcher` / `org.kde.StatusNotifierItem`,
  including DBusMenu)
- media players (`org.mpris.MediaPlayer2.*`, sandbox → host)

[中文文档](README.zh-CN.md)

## Architecture

The sandbox runs its own `dbus-daemon` with no service directories, and it
shares nothing with the host bus: host names, activatable services and portals
do not exist inside it.

The bridge puts exactly those three channels on top of that bus.  Every other
message stays in the sandbox, where applications share a complete, unrestricted
bus and can take names, message each other and export objects freely.

### The three adapters

| Adapter | Direction | How it works |
| --- | --- | --- |
| `notifications` | both ways | The bridge owns `org.freedesktop.Notifications` on the sandbox side and acts as a client of the host notification service, keeping a mapping between the two id spaces (including `replaces_id`, `CloseNotification`, `NotificationClosed` and `ActionInvoked`).  Notifications an application leaves behind are closed when it exits. |
| `sni` | both ways | The bridge owns `org.kde.StatusNotifierWatcher` on the sandbox side.  When an application registers a tray item, the bridge takes a host name `org.kde.StatusNotifierItem-<pid>-<n>` and registers it with the host watcher, then forwards property reads, `Activate`, `NewTitle`/`NewIcon` and the DBusMenu on the original paths. |
| `mpris` | sandbox → host | Watches name acquisition and release for `org.mpris.MediaPlayer2.*` inside the sandbox and exports each player on the host under the same name (`.sandbox` is appended on a clash), forwarding calls, properties and signals. |

### Why every imported service gets its own host connection

A D-Bus connection has exactly one unique name, and every signal carries it as
the sender.  If several sandbox services shared one connection on the host, a
match such as `sender='org.foo'` would also receive signals from `org.bar`.
`Router.import_service()` therefore opens a new host connection for every
exported service.

## Installation

```sh
python3 -m venv --system-site-packages ~/.venvs/deskcage
. ~/.venvs/deskcage/bin/activate
pip install .
```

`pipx install --system-site-packages .` works too.

## Usage

```sh
bus-bridge doctor                 # check the environment and host services
bus-bridge run -- bash            # private bus + command
bus-bridge serve --state-dir DIR  # bus + bridge only, prints the address
```

`run` waits until the bridge has registered its names, runs the command, and
destroys the bus when the command exits.

Notifications do not have their `sender-pid` hint rewritten by default; if the
host notification service relies on it to match application icons, enable
`--pid-hint` (the bridge looks up the sender PID asynchronously, with a one-off
synchronous call the first time).

Manual combination:

```sh
bus-bridge bus --state-dir /tmp/sb        # prints unix:path=...
bus-bridge bridge --private unix:path=/tmp/sb/bus
```

## Tests

```sh
tests/run_all.sh
```

Every test brings up two independent `dbus-daemon` instances (host and sandbox)
plus one bridge process:

- `test_notifications.py`: Notify, id mapping, replace, close, actions,
  server-side closing
- `test_mpris.py`: host-visible players, method calls, property reads,
  `PropertiesChanged`, `Seeked` relaying, names disappearing after the player
  exits
- `test_sni.py`: tray item registration (name based and path based), properties,
  `Activate`, `NewTitle`, DBusMenu, several applications at once, cleanup on exit
- `test_isolation.py`: sandbox names do not leak, host names stay invisible, no
  activatable services
- `test_pid_hint.py`: `--pid-hint` does not deadlock the filter
- `test_cli.py`: the private bus passed to `bus-bridge run` is usable and clean
- `test_packaging.py`: runs `-m bus_bridge` and `bus-bridge run` from a simulated
  installation directory (site-packages) without the source tree

The tests need PyGObject (`python-gobject`).

## Known limits

- **No portals.** xdg-desktop-portal permissions and identity are bound to the
  caller; the right approach is a per-sandbox `xdg-dbus-proxy` channel, not
  reusing this bridge.
- **MPRIS only flows sandbox → host.**  Sandboxed applications cannot see
  players that already exist on the host.
- Tray menu objects are forwarded by path; an application that puts its menu on
  an unusual path without emitting `NewMenu` needs extra declarations.
- When the host notification/tray service is missing the bridge still owns the
  sandbox-side names and calls return errors.
- One sandbox has exactly one bridge process; separate sandboxes never share a
  private bus.
