# Judy Council

Judy Council is the governance service for inter-agent proposal evaluation and commit control. It runs as a gRPC service, enforces signed requests, applies council policy rules, and writes approved state changes to SQLite.

## What It Does

- Receives proposals from agent-zone services
- Evaluates proposals through the council matrix
- Returns `APPROVED`, `REJECTED`, or `PENDING_REVIEW`
- Commits only approved mutations
- Persists every decision to audit logs

## gRPC Contract

Service: `judy.JudyCouncil`

- `Health(google.protobuf.Empty) -> google.protobuf.Struct`
- `JudgeProposal(google.protobuf.Struct) -> google.protobuf.Struct`
- `CommitProposal(google.protobuf.Struct) -> google.protobuf.Struct`

Shared schema: `proto/judy.proto`

## Security Model

- Request signature required by default (`JUDY_REQUIRE_SIGNATURE=true`)
- Signature verified with HMAC SHA-256 (`X-Charon-Signature`)
- Optional TLS server mode for gRPC (`JUDY_GRPC_TLS_ENABLED=true`)
- Optional inbound mTLS client certificate enforcement (`JUDY_GRPC_TLS_REQUIRE_CLIENT_AUTH=true`)
- Namespace-restricted ingress via Helm NetworkPolicy

## Data Stores

SQLite tables initialized automatically at startup:

- `local_backlog`
- `audit_logs`
- `review_actions`

## Local Run

```bash
docker compose up --build -d
```

Judy listens on `localhost:50052`.

## Tests

```bash
docker compose run --rm --build judy pytest -q
```

## Configuration

```env
JUDY_DB_PATH=/data/judy.db
GRPC_PORT=50052
GRPC_MAX_WORKERS=32
JUDY_REQUIRE_SIGNATURE=true
CHARON_SIGNATURE_HEADER=X-Charon-Signature
CHARON_SIGNATURE_SECRET=charon-dev-secret
JUDY_GRPC_TLS_ENABLED=false
JUDY_GRPC_TLS_CERT_PATH=/etc/judy/tls/server.crt
JUDY_GRPC_TLS_KEY_PATH=/etc/judy/tls/server.key
JUDY_GRPC_TLS_REQUIRE_CLIENT_AUTH=false
JUDY_GRPC_TLS_CLIENT_CA_CERT_PATH=/etc/judy/ca/clients-ca.crt
```

### Compose mTLS Profile

Generate local dev certificates first:

```powershell
./scripts/generate-dev-certs.ps1 -Force
```

Run Judy with mTLS enabled:

```bash
docker compose -f docker-compose.yml -f docker-compose.mtls.yml up --build
```

Verify certificate chains and handshake:

```powershell
./scripts/verify-mtls.ps1
```

If Judy is not running, verify cert trust only:

```powershell
./scripts/verify-mtls.ps1 -SkipHandshake
```

## Kubernetes / Helm

Helm chart path: `charts/judy`

```bash
helm upgrade --install judy charts/judy -n governance-zone --create-namespace
```

The chart includes:

- Deployment
- ServiceAccount
- Service (gRPC)
- Signature secret
- Ingress NetworkPolicy from `agent-zone`
- TLS-ready mount points for server certificates

## Repository Layout

```text
JudgeJudy/
├── app/
│   ├── grpc_server.py
│   ├── database.py
│   ├── governance.py
│   └── signer.py
├── proto/
│   └── judy.proto
├── charts/judy/
├── tests/
│   └── test_api.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
