"""Review SLAs (§8.4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DIGEST_CUTOFF = (19, 25)   # daily picks must be decided before the 19:30 digest (§8.4)


def default_due(task_type: str, created: datetime) -> datetime:
    """§8.4 SLAs: picks before tonight's digest; event/thesis items within hours; others within a day."""
    local = created.astimezone(IST)
    if task_type == "approve_reco":
        cutoff = local.replace(hour=DIGEST_CUTOFF[0], minute=DIGEST_CUTOFF[1], second=0, microsecond=0)
        return cutoff if cutoff > local else cutoff + timedelta(days=1)
    if task_type in ("high_severity_event", "review_thesis_break"):
        return created + timedelta(hours=4)
    return created + timedelta(hours=24)
