from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def login(username: str = "Nate") -> None:
    code = "nate" if username == "Nate" else "doom"
    response = client.post(
        "/login",
        data={"username": username, "passcode": code},
        follow_redirects=False,
    )
    assert response.status_code in {303, 307}


def test_review_gate_redirects_unauthenticated_users() -> None:
    response = client.get("/review", follow_redirects=False)
    assert response.status_code in {303, 307}
    assert response.headers["location"] == "/login"


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_judge_rejects_missing_entity() -> None:
    response = client.post(
        "/proposals/judge",
        json={
            "transaction_metadata": {
                "agent_id": "backlog-assistant-v1",
                "timestamp": "2026-07-06T21:54:13Z",
                "correlation_id": "test-corr-001",
            },
            "proposed_action": {
                "target_table": "local_backlog",
                "action_type": "UPDATE_STATUS",
                "entity_id": "missing-game",
                "payload": {
                    "status": "COMPLETED"
                },
            },
            "agent_rationale": "Testing a rejection path.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["final_verdict"] == "REJECTED"


def test_commit_approved_change_updates_entity() -> None:
    response = client.post(
        "/proposals/commit",
        json={
            "transaction_metadata": {
                "agent_id": "backlog-assistant-v1",
                "timestamp": "2026-07-06T21:54:13Z",
                "correlation_id": "test-corr-002",
            },
            "proposed_action": {
                "target_table": "local_backlog",
                "action_type": "UPDATE_STATUS",
                "entity_id": "game_105",
                "payload": {
                    "status": "ACTIVE",
                    "completion": 82,
                    "notes": "Updated by test suite."
                },
            },
            "agent_rationale": "Testing a successful commit path.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["committed"] is True
    assert body["entity"]["current_completion"] == 82


def test_review_console_shows_votes_and_verdict() -> None:
    login("Nate")
    correlation_id = "test-corr-003"
    client.post(
        "/proposals/judge",
        json={
            "transaction_metadata": {
                "agent_id": "backlog-assistant-v1",
                "timestamp": "2026-07-06T21:54:13Z",
                "correlation_id": correlation_id,
            },
            "proposed_action": {
                "target_table": "local_backlog",
                "action_type": "UPDATE_STATUS",
                "entity_id": "game_105",
                "payload": {
                    "status": "ACTIVE"
                },
            },
            "agent_rationale": "Testing the review console.",
        },
    )

    response = client.get("/review?verdict=all")
    assert response.status_code == 200
    assert "Judy Council Review Console" in response.text
    assert "JUDY-SYNC" in response.text
    assert correlation_id in response.text


def test_doom_override_is_recorded_through_the_council() -> None:
    login("DOOM")
    judge_response = client.post(
        "/proposals/judge",
        json={
            "transaction_metadata": {
                "agent_id": "backlog-assistant-v1",
                "timestamp": "2026-07-06T21:54:13Z",
                "correlation_id": "test-corr-004",
            },
            "proposed_action": {
                "target_table": "local_backlog",
                "action_type": "UPDATE_STATUS",
                "entity_id": "game_105",
                "payload": {
                    "status": "ACTIVE"
                },
            },
            "agent_rationale": "Seed decision for the override flow.",
        },
    )
    council_id = judge_response.json()["council_id"]

    response = client.post(
        "/review/override",
        data={
            "council_id": council_id,
            "requested_verdict": "PENDING_REVIEW",
            "note": "DOOM is auditing the council verdict.",
        },
        follow_redirects=False,
    )
    assert response.status_code in {303, 307}

    review_actions_response = client.get("/review-actions")
    actions = review_actions_response.json()
    assert any(action["actor_name"] == "DOOM" and action["target_council_id"] == council_id for action in actions)
