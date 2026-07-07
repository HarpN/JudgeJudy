from __future__ import annotations

import html
import json
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .database import (
    append_audit_log,
    get_audit_log_entry,
    get_backlog_entity,
    get_db_path,
    init_db,
    insert_review_action,
    list_audit_logs,
    list_backlog,
    list_review_actions,
    update_backlog_entity,
)
from .governance import evaluate_proposal

USERS = {
    "DOOM": {"passcode": os.getenv("JUDY_DOOM_CODE", "doom"), "actor_type": "AI", "display_name": "DOOM"},
    "Nate": {"passcode": os.getenv("JUDY_NATE_CODE", "nate"), "actor_type": "HUMAN", "display_name": "Nate"},
}
SESSION_SECRET = os.getenv("JUDY_SESSION_SECRET", "judy-demo-session-secret")
VALID_REVIEW_VERDICTS = ("APPROVED", "REJECTED", "PENDING_REVIEW")


class TransactionMetadata(BaseModel):
    agent_id: str = Field(..., min_length=1)
    timestamp: str = Field(..., min_length=1)
    correlation_id: str = Field(..., min_length=1)


class ProposedAction(BaseModel):
    target_table: str = Field(..., min_length=1)
    action_type: str = Field(..., min_length=1)
    entity_id: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProposalRequest(BaseModel):
    transaction_metadata: TransactionMetadata
    proposed_action: ProposedAction
    agent_rationale: str = Field(default="")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Judy Council", version="1.1.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)
init_db()


def _current_username(request: Request) -> str | None:
    username = request.session.get("user")
    if username in USERS:
        return username
    return None


def _current_user(request: Request) -> dict[str, str] | None:
    username = _current_username(request)
    if username is None:
        return None
    user = USERS[username]
    return {"username": username, **user}


def _require_user(request: Request) -> dict[str, str]:
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def _flash_url(message: str) -> str:
    return f"/review?message={quote_plus(message)}"


