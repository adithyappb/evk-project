"""Application-deadline reminder agent.

For each opportunity whose deadline falls in the configured reminder window,
email opted-in students who received a draft for it (i.e. were matched) and
haven't already been reminded at that interval.
"""

from __future__ import annotations

from datetime import UTC, datetime

from evk.config import get_settings
from evk.factory import get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InkboxClient
from evk.logging import logger
from evk.models import DraftStatus, Opportunity, ReminderLog, Student


class ReminderAgent:
    """Scheduled agent — run periodically (cron, Cloud Scheduler, APScheduler)."""

    def __init__(
        self,
        *,
        repos: Repos | None = None,
        inkbox: InkboxClient | None = None,
    ) -> None:
        self._repos = repos or get_repos()
        self._inkbox = inkbox or get_inkbox()
        self._settings = get_settings()

    def run(self) -> int:
        """Send all due reminders. Returns count sent."""
        days_windows = sorted(self._settings.reminder_days_before, reverse=True)
        if not days_windows:
            return 0
        horizon = max(days_windows)
        upcoming = self._repos.opportunities.with_upcoming_deadlines(within_days=horizon)
        sent = 0
        now = datetime.now(UTC)
        for opp in upcoming:
            if opp.deadline is None:
                continue
            days_left = (opp.deadline - now).total_seconds() / 86_400
            window = _closest_window(days_left, days_windows)
            if window is None:
                continue
            sent += self._remind_students_for(opp, days_before=window)
        return sent

    def _remind_students_for(self, opp: Opportunity, *, days_before: int) -> int:
        # Find every student we already drafted/sent for this opportunity.
        drafts_all = self._repos.drafts.list_by_status(DraftStatus.SENT, limit=1000)
        relevant = [d for d in drafts_all if d.opportunity_id == opp.id]
        sent = 0
        for draft in relevant:
            if self._repos.reminders.exists(draft.student_id, opp.id, days_before):
                continue
            student = self._repos.students.get(draft.student_id)
            if not student or not student.opted_in:
                continue
            try:
                self._send_reminder(student=student, opp=opp, days_before=days_before)
                sent += 1
            except Exception:
                logger.bind(
                    student_id=student.id, opp_id=opp.id, days_before=days_before
                ).exception("reminder.send_failed")
        return sent

    def _send_reminder(self, *, student: Student, opp: Opportunity, days_before: int) -> None:
        use_whatsapp = (
            student.preferred_notification_method == "whatsapp"
            and bool(student.phone)
        )
        if use_whatsapp:
            self._send_whatsapp_reminder(student=student, opp=opp, days_before=days_before)
        else:
            self._send_email_reminder(student=student, opp=opp, days_before=days_before)
        reminder_id = f"{student.id}_{opp.id}_{days_before}"
        self._repos.reminders.upsert(
            ReminderLog(
                id=reminder_id,
                student_id=student.id,
                opportunity_id=opp.id,
                days_before=days_before,
            )
        )

    def _send_email_reminder(self, *, student: Student, opp: Opportunity, days_before: int) -> None:
        subject = f"Reminder: {opp.title} — {days_before} day{'s' if days_before != 1 else ''} left"
        deadline_str = opp.deadline.date().isoformat() if opp.deadline else "soon"
        link_line = f"\nApply: {opp.url}\n" if opp.url else ""
        body_text = (
            f"Hi {student.name.split()[0] if student.name else 'there'},\n\n"
            f'Quick nudge — the deadline for "{opp.title}" ({opp.organization}) '
            f"is {deadline_str}, about {days_before} day"
            f"{'s' if days_before != 1 else ''} away.\n\n"
            f"{opp.summary}\n{link_line}\n"
            "If you've already applied, ignore this — and good luck!\n\n"
            "— The Opportunities Team\n"
        )
        message_id = self._inkbox.send(to=[student.email], subject=subject, body_text=body_text)
        logger.bind(
            student_id=student.id,
            opp_id=opp.id,
            days_before=days_before,
            inkbox_id=message_id,
        ).info("reminder.email_sent")

    def _send_whatsapp_reminder(self, *, student: Student, opp: Opportunity, days_before: int) -> None:
        from evk.twilio_client import TwilioClient
        deadline_str = opp.deadline.date().isoformat() if opp.deadline else "soon"
        first_name = student.name.split()[0] if student.name else "there"
        link_line = f"\n🔗 Apply: {opp.url}" if opp.url else ""
        body = (
            f"👋 Hi {first_name}! Quick reminder from EVkids:\n\n"
            f"*{opp.title}* ({opp.organization})\n"
            f"⏰ Deadline: {deadline_str} — {days_before} day{'s' if days_before != 1 else ''} left"
            f"{link_line}\n\n"
            "If you've already applied, ignore this — good luck!"
        )
        sid = TwilioClient().send_whatsapp(to=student.phone, body=body)
        logger.bind(
            student_id=student.id,
            opp_id=opp.id,
            days_before=days_before,
            twilio_sid=sid,
        ).info("reminder.whatsapp_sent")


def _closest_window(days_left: float, windows: list[int]) -> int | None:
    """Return the smallest window `w` such that days_left <= w and days_left >= w-1."""
    # We fire exactly once per configured window, when days_left is in [w-1, w].
    for w in windows:
        if w - 1 <= days_left <= w:
            return w
    return None


__all__ = ["ReminderAgent"]
