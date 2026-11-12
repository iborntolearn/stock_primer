"""Budget manager (§4.8): per-run and per-day spend, 80% soft warning, 100% hard stop."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class BudgetManager:
    def __init__(self, run_limit_usd: float, day_limit_usd: float, soft_fraction: float = 0.8,
                 day_spent_usd: float = 0.0, month_limit_usd: float = float("inf"), month_spent_usd: float = 0.0):
        self.run_limit = run_limit_usd
        self.day_limit = day_limit_usd
        self.month_limit = month_limit_usd
        self.soft_fraction = soft_fraction
        self.run_spent = 0.0
        self.day_spent = day_spent_usd
        self.month_spent = month_spent_usd
        self._warned = False
        self._lock = threading.Lock()

    def check(self) -> None:
        """Call before every LLM request."""
        if self.run_spent >= self.run_limit or self.day_spent >= self.day_limit or self.month_spent >= self.month_limit:
            raise BudgetExceeded(
                f"budget exhausted: run ${self.run_spent:.3f}/${self.run_limit:.2f}, "
                f"day ${self.day_spent:.3f}/${self.day_limit:.2f}, month ${self.month_spent:.2f}/${self.month_limit:.2f}"
            )

    def charge(self, usd: float) -> None:
        with self._lock:
            self.run_spent += usd
            self.day_spent += usd
            self.month_spent += usd
            if not self._warned and self.fraction_used() >= self.soft_fraction:
                self._warned = True
                log.warning("budget soft limit reached (%.0f%%)", self.fraction_used() * 100)

    def fraction_used(self) -> float:
        return max(self.run_spent / self.run_limit if self.run_limit else 1.0,
                   self.day_spent / self.day_limit if self.day_limit else 1.0,
                   self.month_spent / self.month_limit if self.month_limit else 1.0)

    @property
    def degraded(self) -> bool:
        return self.fraction_used() >= self.soft_fraction
