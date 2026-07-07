from __future__ import annotations

import os
from concurrent import futures
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
_GRPC_TLS_ENABLED = os.getenv("JUDY_GRPC_TLS_ENABLED", "false").lower() == "true"
_GRPC_TLS_CERT_PATH = os.getenv("JUDY_GRPC_TLS_CERT_PATH", "/etc/judy/tls/tls.crt")
_GRPC_TLS_KEY_PATH = os.getenv("JUDY_GRPC_TLS_KEY_PATH", "/etc/judy/tls/tls.key")


def _dict_to_struct(payload: dict[str, Any]) -> struct_pb2.Struct:
    message = struct_pb2.Struct()
    json_format.ParseDict(payload, message)
    return message


def _struct_to_dict(message: struct_pb2.Struct) -> dict[str, Any]:
    return json_format.MessageToDict(message)


def _metadata_dict(context: grpc.ServicerContext) -> dict[str, str]:
    return {item.key.lower(): item.value for item in context.invocation_metadata()}


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

    try:
        request = ProposalRequest.model_validate(payload)
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

    try:
        request = ProposalRequest.model_validate(payload)
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
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))

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
        with open(_GRPC_TLS_KEY_PATH, "rb") as key_file:
            private_key = key_file.read()
        with open(_GRPC_TLS_CERT_PATH, "rb") as cert_file:
            certificate_chain = cert_file.read()
        credentials = grpc.ssl_server_credentials(((private_key, certificate_chain),))
        server.add_secure_port(listen_address, credentials)
    else:
        server.add_insecure_port(listen_address)

    return server


def serve() -> None:
    server = create_server()
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
