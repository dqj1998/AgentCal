"""Supervisor loop: reconcile -> poll -> apply -> execute -> project."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .clock import utcnow
from .config import AppConfig
from .executor import Executor
from .fsm import Fsm
from .poller import CalendarPoller
from .projector import CalendarProjector
from .scheduler import Scheduler

log = logging.getLogger(__name__)


@dataclass
class TickReport:
    reconcile: dict = field(default_factory=dict)
    poll: dict | None = None
    applied: int = 0
    executed: int = 0
    projected: int = 0
    notified: int = 0


class Supervisor:
    def __init__(
        self,
        conn,
        cfg: AppConfig,
        fsm: Fsm,
        scheduler: Scheduler,
        executor: Executor,
        projector: CalendarProjector,
        poller: CalendarPoller | None = None,
        notifier=None,
        clock=utcnow,
    ):
        self.conn = conn
        self.cfg = cfg
        self.fsm = fsm
        self.scheduler = scheduler
        self.executor = executor
        self.projector = projector
        self.poller = poller
        self.notifier = notifier
        self.clock = clock

    def tick_once(self) -> TickReport:
        report = TickReport(reconcile=self.fsm.reconcile().__dict__)
        if self.poller is not None:
            stats = self.poller.tick()
            report.poll = stats.__dict__
            if stats.error == "auth_expired":
                log.error("calendar auth expired: run agentcal auth login")
                if self.notifier is not None:
                    self.notifier.system(
                        "日历授权失效：请在本机运行 agentcal auth login", key="auth_expired"
                    )
        report.applied = self.scheduler.apply_commands()
        report.executed = self.executor.run_due(limit=2)
        report.projected = self.projector.flush()
        if self.notifier is not None:
            report.notified = self.notifier.flush()
        return report

    def run_forever(self) -> None:
        while True:
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 - the loop must survive one bad tick
                log.exception("tick failed")
            time.sleep(self.cfg.core.poll_interval_s)