def _login_page(error: str | None = None) -> str:
    error_html = f'<div class="alert error">{html.escape(error)}</div>' if error else ""
    options_html = "".join(
        f'<option value="{html.escape(username, quote=True)}">{html.escape(profile["display_name"])} · {profile["actor_type"]}</option>'
        for username, profile in USERS.items()
    )
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Judy Council Login</title>
        <style>
            :root {{ color-scheme: dark; }}
            body {{ margin: 0; font-family: Inter, Segoe UI, Arial, sans-serif; min-height: 100vh; display: grid; place-items: center; background: radial-gradient(circle at top, #17253e 0%, #0b111d 55%, #060a12 100%); color: #e6edf7; }}
            .panel {{ width: min(560px, calc(100vw - 32px)); background: rgba(11, 17, 29, 0.88); border: 1px solid rgba(144, 164, 196, 0.16); border-radius: 24px; padding: 32px; box-shadow: 0 24px 80px rgba(0,0,0,0.45); }}
            .eyebrow {{ text-transform: uppercase; letter-spacing: .15em; color: #8fa4c7; font-size: .75rem; margin-bottom: 12px; }}
            h1 {{ margin: 0 0 10px; font-size: clamp(2rem, 4vw, 3rem); }}
            p {{ color: #b7c3d7; line-height: 1.6; }}
            form {{ display: grid; gap: 14px; margin-top: 24px; }}
            label {{ display: grid; gap: 8px; font-size: .9rem; color: #cdd7e6; }}
            select, input {{ background: rgba(255,255,255,0.04); color: #e6edf7; border: 1px solid rgba(144, 164, 196, 0.22); border-radius: 14px; padding: 12px 14px; font: inherit; }}
            button {{ border: 0; border-radius: 14px; padding: 12px 16px; background: linear-gradient(135deg, #4f7cff, #69d2ff); color: #06111f; font-weight: 800; cursor: pointer; }}
            .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }}
            .pill {{ border-radius: 999px; padding: 6px 10px; background: rgba(255,255,255,0.06); color: #cdd7e6; font-size: .82rem; }}
            .alert {{ border-radius: 14px; padding: 12px 14px; margin-top: 16px; }}
            .error {{ background: rgba(255, 96, 96, 0.16); color: #ffb3b3; }}
            .hint {{ font-size: .85rem; color: #8fa4c7; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <main class="panel">
            <div class="eyebrow">Judy Council Access Gate</div>
            <h1>Sign in to review judgments.</h1>
            <p>Choose a reviewer profile to open the console. DOOM is treated as an AI auditor, so any DOOM override is judged by the same council matrix before it is recorded.</p>
            {error_html}
            <form method="post" action="/login">
                <label>
                    User
                    <select name="username" required>
                        {options_html}
                    </select>
                </label>
                <label>
                    Access code
                    <input name="passcode" type="password" placeholder="Enter the demo code" required />
                </label>
                <button type="submit">Enter review console</button>
            </form>
            <div class="meta">
                <span class="pill">Users: DOOM, Nate</span>
                <span class="pill">Session gated</span>
                <span class="pill">Annotation + Override</span>
            </div>
            <div class="hint">Demo codes default to <strong>doom</strong> and <strong>nate</strong> unless you override them with environment variables.</div>
        </main>
    </body>
    </html>
    """


def _render_vote_pills(votes: list[dict[str, Any]]) -> str:
    return "".join(
        f'<span class="vote-pill {html.escape(vote["status"].lower(), quote=True)}">{html.escape(vote["judge_id"])}: {html.escape(vote["status"])} </span>'
        for vote in votes
    ) or '<span class="muted">No votes stored</span>'


def _render_decision_card(entry: dict[str, Any]) -> str:
    payload_text = html.escape(json.dumps(entry.get("payload", {}), indent=2, ensure_ascii=False), quote=False)
    votes_html = _render_vote_pills(entry.get("votes", []))
    return f"""
    <article class="card">
        <div class="card-head">
            <div>
                <div class="eyebrow">{html.escape(entry['created_at'])}</div>
                <h3>{html.escape(entry['action_type'])} · {html.escape(entry['target_table'])}</h3>
            </div>
            <span class="verdict verdict-{html.escape(entry['verdict'].lower(), quote=True)}">{html.escape(entry['verdict'])}</span>
        </div>
        <p><strong>Council:</strong> {html.escape(entry['council_id'])} · <strong>Agent:</strong> {html.escape(entry['agent_id'])}</p>
        <p><strong>Correlation:</strong> {html.escape(entry['correlation_id'])}</p>
        <p><strong>Rationale:</strong> {html.escape(entry['rationale'])}</p>
        <p><strong>System action:</strong> {html.escape(entry['system_action'])} · <strong>Human review:</strong> {str(entry['human_review_required']).lower()}</p>
        <div class="votes">{votes_html}</div>
        <div class="action-grid">
            <form method="post" action="/review/annotate" class="action-form">
                <input type="hidden" name="council_id" value="{html.escape(entry['council_id'], quote=True)}" />
                <input type="hidden" name="target_verdict" value="{html.escape(entry['verdict'], quote=True)}" />
                <label>
                    Annotation
                    <textarea name="note" maxlength="300" placeholder="Add a note for this decision"></textarea>
                </label>
                <button type="submit" class="secondary">Save annotation</button>
            </form>
            <form method="post" action="/review/override" class="action-form">
                <input type="hidden" name="council_id" value="{html.escape(entry['council_id'], quote=True)}" />
                <label>
                    Requested verdict
                    <select name="requested_verdict">
                        {''.join(f'<option value="{verdict}">{verdict}</option>' for verdict in VALID_REVIEW_VERDICTS)}
                    </select>
                </label>
                <label>
                    Override note
                    <textarea name="note" maxlength="300" placeholder="Explain why this should be overridden"></textarea>
                </label>
                <button type="submit">Propose override</button>
            </form>
        </div>
        <details>
            <summary>Payload</summary>
            <pre>{payload_text}</pre>
        </details>
    </article>
    """


def _render_review_action_card(action: dict[str, Any]) -> str:
    payload_text = html.escape(json.dumps(action.get("payload", {}), indent=2, ensure_ascii=False), quote=False)
    votes_html = _render_vote_pills(action.get("votes", []))
    return f"""
    <article class="card action-card">
        <div class="card-head">
            <div>
                <div class="eyebrow">{html.escape(action['created_at'])}</div>
                <h3>{html.escape(action['action_kind'])} · {html.escape(action['actor_name'])}</h3>
            </div>
            <span class="verdict verdict-{html.escape(action['verdict'].lower(), quote=True)}">{html.escape(action['verdict'])}</span>
        </div>
        <p><strong>Actor:</strong> {html.escape(action['actor_name'])} · <strong>Type:</strong> {html.escape(action['actor_type'])}</p>
        <p><strong>Target council:</strong> {html.escape(action['target_council_id'])}</p>
        <p><strong>Requested verdict:</strong> {html.escape(action['requested_verdict'] or 'n/a')}</p>
        <p><strong>Review council:</strong> {html.escape(action['council_id'])} · <strong>System action:</strong> {html.escape(action['system_action'])}</p>
        <p><strong>Note:</strong> {html.escape(action['note'])}</p>
        <div class="votes">{votes_html}</div>
        <details>
            <summary>Payload</summary>
            <pre>{payload_text}</pre>
        </details>
    </article>
    """


def _render_review_page(user: dict[str, str], limit: int = 25, verdict: str = "all", message: str = "") -> str:
    logs = list_audit_logs(limit=limit)
    filtered_logs = [entry for entry in logs if verdict == "all" or entry["verdict"] == verdict]
    review_actions = list_review_actions(limit=limit)

    verdict_counts: dict[str, int] = {"APPROVED": 0, "REJECTED": 0, "PENDING_REVIEW": 0}
    for entry in logs:
        verdict_counts[entry["verdict"]] = verdict_counts.get(entry["verdict"], 0) + 1

    decision_cards = "".join(_render_decision_card(entry) for entry in filtered_logs) or '<p class="empty">No audit entries match the selected filter.</p>'
    action_cards = "".join(_render_review_action_card(action) for action in review_actions) or '<p class="empty">No annotations or overrides have been recorded yet.</p>'
    message_html = f'<div class="alert success">{html.escape(message)}</div>' if message else ""
    user_badge = f"{html.escape(user['display_name'])} · {html.escape(user['actor_type'])}"

    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Judy Council Review</title>
        <style>
            :root {{ color-scheme: dark; }}
            body {{ margin: 0; font-family: Inter, Segoe UI, Arial, sans-serif; background: linear-gradient(180deg, #09111f 0%, #101a2c 100%); color: #e6edf7; }}
            .shell {{ max-width: 1280px; margin: 0 auto; padding: 32px 20px 48px; }}
            .topbar {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
            .userbox {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
            .user-pill, .logout, .chip {{ border-radius: 999px; padding: 10px 14px; border: 1px solid rgba(144, 164, 196, 0.16); background: rgba(255,255,255,0.03); color: #e6edf7; text-decoration: none; }}
            .logout {{ background: rgba(255,255,255,0.02); }}
            .hero {{ display: grid; gap: 12px; margin-bottom: 24px; }}
            .hero h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.4rem); }}
            .hero p {{ margin: 0; color: #b7c3d7; max-width: 80ch; }}
            .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 24px 0; }}
            .stat, .card {{ background: rgba(13, 21, 37, 0.82); border: 1px solid rgba(144, 164, 196, 0.16); box-shadow: 0 16px 40px rgba(0,0,0,0.25); border-radius: 18px; }}
            .stat {{ padding: 16px; }}
            .stat strong {{ display: block; font-size: 2rem; margin-top: 8px; }}
            .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0 24px; }}
            .chip.active {{ background: #4f7cff; border-color: #4f7cff; }}
            .grid {{ display: grid; gap: 16px; }}
            .card {{ padding: 18px; }}
            .card-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
            .eyebrow {{ text-transform: uppercase; letter-spacing: .12em; font-size: .72rem; color: #8fa4c7; margin-bottom: 6px; }}
            .card h3 {{ margin: 0; font-size: 1.1rem; }}
            .verdict {{ border-radius: 999px; padding: 6px 10px; font-size: .75rem; font-weight: 700; }}
            .verdict-approved {{ background: rgba(58, 183, 117, .18); color: #7bed9f; }}
            .verdict-rejected {{ background: rgba(255, 96, 96, .18); color: #ff8b8b; }}
            .verdict-pending_review {{ background: rgba(255, 197, 71, .16); color: #ffd98a; }}
            .vote-pill {{ display: inline-block; margin: 0 8px 8px 0; padding: 6px 10px; border-radius: 999px; font-size: .78rem; background: rgba(255,255,255,0.06); }}
            .vote-pill.approved {{ color: #7bed9f; }}
            .vote-pill.rejected {{ color: #ff8b8b; }}
            .vote-pill.pending_review {{ color: #ffd98a; }}
            .action-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin: 18px 0 12px; }}
            .action-form {{ display: grid; gap: 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(144, 164, 196, 0.12); border-radius: 16px; padding: 14px; }}
            label {{ display: grid; gap: 8px; color: #cdd7e6; font-size: .92rem; }}
            textarea, select {{ width: 100%; background: rgba(255,255,255,0.04); color: #e6edf7; border: 1px solid rgba(144, 164, 196, 0.22); border-radius: 12px; padding: 10px 12px; font: inherit; box-sizing: border-box; }}
            textarea {{ min-height: 88px; resize: vertical; }}
            button {{ border: 0; border-radius: 12px; padding: 11px 14px; background: linear-gradient(135deg, #4f7cff, #69d2ff); color: #06111f; font-weight: 800; cursor: pointer; }}
            button.secondary {{ background: rgba(255,255,255,0.08); color: #e6edf7; border: 1px solid rgba(144, 164, 196, 0.18); }}
            details {{ margin-top: 12px; }}
            summary {{ cursor: pointer; color: #9fb2cf; }}
            pre {{ white-space: pre-wrap; word-break: break-word; background: rgba(0,0,0,0.25); padding: 14px; border-radius: 12px; overflow: auto; }}
            .section-title {{ margin: 32px 0 14px; font-size: 1.35rem; }}
            .empty {{ color: #b7c3d7; }}
            .muted {{ color: #8fa4c7; }}
            .alert {{ border-radius: 14px; padding: 12px 14px; margin-bottom: 18px; }}
            .success {{ background: rgba(58, 183, 117, 0.16); color: #b9f7cf; }}
            @media (max-width: 720px) {{ .card-head {{ flex-direction: column; }} }}
        </style>
    </head>
    <body>
        <main class="shell">
            <div class="topbar">
                <div class="userbox">
                    <span class="user-pill">Signed in as {user_badge}</span>
                    <span class="user-pill">Review gate active</span>
                </div>
                <a class="logout" href="/logout">Logout</a>
            </div>

            <section class="hero">
                <div class="eyebrow">Judy Council Review Console</div>
                <h1>Review judgements, judges, and council decisions.</h1>
                <p>This console shows every recorded proposal judgment, the verdict issued by the council, the judge votes that produced it, and the payload that was reviewed. Both Nate and DOOM can annotate and propose overrides. DOOM's override proposals are judged by the same council matrix before they are recorded.</p>
            </section>

            {message_html}

            <section class="stats">
                <div class="stat"><span>Total Records</span><strong>{len(logs)}</strong></div>
                <div class="stat"><span>Approved</span><strong>{verdict_counts.get('APPROVED', 0)}</strong></div>
                <div class="stat"><span>Rejected</span><strong>{verdict_counts.get('REJECTED', 0)}</strong></div>
                <div class="stat"><span>Pending Review</span><strong>{verdict_counts.get('PENDING_REVIEW', 0)}</strong></div>
            </section>

            <nav class="toolbar">
                <a class="chip {'active' if verdict == 'all' else ''}" href="/review?verdict=all">All</a>
                <a class="chip {'active' if verdict == 'APPROVED' else ''}" href="/review?verdict=APPROVED">Approved</a>
                <a class="chip {'active' if verdict == 'REJECTED' else ''}" href="/review?verdict=REJECTED">Rejected</a>
                <a class="chip {'active' if verdict == 'PENDING_REVIEW' else ''}" href="/review?verdict=PENDING_REVIEW">Pending Review</a>
            </nav>

            <h2 class="section-title">Council decisions</h2>
            <section class="grid">
                {decision_cards}
            </section>

            <h2 class="section-title">Annotations and overrides</h2>
            <section class="grid">
                {action_cards}
            </section>
        </main>
    </body>
    </html>
    """


def _build_review_proposal(*, user: dict[str, str], council_id: str, action_kind: str, requested_verdict: str, note: str) -> ProposalRequest:
    return ProposalRequest(
        transaction_metadata=TransactionMetadata(
            agent_id=user["username"],
            timestamp="2026-07-06T21:54:13Z",
            correlation_id=f"review-{uuid4().hex[:10]}",
        ),
        proposed_action=ProposedAction(
            target_table="review_actions",
            action_type=action_kind,
            entity_id=council_id,
            payload={
                "actor_name": user["username"],
                "actor_type": user["actor_type"],
                "target_council_id": council_id,
                "requested_verdict": requested_verdict,
                "note": note,
            },
        ),
        agent_rationale=note,
    )


def _process_review_action(*, request: Request, council_id: str, action_kind: str, requested_verdict: str, note: str) -> RedirectResponse:
    user = _require_user(request)
    target_entry = get_audit_log_entry(council_id)
    if target_entry is None:
        raise HTTPException(status_code=404, detail="Council decision not found")

    proposal = _build_review_proposal(
        user=user,
        council_id=council_id,
        action_kind=action_kind,
        requested_verdict=requested_verdict,
        note=note,
    )
    decision = evaluate_proposal(target_entry, proposal.model_dump())
    append_audit_log(
        correlation_id=proposal.transaction_metadata.correlation_id,
        agent_id=proposal.transaction_metadata.agent_id,
        council_id=decision.council_id,
        target_table=proposal.proposed_action.target_table,
        action_type=proposal.proposed_action.action_type,
        verdict=decision.final_verdict,
        rationale=decision.final_rationale,
        system_action=decision.system_action,
        human_review_required=decision.human_review_required,
        votes=decision.to_dict()["votes"],
        payload=proposal.model_dump(),
    )
    insert_review_action(
        actor_name=user["username"],
        actor_type=user["actor_type"],
        action_kind=action_kind,
        target_council_id=council_id,
        requested_verdict=requested_verdict,
        note=note,
        council_id=decision.council_id,
        verdict=decision.final_verdict,
        rationale=decision.final_rationale,
        system_action=decision.system_action,
        human_review_required=decision.human_review_required,
        votes=decision.to_dict()["votes"],
        payload=proposal.model_dump(),
    )
    return RedirectResponse(url=_flash_url(f"{action_kind} recorded for {council_id} ({decision.final_verdict})"), status_code=303)


def _record_decision(request: ProposalRequest, decision) -> None:
    append_audit_log(
        correlation_id=request.transaction_metadata.correlation_id,
        agent_id=request.transaction_metadata.agent_id,
        council_id=decision.council_id,
        target_table=request.proposed_action.target_table,
        action_type=request.proposed_action.action_type,
        verdict=decision.final_verdict,
        rationale=decision.final_rationale,
        system_action=decision.system_action,
        human_review_required=decision.human_review_required,
        votes=decision.to_dict()["votes"],
        payload=request.model_dump(),
    )


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/review", status_code=307)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Any:
    if _current_user(request) is not None:
        return RedirectResponse(url="/review", status_code=303)
    return HTMLResponse(_login_page())


@app.post("/login")
def login(request: Request, username: str = Form(...), passcode: str = Form(...)) -> Any:
    profile = USERS.get(username)
    if profile is None or passcode != profile["passcode"]:
        return HTMLResponse(_login_page("Invalid username or access code."), status_code=401)

    request.session["user"] = username
    request.session["actor_type"] = profile["actor_type"]
    return RedirectResponse(url="/review", status_code=303)


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, verdict: str = "all", limit: int = 25, message: str = "") -> Any:
    user = _current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(_render_review_page(user, limit=limit, verdict=verdict, message=message))


@app.post("/review/annotate")
def annotate_review(request: Request, council_id: str = Form(...), note: str = Form(...)) -> RedirectResponse:
    return _process_review_action(request=request, council_id=council_id, action_kind="ADD_ANNOTATION", requested_verdict="", note=note)


@app.post("/review/override")
def override_review(
    request: Request,
    council_id: str = Form(...),
    requested_verdict: str = Form(...),
    note: str = Form(...),
) -> RedirectResponse:
    requested_verdict = requested_verdict.upper()
    if requested_verdict not in VALID_REVIEW_VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid requested verdict")
    return _process_review_action(request=request, council_id=council_id, action_kind="OVERRIDE_VERDICT", requested_verdict=requested_verdict, note=note)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "database": str(get_db_path()),
        "records": len(list_backlog()),
    }


@app.get("/backlog")
def backlog() -> list[dict[str, Any]]:
    return list_backlog()


@app.get("/audit-logs")
def audit_logs(limit: int = 50) -> list[dict[str, Any]]:
    return list_audit_logs(limit=limit)


@app.get("/review-actions")
def review_actions(request: Request, limit: int = 25) -> Any:
    if _current_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return list_review_actions(limit=limit)


@app.post("/proposals/judge")
def judge_proposal(request: ProposalRequest) -> dict[str, Any]:
    entity = get_backlog_entity(request.proposed_action.entity_id)
    decision = evaluate_proposal(entity, request.model_dump())
    _record_decision(request, decision)
    return decision.to_dict()


@app.post("/proposals/commit")
def commit_proposal(request: ProposalRequest) -> dict[str, Any]:
    entity = get_backlog_entity(request.proposed_action.entity_id)
    decision = evaluate_proposal(entity, request.model_dump())
    current_payload = request.model_dump()

    if decision.final_verdict == "APPROVED":
        updated = update_backlog_entity(
            request.proposed_action.entity_id,
            request.proposed_action.payload,
        )
        current_payload["committed_entity"] = updated
    else:
        current_payload["committed_entity"] = None

    append_audit_log(
        correlation_id=request.transaction_metadata.correlation_id,
        agent_id=request.transaction_metadata.agent_id,
        council_id=decision.council_id,
        target_table=request.proposed_action.target_table,
        action_type=request.proposed_action.action_type,
        verdict=decision.final_verdict,
        rationale=decision.final_rationale,
        system_action=decision.system_action,
        human_review_required=decision.human_review_required,
        votes=decision.to_dict()["votes"],
        payload=current_payload,
    )

    return {
        "decision": decision.to_dict(),
        "committed": decision.final_verdict == "APPROVED",
        "entity": get_backlog_entity(request.proposed_action.entity_id),
    }


@app.get("/rules")
def rules() -> list[dict[str, str]]:
    return [
        {"rule_id": "INT-001", "category": "Authority", "action": "Hard Reject"},
        {"rule_id": "INT-002", "category": "Existence", "action": "Hard Reject"},
        {"rule_id": "INT-003", "category": "Consistency", "action": "Hard Reject"},
        {"rule_id": "INT-004", "category": "Temporal", "action": "Human Review"},
        {"rule_id": "SAFE-001", "category": "UI Integrity", "action": "Flag for Review"},
        {"rule_id": "SAFE-003", "category": "Immutable State", "action": "Flag for Review"},
        {"rule_id": "SAFE-007", "category": "Safety / Content", "action": "Hard Reject"},
    ]
