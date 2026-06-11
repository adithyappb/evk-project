"""Web scraper agent — fetches public opportunity pages from configured sources
and feeds the extracted text through the existing ingestion + Gemini pipeline.

Adding a new source: append an entry to SCRAPER_SOURCES with a unique key,
a human-readable name, and the URL of the page that lists opportunities.
The ingestion pipeline will classify the page text with Gemini and extract
structured Opportunity records, placing uncertain ones in the review queue.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser

import httpx

from evk.agents.ingestion import IngestionAgent
from evk.factory import get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.logging import logger

SCRAPER_SOURCES: dict[str, dict[str, str]] = {
    "youthline": {
        "name": "YouthLine NE",
        "url": "https://youthlinene.org/opportunities/",
    },
    "boston_gov": {
        "name": "Boston.gov Youth Employment",
        "url": "https://www.boston.gov/departments/youth-employment-and-opportunity",
    },
}

_HEADERS = {
    "User-Agent": (
        "EVkids-OpportunityBot/1.0 "
        "(non-profit student outreach; contact: admin@evkids.org)"
    )
}


class _TextExtractor(HTMLParser):
    """Minimal HTML → plain-text stripper using the stdlib parser."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "head", "meta", "link"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._depth = 0  # nesting depth of skip-tags

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth == 0:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._parts)


def _strip_html(raw_html: str, max_chars: int = 40_000) -> str:
    extractor = _TextExtractor()
    extractor.feed(raw_html)
    return extractor.get_text()[:max_chars]


def _synthetic_msg_id(source_key: str, url: str, body_sample: str) -> str:
    """Stable ID so the ingestion dedup layer ignores repeat scrapes of unchanged pages."""
    fingerprint = f"{source_key}:{url}:{body_sample[:800]}"
    return "scrape_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:20]


class WebScraperAgent:
    """Fetches opportunity listings from public sources and ingests via Gemini classifier."""

    def __init__(
        self,
        *,
        repos: Repos | None = None,
        inkbox: InkboxClient | None = None,
    ) -> None:
        self._repos = repos or get_repos()
        self._inkbox = inkbox or get_inkbox()

    def scrape(self, source_key: str) -> int:
        """Scrape a named source. Returns number of opportunity IDs queued."""
        source = SCRAPER_SOURCES.get(source_key)
        if source is None:
            raise ValueError(
                f"Unknown scraper source {source_key!r}. "
                f"Valid keys: {list(SCRAPER_SOURCES)}"
            )
        return self._fetch_and_ingest(
            source_key=source_key,
            name=source["name"],
            url=source["url"],
        )

    def scrape_all(self) -> dict[str, int]:
        """Scrape every configured source. Returns {source_key: opp_count}."""
        results: dict[str, int] = {}
        for key in SCRAPER_SOURCES:
            try:
                results[key] = self.scrape(key)
            except Exception:
                logger.bind(source=key).exception("scraper.source_failed")
                results[key] = 0
        return results

    def _fetch_and_ingest(self, *, source_key: str, name: str, url: str) -> int:
        logger.bind(source=source_key, url=url).info("scraper.fetching")
        try:
            resp = httpx.get(url, timeout=20, follow_redirects=True, headers=_HEADERS)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.bind(source=source_key, url=url, error=str(exc)).error("scraper.fetch_failed")
            raise RuntimeError(f"HTTP error fetching {url}: {exc}") from exc

        body_text = _strip_html(resp.text)
        if not body_text.strip():
            logger.bind(source=source_key).warning("scraper.empty_body")
            return 0

        msg_id = _synthetic_msg_id(source_key, url, body_text)
        msg = InboundMessage(
            id=msg_id,
            rfc_message_id=None,
            thread_id=None,
            from_address=f"scraper+{source_key}@evkids.internal",
            subject=f"[scraped] {name}",
            body_text=body_text,
            body_html="",
            raw=None,
        )

        ingestion = IngestionAgent(repos=self._repos, inkbox=self._inkbox)
        raw = ingestion.handle_inbound(msg)
        count = len(raw.extracted_opportunity_ids)
        logger.bind(
            source=source_key,
            status=raw.status.value,
            opportunities=count,
            raw_id=raw.id,
        ).info("scraper.ingested")
        return count


__all__ = ["SCRAPER_SOURCES", "WebScraperAgent"]
