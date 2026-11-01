"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

from gi.repository import GLib

from . import __version__
from .bridge import Bridge, Config
from .daemon import PrivateBus, PrivateBusError, host_bus_address

log = logging.getLogger("bus-bridge")

# Parent of the package: ``src`` in a checkout, ``site-packages`` when
# installed.  The child bridge is started with ``-m`` and this on PYTHONPATH,
# so ``bus-bridge run`` works from both layouts.
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config(args) -> Config:
    return Config(
        notifications=not args.no_notifications,
        sni=not args.no_sni,
        mpris=not args.no_mpris,
        rewrite_pid_hint=args.pid_hint,
        call_timeout=args.call_timeout,
    )


def _add_log_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-level", default=argparse.SUPPRESS)
    parser.add_argument("--log-file", default=argparse.SUPPRESS)


def _add_toggle_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-notifications", action="store_true", help="do not bridge notifications")
    parser.add_argument("--no-sni", action="store_true", help="do not bridge the tray")
    parser.add_argument("--no-mpris", action="store_true", help="do not bridge MPRIS")
    parser.add_argument("--pid-hint", action="store_true", help="rewrite the notification sender-pid hint (slow, off by default)")
    parser.add_argument("--call-timeout", type=int, default=25, help="upstream call timeout (seconds)")


def _setup_logging(level: str, path: str | None) -> None:
    if path:
        handler: logging.Handler = logging.FileHandler(path)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def cmd_bridge(args) -> int:
    config = _config(args)
    host = args.host or host_bus_address()
    bridge = Bridge(args.private, host, config)
    try:
        bridge.build()
    except Exception as exc:
        log.error("cannot start bridge: %s", exc)
        return 2
    bridge.loop = GLib.MainLoop()
    from .daemon import SignalTrap

    SignalTrap(bridge.stop).install()
    log.info("bridge: private=%s host=%s", args.private, host)
    bridge.loop.run()
    bridge.router.shutdown()
    return 0


def cmd_bus(args) -> int:
    daemon = PrivateBus(state_dir=args.state_dir, keep=args.keep)
    try:
        address = daemon.start()
    except Exception as exc:
        log.error("cannot start private bus: %s", exc)
        return 2
    print(address, flush=True)
    loop = GLib.MainLoop()
    from .daemon import SignalTrap

    SignalTrap(loop.quit).install()
    try:
        loop.run()
    finally:
        daemon.stop()
    return 0


def cmd_serve(args) -> int:
    daemon = PrivateBus(state_dir=args.state_dir, keep=args.keep)
    try:
        address = daemon.start()
    except Exception as exc:
        log.error("cannot start private bus: %s", exc)
        return 2
    bridge = Bridge(address, args.host or host_bus_address(), _config(args))
    try:
        bridge.build()
    except Exception as exc:
        log.error("cannot start bridge: %s", exc)
        daemon.stop()
        return 2
    print(address, flush=True)
    loop = GLib.MainLoop()
    bridge.loop = loop

    def stop() -> None:
        bridge.stop()
        daemon.stop()
        loop.quit()

    from .daemon import SignalTrap

    SignalTrap(stop).install()
    try:
        loop.run()
    finally:
        bridge.router.shutdown()
        daemon.stop()
    return 0


