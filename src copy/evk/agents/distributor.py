"""Distribution agent — sends approved drafts via Inkbox.

Safety rails (per production mandate):

* Only drafts in ``status=APPROVED`` are ever shipped; anything else raises.
* Batches at most ``delivery_batch_size`` (≤ 45) drafts per pass.
* Sleeps ``delivery_delay_seconds`` between successive sends.
* Respects a daily per-process quota — a ``QuotaExceededError`` aborts the
  batch; un-sent drafts remain ``APPROVED`` for the next run to pick up.
* Every send attempt persists a status update, even on failure, so the UI
  is never out of sync with reality.
"""

from __future__ import annotations

from datetime import UTC, datetime

from evk.config import get_settings
from evk.factory import get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InkboxClient
from evk.logging import logger
from evk.models import DraftMessage, DraftStatus
from evk.ratelimit import (
    DailyQuota,
    QuotaExceededError,
    batched,
    get_daily_quota,
    sleep_between,
)


class DistributorAgent:
    """Send approved drafts. Never sends anything not in status=APPROVED."""

    def __init__(
        self,
        *,
        repos: Repos | None = None,
        inkbox: InkboxClient | None = None,
        quota: DailyQuota | None = None,
    ) -> None:
        self._repos = repos or get_repos()
        self._inkbox = inkbox or get_inkbox()
        self._quota = quota or get_daily_quota()

    def send_approved(
        self, *, limit: int = 100, batch_size: int | None = None
    ) -> list[DraftMessage]:
        """Send every APPROVED draft up to ``limit``. Returns the ones sent."""
        size = batch_size or get_settings().delivery_batch_size
        approved = self._repos.drafts.list_by_status(DraftStatus.APPROVED, limit=limit)
        sent: list[DraftMessage] = []
        for batch in batched(approved, size):
            for i, draft in enumerate(batch):
                try:
                    self._quota.charge(1)
                except QuotaExceededError:
                    logger.bind(draft_id=draft.id, remaining=len(approved) - len(sent)).warning(
                        "distributor.quota_exhausted"
                    )
                    raise
                try:
                    sent.append(self.send_one(draft))
                except Exception as exc:
                    logger.bind(draft_id=draft.id).exception("distributor.send_failed")
                    self._repos.drafts.patch(
                        draft.id,
                        {
                            "status": DraftStatus.FAILED.value,
                            "send_error": str(exc)[:500],
                        },
                    )
                if i < len(batch) - 1:
                    sleep_between()
        logger.bind(sent=len(sent), quota=self._quota.snapshot()).info("distributor.batch_complete")
        return sent

    def send_one(self, draft: DraftMessage) -> DraftMessage:
        """Send a single draft. Raises on failure; updates Firestore regardless."""
        if draft.status is not DraftStatus.APPROVED:
            raise ValueError(f"Draft {draft.id} is not approved (status={draft.status.value})")
        message_id = self._inkbox.send(
            to=[draft.to_email],
            subject=draft.subject,
            body_text=draft.body_text,
            body_html=draft.body_html or None,
        )
        now = datetime.now(UTC)
        self._repos.drafts.patch(
            draft.id,
            {
                "status": DraftStatus.SENT.value,
                "sent_at": now,
                "inkbox_message_id": message_id,
                "send_error": None,
            },
        )
        draft.status = DraftStatus.SENT
        draft.sent_at = now
        draft.inkbox_message_id = message_id
        logger.bind(draft_id=draft.id, to=draft.to_email, inkbox_id=message_id).info(
            "distributor.sent"
        )
        return draft


__all__ = ["DistributorAgent"]
