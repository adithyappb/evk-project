"""EVK CLI — the single entrypoint for humans and cron.

Commands:
    evk serve         Run the FastAPI app (webhooks + admin).
    evk seed          Upload seed students & opportunities to Firestore.
    evk poll          One-shot: pull unread Inkbox messages and run pipeline.
    evk remind        One-shot: send due deadline reminders.
    evk scheduler     Long-running scheduler (polling + reminders).
    evk simulate      Run the full pipeline on a local .txt/.eml file (no Inkbox).
    evk drafts        List pending drafts.
    evk approve <id>  Approve and send a draft by id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from evk.agents.digest import DigestAgent
from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.reminder import ReminderAgent
from evk.config import get_settings
from evk.factory import describe_wiring, get_repos
from evk.inkbox_client import InboundMessage
from evk.logging import configure_logging, logger
from evk.models import DraftStatus
from evk.seed import seed_all

app = typer.Typer(help="EVK: opportunity ingestion & distribution pipeline.", no_args_is_help=True)
console = Console()


@app.callback()
def _init() -> None:
    configure_logging()


@app.command()
def info() -> None:
    """Show current mode and which backends (real vs stub) are wired up."""
    wiring = describe_wiring()
    table = Table(title="EVK wiring")
    table.add_column("component", style="cyan", no_wrap=True)
    table.add_column("backend", style="green")
    for k, v in wiring.items():
        table.add_row(k, str(v))
    console.print(table)


# --------------------------------------------------------------------------- #
# serve                                                                       #
# --------------------------------------------------------------------------- #


@app.command()
def serve(
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
) -> None:
    """Run the FastAPI server (webhooks + admin REST + dashboard UI)."""
    settings = get_settings()
    bind_host = host or settings.app_host
    bind_port = port or settings.app_port
    public_host = "localhost" if bind_host in {"0.0.0.0", "::"} else bind_host
    # Loguru intercepts stdlib logging, which hides uvicorn's "running on" line.
    # Print an ASCII-only banner up front — stays readable even on cp1252 pipes.
    bar = "=" * 60
    print(flush=True)
    print(bar, flush=True)
    print(f"  EVK serving  |  {describe_wiring().get('mode', 'local')} mode", flush=True)
    print(f"  Dashboard   http://{public_host}:{bind_port}/", flush=True)
    print(f"  Health      http://{public_host}:{bind_port}/healthz", flush=True)
    print(f"  Webhook     http://{public_host}:{bind_port}/webhooks/inkbox", flush=True)
    print("  (Ctrl+C to stop)", flush=True)
    print(bar, flush=True)
    print(flush=True)
    uvicorn.run(
        "evk.api:app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level=settings.app_log_level.lower(),
    )


# --------------------------------------------------------------------------- #
# seed                                                                        #
# --------------------------------------------------------------------------- #


@app.command()
def seed() -> None:
    """Upload demo students + seed opportunities to Firestore."""
    counts = seed_all()
    console.print(
        f"[green]seeded[/green] students={counts['students']} opportunities={counts['opportunities']}"
    )


# --------------------------------------------------------------------------- #
# poll + remind                                                               #
# --------------------------------------------------------------------------- #


@app.command()
def poll() -> None:
    """Pull unread Inkbox messages and run the full ingestion pipeline once."""
    ingestion = IngestionAgent()
    processed, pending = ingestion.poll_unread()
    console.print(
        f"[green]processed[/green] {len(processed)} messages, "
        f"[yellow]{len(pending)}[/yellow] drafts pending approval."
    )


@app.command()
def remind() -> None:
    """Send all due deadline reminders."""
    agent = ReminderAgent()
    n = agent.run()
    console.print(f"[green]sent[/green] {n} reminders.")


@app.command()
def digest(
    top_n: int = 5,
    min_score: float = 0.5,
) -> None:
    """Build this week's digest drafts (one per opted-in student) — pending approval."""
    agent = DigestAgent(top_n=top_n, min_score=min_score)
    drafts_created = agent.build_and_queue()
    console.print(
        f"[green]queued[/green] {len(drafts_created)} digest draft(s) — awaiting approval."
    )


