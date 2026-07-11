from __future__ import annotations

import grpc
import pytest
from google.protobuf import empty_pb2, json_format, struct_pb2

from app.grpc_server import create_server
from app.signer import sign_payload


@pytest.fixture(scope="module")
def channel() -> grpc.Channel:
    server = create_server(bind_address="127.0.0.1:0")
    server.start()

    grpc_channel = grpc.insecure_channel(f"127.0.0.1:{server.bound_port}")
    grpc.channel_ready_future(grpc_channel).result(timeout=5)

    yield grpc_channel

    grpc_channel.close()
    server.stop(None)


def _health_call(channel: grpc.Channel):
    return channel.unary_unary(
        "/judy.JudyCouncil/Health",
        request_serializer=empty_pb2.Empty.SerializeToString,
        response_deserializer=struct_pb2.Struct.FromString,
    )


def _judge_call(channel: grpc.Channel):
    return channel.unary_unary(
        "/judy.JudyCouncil/JudgeProposal",
        request_serializer=struct_pb2.Struct.SerializeToString,
        response_deserializer=struct_pb2.Struct.FromString,
    )


def _commit_call(channel: grpc.Channel):
    return channel.unary_unary(
        "/judy.JudyCouncil/CommitProposal",
        request_serializer=struct_pb2.Struct.SerializeToString,
        response_deserializer=struct_pb2.Struct.FromString,
    )


def _struct_payload(payload: dict) -> struct_pb2.Struct:
    message = struct_pb2.Struct()
    json_format.ParseDict(payload, message)
    return message


def _signed_metadata(payload: dict) -> tuple[tuple[str, str], ...]:
    normalized = json_format.MessageToDict(_struct_payload(payload))
    signature = sign_payload("charon-dev-secret", normalized)
    return (("x-charon-signature", signature),)


def test_health(channel: grpc.Channel) -> None:
    response = _health_call(channel)(empty_pb2.Empty())
    body = json_format.MessageToDict(response)
    assert body["status"] == "ok"
    assert body["transport"] == "grpc"


def test_judge_rejects_missing_entity(channel: grpc.Channel) -> None:
    payload = {
        "transaction_metadata": {
            "agent_id": "backlog-assistant-v1",
            "timestamp": "2026-07-06T21:54:13Z",
            "correlation_id": "test-corr-001",
        },
        "proposed_action": {
            "target_table": "local_backlog",
            "action_type": "UPDATE_STATUS",
            "entity_id": "missing-game",
            "payload": {"status": "COMPLETED"},
        },
        "agent_rationale": "Testing a rejection path.",
    }

    response = _judge_call(channel)(_struct_payload(payload), metadata=_signed_metadata(payload))
    body = json_format.MessageToDict(response)
    assert body["final_verdict"] == "REJECTED"


def test_commit_approved_change_updates_entity(channel: grpc.Channel) -> None:
    payload = {
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
                "notes": "Updated by test suite.",
            },
        },
        "agent_rationale": "Testing a successful commit path.",
    }

    response = _commit_call(channel)(_struct_payload(payload), metadata=_signed_metadata(payload))
    body = json_format.MessageToDict(response)
    assert body["committed"] is True
    assert body["entity"]["current_completion"] == 82.0


def test_judge_requires_signature(channel: grpc.Channel) -> None:
    payload = {
        "transaction_metadata": {
            "agent_id": "backlog-assistant-v1",
            "timestamp": "2026-07-06T21:54:13Z",
            "correlation_id": "test-corr-003",
        },
        "proposed_action": {
            "target_table": "local_backlog",
            "action_type": "UPDATE_STATUS",
            "entity_id": "game_105",
            "payload": {"status": "ACTIVE"},
        },
        "agent_rationale": "Unsigned proposal should fail.",
    }

    with pytest.raises(grpc.RpcError) as exc:
        _judge_call(channel)(_struct_payload(payload))

    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
