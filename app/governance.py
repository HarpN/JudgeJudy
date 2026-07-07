from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

FORBIDDEN_TOKENS = ("<script", "{{", "}}", "ignore previous instructions", "rm -rf", "DROP TABLE")
ALLOWED_ACTIONS = {"UPDATE_STATUS", "SYNC_RECONCILE", "FLAG_REVIEW", "ADD_ANNOTATION", "OVERRIDE_VERDICT"}
ALLOWED_TABLES = {"local_backlog", "review_actions"}
ALLOWED_VERDICTS = {"APPROVED", "REJECTED", "PENDING_REVIEW"}


@dataclass
class Vote:
    judge_id: str
    status: str
    rationale: str


@dataclass
class CouncilDecision:
    council_id: str
    final_verdict: str
    final_rationale: str
    votes: list[Vote]
    system_action: str
    human_review_required: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["votes"] = [asdict(vote) for vote in self.votes]
        return payload


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_strings(nested)


def _contains_forbidden_tokens(*values: Any) -> bool:
    text = " ".join(segment.lower() for value in values for segment in _iter_strings(value))
    return any(token.lower() in text for token in FORBIDDEN_TOKENS)


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def evaluate_proposal(entity: dict[str, Any] | None, proposal: dict[str, Any]) -> CouncilDecision:
    council_id = f"cncl-{uuid4().hex[:10]}"
    action = proposal.get("proposed_action", {})
    metadata = proposal.get("transaction_metadata", {})
    payload = action.get("payload", {})

    votes: list[Vote] = []
    review_flags: list[str] = []
    hard_rejects: list[str] = []

    target_table = action.get("target_table")
    action_type = action.get("action_type")

    if target_table not in ALLOWED_TABLES:
        hard_rejects.append("INT-001: Database writes are restricted to local_backlog.")

    if action_type not in ALLOWED_ACTIONS:
        review_flags.append("SAFE-001: Action type is not recognized as a standard write path.")

    if entity is None:
        hard_rejects.append("INT-002: Target entity does not exist.")

    if target_table == "review_actions" and action_type == "OVERRIDE_VERDICT":
        requested_verdict = str(payload.get("requested_verdict", "")).upper()
        if requested_verdict not in ALLOWED_VERDICTS:
            hard_rejects.append("INT-002: Requested verdict is not valid for a council override.")

    if entity is not None and action_type == "UPDATE_STATUS":
        requested_status = str(payload.get("status", "")).upper()
        if requested_status == "COMPLETED":
            earned = int(payload.get("non_dlc_trophies_earned", entity["non_dlc_trophies_earned"]))
            total = int(payload.get("non_dlc_trophies_total", entity["non_dlc_trophies_total"]))
            completion = int(payload.get("completion", entity["current_completion"]))
            if earned < total or completion < 100:
                hard_rejects.append("INT-003: Platinum/completed status requires all non-DLC trophies earned.")

    if entity is not None:
        release_date = _parse_date(entity.get("release_date"))
        completion_date = _parse_date(payload.get("completion_date"))
        if release_date and completion_date and completion_date < release_date:
            review_flags.append("INT-004: Completion date predates release date.")

    if _contains_forbidden_tokens(action, metadata, payload, proposal.get("agent_rationale")):
        hard_rejects.append("SAFE-007: Unsafe or disallowed content detected.")

    rationale = proposal.get("agent_rationale", "")
    if len(str(rationale)) > 150:
        review_flags.append("SAFE-001: Agent rationale exceeds the length threshold.")

    votes.append(
        Vote(
            judge_id="JUDY-SYNC",
            status="REJECTED" if any(flag.startswith("INT-001") or flag.startswith("INT-002") or flag.startswith("INT-003") for flag in hard_rejects) else "APPROVED",
            rationale="Schema and source integrity evaluated.",
        )
    )
    votes.append(
        Vote(
            judge_id="JUDY-SCRAPE",
            status="REJECTED" if any(flag.startswith("SAFE-007") for flag in hard_rejects) else "APPROVED",
            rationale="Content safety evaluation completed.",
        )
    )
    votes.append(
        Vote(
            judge_id="JUDY-AGENT",
            status="PENDING_REVIEW" if review_flags and not hard_rejects else ("REJECTED" if hard_rejects else "APPROVED"),
            rationale="Domain logic and operational heuristics reviewed.",
        )
    )

    if hard_rejects:
        final_verdict = "REJECTED"
        system_action = "NO_WRITE"
        human_review_required = False
        final_rationale = "; ".join(hard_rejects)
    elif review_flags:
        final_verdict = "PENDING_REVIEW"
        system_action = "DEFERRED_VALIDATION"
        human_review_required = True
        final_rationale = "; ".join(review_flags)
    else:
        final_verdict = "APPROVED"
        system_action = "COMMIT"
        human_review_required = False
        final_rationale = "Consensus achieved and proposal cleared for writeback."

    return CouncilDecision(
        council_id=council_id,
        final_verdict=final_verdict,
        final_rationale=final_rationale,
        votes=votes,
        system_action=system_action,
        human_review_required=human_review_required,
    )
