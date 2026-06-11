"""Ingestion agent.

Turns an Inkbox inbound message into a persisted `RawEmail` and kicks off
classification → personalization. Idempotent on Inkbox message id.
"""

from __future__ import annotations

from evk.agents.classifier import ClassifierAgent, to_opportunity
from evk.agents.personalizer import PersonalizerAgent
from evk.config import get_settings
from evk.dedup import find_duplicate
from evk.email_sanitizer import strip_footers
from evk.factory import get_gemini, get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.logging import logger
from evk.models import (
    DraftMessage,
    DraftStatus,
    Opportunity,
    RawEmail,
    RawEmailStatus,
)


class IngestionAgent:
    """Ingest → classify → persist opportunities → draft personalized messages."""

    def __init__(
        self,
        *,
        repos: Repos | None = None,
        inkbox: InkboxClient | None = None,
        classifier: ClassifierAgent | None = None,
        personalizer: PersonalizerAgent | None = None,
    ) -> None:
        self._repos = repos or get_repos()
        self._inkbox = inkbox or get_inkbox()
        self._classifier = classifier or ClassifierAgent()
        self._personalizer = personalizer or PersonalizerAgent(repos=self._repos)

    # --- public ------------------------------------------------------------

    def handle_inbound(self, msg: InboundMessage) -> RawEmail:
        """Full pipeline for a single inbound message. Idempotent."""
        raw, created = self._persist_raw(msg)
        if not created:
            logger.bind(raw_email_id=raw.id).info("ingestion.already_seen")
            return raw

        try:
            result = self._classifier.classify(raw)
        except Exception as exc:
            logger.bind(raw_email_id=raw.id).exception("ingestion.classify_failed")
            self._repos.raw_emails.mark_status(raw.id, RawEmailStatus.FAILED, error=str(exc)[:500])
            raw.status = RawEmailStatus.FAILED
            raw.classification_error = str(exc)[:500]
            return raw

        # Publish-through threshold: low-confidence classifications are shelved
        # to keep noisy / spammy newsletters from polluting the catalog.
        min_conf = get_settings().classifier_min_confidence
        if not result.is_opportunity or not result.opportunities or result.confidence < min_conf:
            self._repos.raw_emails.mark_status(
                raw.id, RawEmailStatus.SKIPPED, extracted_opportunity_ids=[]
            )
            raw.status = RawEmailStatus.SKIPPED
            logger.bind(
                raw_email_id=raw.id,
                reasoning=result.reasoning,
                confidence=result.confidence,
                threshold=min_conf,
            ).info("ingestion.not_opportunity")
            return raw

        persisted_opps = self._persist_opportunities(result.opportunities, source=raw)
        self._repos.raw_emails.mark_status(
            raw.id,
            RawEmailStatus.CLASSIFIED,
            extracted_opportunity_ids=[o.id for o in persisted_opps],
        )
        raw.status = RawEmailStatus.CLASSIFIED
        raw.extracted_opportunity_ids = [o.id for o in persisted_opps]

        drafts = self._personalizer.draft_for_opportunities(persisted_opps)
        logger.bind(
            raw_email_id=raw.id,
            opps=len(persisted_opps),
            drafts=len(drafts),
        ).info("ingestion.done")
        return raw

    def handle_many(self, msgs: list[InboundMessage]) -> list[RawEmail]:
        return [self.handle_inbound(m) for m in msgs]

    def poll_unread(self) -> tuple[list[RawEmail], list[DraftMessage]]:
        """Polling fallback when webhooks aren't configured. Marks messages read."""
        processed: list[RawEmail] = []
        to_mark: list[str] = []
        for msg in self._inkbox.iter_unread_inbound():
            processed.append(self.handle_inbound(msg))
            to_mark.append(msg.id)
        self._inkbox.mark_read(to_mark)
        drafts_pending = self._repos.drafts.list_by_status(DraftStatus.PENDING_APPROVAL)
        return processed, drafts_pending

    # --- internals ---------------------------------------------------------

    def _persist_raw(self, msg: InboundMessage) -> tuple[RawEmail, bool]:
        # Strip marketing footers / unsubscribe blocks before classification.
        # Keeps prompts short and the classifier focused on real content.
        cleaned_text = strip_footers(msg.body_text or "")
        raw = RawEmail(
            id=msg.id,
            inkbox_message_id=msg.id,
            rfc_message_id=msg.rfc_message_id,
            thread_id=msg.thread_id,
            from_address=msg.from_address,
            subject=msg.subject,
            body_text=cleaned_text,
            body_html=msg.body_html,
        )
        return self._repos.raw_emails.create_if_absent(raw)

    def _persist_opportunities(
        self, extracted_list: list, *, source: RawEmail
    ) -> list[Opportunity]:
        """Persist extracted opportunities with fuzzy dedup against existing ones.

        * ``create_if_absent`` handles exact-ID collisions (same title+deadline).
        * ``find_duplicate`` handles fuzzy collisions (e.g. "GSoC 2026" vs
          "Google Summer of Code 2026") within a 30-day deadline window.
        """
        # Load once outside the loop — this is N reads, not N².
        existing_catalog = self._repos.opportunities.list_all()
        # Check whether the Gemini client supports embeddings (not a stub).
        gemini = get_gemini()
        _has_embeddings = hasattr(gemini, "generate_embedding")
        persisted: list[Opportunity] = []
        for extracted in extracted_list:
            opp = to_opportunity(extracted, source_raw_email=source)
            # Compute embedding before storing when Gemini is available.
            if _has_embeddings:
                try:
                    embedding_text = f"{opp.title} {opp.summary} {opp.organization}"
                    opp.embedding = gemini.generate_embedding(embedding_text)
                except Exception:
                    logger.exception("ingestion.embedding_failed")
            stored, created = self._repos.opportunities.create_if_absent(opp)
            if created:
                dup = find_duplicate(opp, existing_catalog)
                if dup is not None:
                    logger.bind(
                        duplicate_of=dup.existing.id,
                        new_id=opp.id,
                        similarity=round(dup.similarity, 3),
                    ).info("ingestion.fuzzy_duplicate_detected")
                    # Keep the earlier record; delete the one we just wrote.
                    self._repos.opportunities.delete(opp.id)
                    persisted.append(dup.existing)
                    continue
                existing_catalog.append(stored)
            persisted.append(stored)
        return persisted


__all__ = ["IngestionAgent"]
