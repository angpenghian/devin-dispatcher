"""Devin dispatcher — one file, two subcommands.

Spawned by GitHub Actions in the target repo. Renders a prompt template,
calls the Devin v3 API, polls until the session terminates, writes a JSON
run report.

Two commands:
  dispatcher dispatch fix-issue  --repo X/Y --issue N
  dispatcher dispatch any        --repo X/Y --task "..." --slug short-name
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click
import httpx
from jinja2 import Environment, FileSystemLoader, StrictUndefined


# ─── env / config ────────────────────────────────────────────────────────────
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY", "")
DEVIN_ORG_ID = os.getenv("DEVIN_ORG_ID", "")
DEVIN_GITHUB_SECRET_NAME = os.getenv("DEVIN_GITHUB_SECRET_NAME") or None
MAX_ACU_LIMIT = float(os.getenv("MAX_ACU_LIMIT", "20"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "30"))
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_MINUTES", "60")) * 60

DEVIN_API = "https://api.devin.ai"
HERE = Path(__file__).parent
TERMINAL_STATUSES = {
    "finished", "inactivity", "usage_limit_exceeded",
    "waiting_for_user", "waiting_for_approval",
}

OUTPUT_SCHEMA = json.loads((HERE / "output_schema.json").read_text())
PROMPTS = Environment(
    loader=FileSystemLoader(str(HERE)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


# ─── Devin API ────────────────────────────────────────────────────────────────
def _devin_headers() -> dict[str, str]:
    if not DEVIN_API_KEY or not DEVIN_ORG_ID:
        raise click.ClickException("DEVIN_API_KEY and DEVIN_ORG_ID must be set")
    return {"Authorization": f"Bearer {DEVIN_API_KEY}", "Content-Type": "application/json"}


def create_session(prompt: str, title: Optional[str] = None) -> dict:
    body: dict[str, Any] = {
        "prompt": prompt,
        "max_acu_limit": MAX_ACU_LIMIT,
        "structured_output_schema": OUTPUT_SCHEMA,
        "bypass_approval": True,
        "devin_mode": "normal",
    }
    if title:
        body["title"] = title
    if DEVIN_GITHUB_SECRET_NAME:
        body["secret_ids"] = [DEVIN_GITHUB_SECRET_NAME]

    r = httpx.post(
        f"{DEVIN_API}/v3/organizations/{DEVIN_ORG_ID}/sessions",
        headers=_devin_headers(), json=body, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_session(session_id: str) -> dict:
    r = httpx.get(
        f"{DEVIN_API}/v3/organizations/{DEVIN_ORG_ID}/sessions/{session_id}",
        headers=_devin_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()


def poll_until_done(session_id: str) -> dict:
    deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
    while True:
        state = get_session(session_id)
        status = state.get("status_detail") or state.get("status", "unknown")
        click.echo(f"  [poll] status={status}", err=True)
        if status in TERMINAL_STATUSES or time.monotonic() >= deadline:
            return state
        time.sleep(POLL_INTERVAL)


# ─── run reports ─────────────────────────────────────────────────────────────
def write_report(task_type: str, payload: dict, repo: str, state: dict,
                 reports_dir: Path, started_at: datetime) -> dict:
    structured = state.get("structured_output") or {}
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except json.JSONDecodeError:
            structured = {}
    finished_at = datetime.now(timezone.utc)
    report = {
        "task_id": uuid.uuid4().hex[:12],
        "task_type": task_type,
        "payload": payload,
        "repo": repo,
        "devin_session_id": state.get("session_id"),
        "devin_session_url": state.get("url"),
        "final_status": state.get("status_detail") or state.get("status"),
        "outcome": structured.get("outcome") if isinstance(structured, dict) else None,
        "pr_url": structured.get("pr_url") if isinstance(structured, dict) else None,
        "summary": structured.get("summary") if isinstance(structured, dict) else None,
        "acus_consumed": state.get("acus_consumed"),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = re.sub(r"[^a-zA-Z0-9_-]", "-", f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{task_type}-{report['task_id']}")
    path = reports_dir / f"{base}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    click.echo(f"[report] wrote {path}", err=True)
    return report


def run_devin(task_type: str, payload: dict, repo: str, title: str,
              prompt: str, reports_dir: Path) -> dict:
    started_at = datetime.now(timezone.utc)
    click.echo(f"[devin] creating session: {title}", err=True)
    created = create_session(prompt, title=title)
    sid = created["session_id"]
    click.echo(f"[devin] session_id={sid} url={created.get('url')}", err=True)
    final = poll_until_done(sid)
    report = write_report(task_type, payload, repo, final, reports_dir, started_at)
    click.echo(f"[devin] outcome={report['outcome']} pr_url={report['pr_url']}", err=True)
    return report


def _render_prompt(repo: str, branch: str, task_slug: str, task_description: str) -> str:
    """Render the unified prompt with universal guardrails + task-specific body."""
    return PROMPTS.get_template("prompt.j2").render(
        repo=repo, branch=branch, task_slug=task_slug, task_description=task_description,
    )


# ─── CLI ─────────────────────────────────────────────────────────────────────
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option("0.3.0")
def cli() -> None:
    """Devin dispatcher — fire Devin sessions from CI events."""


@cli.group()
def dispatch() -> None:
    """Dispatch a single Devin session for a specific task."""


@dispatch.command("fix-issue")
@click.option("--repo", required=True, help="owner/repo of the target GitHub repo")
@click.option("--branch", default="master", show_default=True)
@click.option("--issue", "issue_number", required=True, type=int)
@click.option("--reports-dir", default="reports", show_default=True,
              type=click.Path(path_type=Path))
def fix_issue_cmd(repo: str, branch: str, issue_number: int, reports_dir: Path) -> None:
    """Spawn a Devin session to read, diagnose, and fix a GitHub issue."""
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    task_description = (
        f"Read GitHub issue #{issue_number} at {issue_url}.\n"
        f"Reproduce the problem if relevant, diagnose root cause, and implement the minimum-scoped fix.\n"
        f"Open a PR titled \"fix: <short description> (closes #{issue_number})\" against {branch}, "
        f"with a body that references the issue and lists the verification commands you ran."
    )
    run_devin(
        task_type="fix-issue",
        payload={"issue_number": issue_number, "issue_url": issue_url, "branch": branch},
        repo=repo,
        title=f"fix-issue#{issue_number} in {repo}",
        prompt=_render_prompt(repo, branch, f"fix-issue-{issue_number}", task_description),
        reports_dir=reports_dir,
    )


@dispatch.command("any")
@click.option("--repo", required=True)
@click.option("--branch", default="master", show_default=True)
@click.option("--task", "task_description", required=True,
              help="Free-form description of the task you want Devin to do.")
@click.option("--slug", "task_slug", required=True,
              help="Short kebab-case slug used in the branch name (devin/<slug>).")
@click.option("--reports-dir", default="reports", show_default=True,
              type=click.Path(path_type=Path))
def any_cmd(repo: str, branch: str, task_description: str, task_slug: str,
            reports_dir: Path) -> None:
    """Spawn a Devin session for an arbitrary task. Inherits all universal guardrails."""
    run_devin(
        task_type="any",
        payload={"task_description": task_description, "task_slug": task_slug, "branch": branch},
        repo=repo,
        title=task_slug,
        prompt=_render_prompt(repo, branch, task_slug, task_description),
        reports_dir=reports_dir,
    )


if __name__ == "__main__":
    cli()
