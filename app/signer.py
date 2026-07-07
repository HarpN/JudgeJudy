from __future__ import annotations

import hashlib
import hmac
import json


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def sign_payload(secret: str, payload: dict) -> str:
    body = canonical_json(payload).encode("utf-8")
    key = secret.encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, payload: dict, signature: str) -> bool:
    expected = sign_payload(secret, payload)
    return hmac.compare_digest(expected, signature)
