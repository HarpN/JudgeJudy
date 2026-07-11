from __future__ import annotations

import os
from concurrent import futures
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2
from pydantic import BaseModel, Field

from .database import (
    append_audit_log,
    get_backlog_entity,
    get_db_path,
    init_db,
    list_backlog,
    update_backlog_entity,
)
from .governance import evaluate_proposal
from .signer import verify_signature


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


_SIGNATURE_HEADER = os.getenv("CHARON_SIGNATURE_HEADER", "X-Charon-Signature").lower()
_SIGNATURE_SECRET = os.getenv("CHARON_SIGNATURE_SECRET", "charon-dev-secret")
_REQUIRE_SIGNATURE = os.getenv("JUDY_REQUIRE_SIGNATURE", "true").lower() == "true"
_HOST = os.getenv("HOST", "0.0.0.0")
_GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))
_GRPC_MAX_WORKERS = int(os.getenv("GRPC_MAX_WORKERS", "32"))
_GRPC_TLS_ENABLED = os.getenv("JUDY_GRPC_TLS_ENABLED", "false").lower() == "true"
_GRPC_TLS_CERT_PATH = os.getenv("JUDY_GRPC_TLS_CERT_PATH", "/etc/judy/tls/server.crt")
_GRPC_TLS_KEY_PATH = os.getenv("JUDY_GRPC_TLS_KEY_PATH", "/etc/judy/tls/server.key")
_GRPC_TLS_REQUIRE_CLIENT_AUTH = os.getenv("JUDY_GRPC_TLS_REQUIRE_CLIENT_AUTH", "false").lower() == "true"
_GRPC_TLS_CLIENT_CA_CERT_PATH = os.getenv("JUDY_GRPC_TLS_CLIENT_CA_CERT_PATH", "/etc/judy/ca/clients-ca.crt")
_REPLAY_TTL_SECONDS = int(os.getenv("JUDY_REPLAY_TTL_SECONDS", "300"))

_seen_nonces: dict[str, datetime] = {}
_nonce_lock = Lock()


def _dict_to_struct(payload: dict[str, Any]) -> struct_pb2.Struct:
    message = struct_pb2.Struct()
    json_format.ParseDict(payload, message)
    return message


def _struct_to_dict(message: struct_pb2.Struct) -> dict[str, Any]:
    return json_format.MessageToDict(message)


def _metadata_dict(context: grpc.ServicerContext) -> dict[str, str]:
    return {item.key.lower(): item.value for item in context.invocation_metadata()}


def _parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_replay_envelope(envelope: dict[str, Any], context: grpc.ServicerContext) -> bool:
    issued_at_raw = envelope.get("issued_at")
    nonce = envelope.get("nonce")

    if not issued_at_raw or not nonce:
        context.set_code(grpc.StatusCode.UNAUTHENTICATED)
        context.set_details("Missing issued_at or nonce in signed envelope")
        return False

    try:
        issued_at = _parse_iso8601(str(issued_at_raw))
    except Exception:
        context.set_code(grpc.StatusCode.UNAUTHENTICATED)
        context.set_details("Invalid issued_at in signed envelope")
        return False

    now = datetime.now(timezone.utc)
    if abs((now - issued_at).total_seconds()) > _REPLAY_TTL_SECONDS:
        context.set_code(grpc.StatusCode.UNAUTHENTICATED)
        context.set_details("Signed envelope is outside replay TTL window")
        return False

    with _nonce_lock:
        # Drop stale nonces before evaluating the incoming nonce.
        stale_cutoff = now.timestamp() - _REPLAY_TTL_SECONDS
        stale = [key for key, seen_at in _seen_nonces.items() if seen_at.timestamp() < stale_cutoff]
        for key in stale:
            _seen_nonces.pop(key, None)

        nonce_value = str(nonce)
        if nonce_value in _seen_nonces:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details("Replay detected for signed envelope nonce")
            return False

        _seen_nonces[nonce_value] = now

    return True


def _extract_proposal_payload(payload: dict[str, Any], context: grpc.ServicerContext) -> dict[str, Any] | None:
    envelope_payload = payload.get("payload")
    if not isinstance(envelope_payload, dict):
        return payload

    if not _validate_replay_envelope(payload, context):
        return None

    return envelope_payload


def _ensure_signature(context: grpc.ServicerContext, payload: dict[str, Any]) -> bool:
    if not _REQUIRE_SIGNATURE:
        return True

    metadata = _metadata_dict(context)
    signature = metadata.get(_SIGNATURE_HEADER)
    if not signature:
        context.set_code(grpc.StatusCode.UNAUTHENTICATED)
        context.set_details("Missing proposal signature metadata")
        return False

    if not verify_signature(_SIGNATURE_SECRET, payload, signature):
        context.set_code(grpc.StatusCode.UNAUTHENTICATED)
        context.set_details("Invalid proposal signature")
        return False

    return True


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


