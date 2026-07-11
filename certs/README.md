# Local mTLS Certificates

Place local development certificates in this directory when testing the mTLS compose profile.

## Generate Certificates

Run from the repository root:

```powershell
./scripts/generate-dev-certs.ps1 -Force
```

Optional parameters:

- `-DaysValid 30` sets certificate lifetime (default 30 days)
- `-CertsDir certs` sets output directory relative to repo root

## Verify Certificates And Handshakes

```powershell
./scripts/verify-mtls.ps1
```

Use `-SkipHandshake` when services are not running and you only want chain validation.

Expected paths:

- certs/ca/clients-ca.crt
- certs/ca/judy-server-ca.crt
- certs/judy/server.crt
- certs/judy/server.key
- certs/clients/caller.crt
- certs/clients/caller.key

These files are ignored by git and should never contain production private keys.

Security notes:

- These certificates are for local development only.
- Rotate by re-running the script with `-Force`.
- Keep private key permissions restricted to your local user account.