# --------------------------------------------------------------------------- #
# scheduler (long-running)                                                    #
# --------------------------------------------------------------------------- #


@app.command()
def scheduler(
    poll_minutes: int = 5,
    remind_hours: int = 6,
) -> None:
    """Run polling + reminders on an in-process schedule. Use webhooks in prod instead."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    ingestion = IngestionAgent()
    reminder_agent = ReminderAgent()
    sched = BlockingScheduler(timezone="UTC")

    def _poll() -> None:
        try:
            processed, pending = ingestion.poll_unread()
            logger.bind(processed=len(processed), pending=len(pending)).info("scheduler.poll")
        except Exception:
            logger.exception("scheduler.poll_failed")

    def _remind() -> None:
        try:
            sent = reminder_agent.run()
            logger.bind(sent=sent).info("scheduler.remind")
        except Exception:
            logger.exception("scheduler.remind_failed")

    sched.add_job(_poll, "interval", minutes=poll_minutes, next_run_time=datetime.now(UTC))
    sched.add_job(_remind, "interval", hours=remind_hours)
    console.print(
        f"[green]scheduler running[/green] (poll every {poll_minutes}m, remind every {remind_hours}h)"
    )
    sched.start()


# --------------------------------------------------------------------------- #
# simulate                                                                    #
# --------------------------------------------------------------------------- #


@app.command()
def simulate(
    path: Annotated[
        Path, typer.Argument(help="Path to a .txt / .eml file containing the email body.")
    ],
    sender: str = "newsletter@example.com",
    subject: str | None = None,
) -> None:
    """Run the full ingestion pipeline on a local file without Inkbox."""
    body = path.read_text(encoding="utf-8", errors="ignore")
    effective_subject = subject or f"[sim] {path.name}"
    fake = InboundMessage(
        id=f"sim_{path.stem}_{int(datetime.now(UTC).timestamp())}",
        rfc_message_id=None,
        thread_id=None,
        from_address=sender,
        subject=effective_subject,
        body_text=body,
        body_html="",
        raw=None,
    )
    ingestion = IngestionAgent()
    raw = ingestion.handle_inbound(fake)
    console.print(
        f"[green]processed[/green] raw_email_id={raw.id} status={raw.status.value} "
        f"opportunities={len(raw.extracted_opportunity_ids)}"
    )


# --------------------------------------------------------------------------- #
# drafts / approve                                                            #
# --------------------------------------------------------------------------- #


@app.command()
def drafts(
    status_filter: str = typer.Option(
        "pending_approval", "--status", help="Filter by draft status."
    ),
    limit: int = 50,
) -> None:
    """List drafts in Firestore."""
    repos = get_repos()
    items = repos.drafts.list_by_status(DraftStatus(status_filter), limit=limit)
    table = Table(title=f"Drafts ({status_filter})")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("to")
    table.add_column("subject")
    table.add_column("score", justify="right")
    table.add_column("status")
    for d in items:
        table.add_row(d.id, d.to_email, d.subject[:60], f"{d.match_score:.2f}", d.status.value)
    console.print(table)


@app.command()
def approve(
    draft_id: Annotated[str, typer.Argument()],
    approver: str = "cli",
    send: bool = True,
) -> None:
    """Approve a draft by id and (optionally) send it immediately."""
    repos = get_repos()
    draft = repos.drafts.get(draft_id)
    if draft is None:
        console.print(f"[red]not found:[/red] {draft_id}")
        raise typer.Exit(code=1)
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.APPROVED.value,
            "approved_by": approver,
            "approved_at": datetime.now(UTC),
        },
    )
    if send:
        draft = repos.drafts.get(draft_id)
        assert draft is not None
        DistributorAgent(repos=repos).send_one(draft)
        console.print(f"[green]sent[/green] {draft_id}")
    else:
        console.print(f"[green]approved[/green] {draft_id} (not sent)")


if __name__ == "__main__":
    app()