def _health(_: empty_pb2.Empty, context: grpc.ServicerContext) -> struct_pb2.Struct:
    del context
    return _dict_to_struct(
        {
            "status": "ok",
            "transport": "grpc",
            "database": str(get_db_path()),
            "records": len(list_backlog()),
        }
    )


def _judge_proposal(request_message: struct_pb2.Struct, context: grpc.ServicerContext) -> struct_pb2.Struct:
    payload = _struct_to_dict(request_message)
    if not _ensure_signature(context, payload):
        return struct_pb2.Struct()

    proposal_payload = _extract_proposal_payload(payload, context)
    if proposal_payload is None:
        return struct_pb2.Struct()

    try:
        request = ProposalRequest.model_validate(proposal_payload)
    except Exception as exc:
        context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        context.set_details(f"Invalid proposal: {exc}")
        return struct_pb2.Struct()

    entity = get_backlog_entity(request.proposed_action.entity_id)
    decision = evaluate_proposal(entity, request.model_dump())
    _record_decision(request, decision)
    return _dict_to_struct(decision.to_dict())


def _commit_proposal(request_message: struct_pb2.Struct, context: grpc.ServicerContext) -> struct_pb2.Struct:
    payload = _struct_to_dict(request_message)
    if not _ensure_signature(context, payload):
        return struct_pb2.Struct()

    proposal_payload = _extract_proposal_payload(payload, context)
    if proposal_payload is None:
        return struct_pb2.Struct()

    try:
        request = ProposalRequest.model_validate(proposal_payload)
    except Exception as exc:
        context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        context.set_details(f"Invalid proposal: {exc}")
        return struct_pb2.Struct()

    entity = get_backlog_entity(request.proposed_action.entity_id)
    decision = evaluate_proposal(entity, request.model_dump())
    current_payload = request.model_dump()

    if decision.final_verdict == "APPROVED":
        updated = update_backlog_entity(request.proposed_action.entity_id, request.proposed_action.payload)
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

    return _dict_to_struct(
        {
            "decision": decision.to_dict(),
            "committed": decision.final_verdict == "APPROVED",
            "entity": get_backlog_entity(request.proposed_action.entity_id),
        }
    )


def create_server(bind_address: str | None = None) -> grpc.Server:
    init_db()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=_GRPC_MAX_WORKERS))

    handlers = {
        "Health": grpc.unary_unary_rpc_method_handler(
            _health,
            request_deserializer=empty_pb2.Empty.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        "JudgeProposal": grpc.unary_unary_rpc_method_handler(
            _judge_proposal,
            request_deserializer=struct_pb2.Struct.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
        "CommitProposal": grpc.unary_unary_rpc_method_handler(
            _commit_proposal,
            request_deserializer=struct_pb2.Struct.FromString,
            response_serializer=struct_pb2.Struct.SerializeToString,
        ),
    }

    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("judy.JudyCouncil", handlers),))

    listen_address = bind_address or f"{_HOST}:{_GRPC_PORT}"

    if _GRPC_TLS_ENABLED:
        if not _GRPC_TLS_CERT_PATH or not _GRPC_TLS_KEY_PATH:
            raise RuntimeError("JUDY_GRPC_TLS_CERT_PATH and JUDY_GRPC_TLS_KEY_PATH are required when TLS is enabled")

        with open(_GRPC_TLS_KEY_PATH, "rb") as key_file:
            private_key = key_file.read()
        with open(_GRPC_TLS_CERT_PATH, "rb") as cert_file:
            certificate_chain = cert_file.read()

        root_certificates = None
        if _GRPC_TLS_REQUIRE_CLIENT_AUTH:
            if not _GRPC_TLS_CLIENT_CA_CERT_PATH:
                raise RuntimeError("JUDY_GRPC_TLS_CLIENT_CA_CERT_PATH is required when client auth is enabled")
            with open(_GRPC_TLS_CLIENT_CA_CERT_PATH, "rb") as ca_file:
                root_certificates = ca_file.read()

        credentials = grpc.ssl_server_credentials(
            ((private_key, certificate_chain),),
            root_certificates=root_certificates,
            require_client_auth=_GRPC_TLS_REQUIRE_CLIENT_AUTH,
        )
        bound_port = server.add_secure_port(listen_address, credentials)
    else:
        bound_port = server.add_insecure_port(listen_address)

    if not bound_port:
        raise RuntimeError("Failed to bind Judy gRPC server")

    server.bound_port = bound_port  # type: ignore[attr-defined]

    return server


def serve() -> None:
    server = create_server()
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