def cmd_run(args) -> int:
    daemon = PrivateBus(state_dir=args.state_dir, keep=args.keep)
    try:
        address = daemon.start()
    except Exception as exc:
        log.error("cannot start private bus: %s", exc)
        return 2

    child_env = dict(os.environ)
    existing = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = PKG_PARENT + (os.pathsep + existing if existing else "")

    bridge = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bus_bridge",
            "bridge",
            "--private",
            address,
            "--host",
            args.host or host_bus_address(),
            "--log-level",
            args.log_level,
            *(["--no-notifications"] if args.no_notifications else []),
            *(["--no-sni"] if args.no_sni else []),
            *(["--no-mpris"] if args.no_mpris else []),
            *(["--pid-hint"] if args.pid_hint else []),
            "--call-timeout",
            str(args.call_timeout),
        ],
        close_fds=True,
        env=child_env,
    )

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    env.pop("DBUS_STARTER_ADDRESS", None)
    env.pop("DBUS_STARTER_BUS_TYPE", None)
    env.pop("DBUS_SESSION_BUS_PID", None)

    try:
        # Wait for the bridge to own its names before starting the payload.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if bridge.poll() is not None:
                log.error("bridge exited during startup (code %s)", bridge.returncode)
                return 2
            try:
                from . import bus

                probe = bus.connect(address)
                names = bus.list_names(probe)
                probe.close_sync(None)
                wanted = set()
                if not args.no_notifications:
                    wanted.add("org.freedesktop.Notifications")
                if not args.no_sni:
                    wanted.add("org.kde.StatusNotifierWatcher")
                if wanted <= set(names):
                    break
            except Exception:
                pass
            time.sleep(0.05)
        return subprocess.call(args.command, env=env)
    finally:
        if bridge.poll() is None:
            bridge.terminate()
            try:
                bridge.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=3)
        daemon.stop()


def cmd_doctor(args) -> int:
    print(f"bus-bridge {__version__}")
    print(f"python               {sys.version.split()[0]}")
    print(f"dbus-daemon          {shutil.which('dbus-daemon') or 'MISSING'}")
    print(f"host bus             {os.environ.get('DBUS_SESSION_BUS_ADDRESS') or '(unset)'}")
    try:
        print(f"host bus resolved    {host_bus_address()}")
    except PrivateBusError as exc:
        print(f"host bus resolved    FAILED: {exc}")
    try:
        from . import bus

        conn = bus.connect(host_bus_address())
        names = bus.list_names(conn)
        for name in (
            "org.freedesktop.Notifications",
            "org.kde.StatusNotifierWatcher",
            "org.freedesktop.portal.Desktop",
        ):
            print(f"  {name:<38} {'yes' if name in names else 'no'}")
        conn.close_sync(None)
    except Exception as exc:
        print(f"host bus probe       FAILED: {exc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bus-bridge",
        description="Private session bus for sandboxes with selective desktop integration.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--log-level", default=os.environ.get("BUS_BRIDGE_LOG", "INFO"))
    parser.add_argument("--log-file", default=None)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_bridge = sub.add_parser("bridge", help="run the bridge in the foreground")
    p_bridge.add_argument("--private", required=True, help="private bus address")
    p_bridge.add_argument("--host", help="host bus address (default: session bus)")
    _add_log_args(p_bridge)
    _add_toggle_args(p_bridge)
    p_bridge.set_defaults(func=cmd_bridge)

    p_bus = sub.add_parser("bus", help="run a private bus and print its address")
    p_bus.add_argument("--state-dir")
    p_bus.add_argument("--keep", action="store_true", help="keep the state directory")
    _add_log_args(p_bus)
    p_bus.set_defaults(func=cmd_bus)

    p_serve = sub.add_parser("serve", help="run private bus + bridge, print the address")
    p_serve.add_argument("--state-dir")
    p_serve.add_argument("--keep", action="store_true", help="keep the state directory")
    p_serve.add_argument("--host", help="host bus address (default: session bus)")
    _add_log_args(p_serve)
    _add_toggle_args(p_serve)
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="run a command inside a fresh private bus")
    p_run.add_argument("--state-dir")
    p_run.add_argument("--keep", action="store_true", help="keep the state directory")
    p_run.add_argument("--host", help="host bus address (default: session bus)")
    _add_log_args(p_run)
    _add_toggle_args(p_run)
    p_run.add_argument("command", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run)

    p_doc = sub.add_parser("doctor", help="print environment / host service status")
    _add_log_args(p_doc)
    p_doc.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level, args.log_file)
    if args.subcommand == "run":
        if not args.command:
            parser.error("run requires a command")
        if args.command[0] == "--":
            args.command = args.command[1:]
    return args.func(args)
