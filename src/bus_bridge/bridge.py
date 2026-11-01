"""Bridge orchestration: two buses, three adapters, one main loop."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gi.repository import GLib

from . import bus
from .daemon import SignalTrap
from .router import Router
from .services.mpris import Mpris
from .services.notifications import Notifications
from .services.sni import Sni

log = logging.getLogger(__name__)


@dataclass
class Config:
    notifications: bool = True
    sni: bool = True
    mpris: bool = True
    rewrite_pid_hint: bool = False
    call_timeout: int = 25


class Bridge:
    def __init__(self, private_address: str, host_address: str, config: Config):
        self.private_address = private_address
        self.host_address = host_address
        self.config = config
        self.router: Router | None = None
        self.loop: GLib.MainLoop | None = None

    def build(self) -> Router:
        private = bus.connect(self.private_address)
        host = bus.connect(self.host_address)
        router = Router(
            private,
            host,
            self.host_address,
            call_timeout=self.config.call_timeout,
        )
        if self.config.notifications:
            router.add_adapter(
                Notifications(router, rewrite_pid_hint=self.config.rewrite_pid_hint)
            )
        if self.config.sni:
            router.add_adapter(Sni(router))
        if self.config.mpris:
            router.add_adapter(Mpris(router))
        self.router = router
        return router

    def run(self) -> None:
        router = self.build()
        self.loop = GLib.MainLoop()
        SignalTrap(self.stop).install()
        log.info("bridge running (private=%s host=%s)", self.private_address, self.host_address)
        self.loop.run()
        router.shutdown()

    def stop(self) -> None:
        if self.router is not None:
            self.router.shutdown()
        if self.loop is not None and self.loop.is_running():
            self.loop.quit()
