# StealthSurf authenticated GET fixtures — 2026-10-01

Captured from the authenticated account API with sequential GET requests only.
All provider mutations, routing changes, runtime changes, deployment and restart
were excluded. These responses are examples, not complete provider capabilities.

Each numbered JSON file is the response envelope, preserving field names,
arrays, ordering, null versus missing and value types. Credentials-bearing
connection_url values are replaced by `<redacted>`. Numeric entity IDs are
consistently fictionalized across configs, locations, discovery and endpoint
metadata. Literal IPs use documentation ranges; the status hostname uses an
`.example.invalid` placeholder mapped from its public IP. ETags are redacted.
No original-to-fixture identity map or raw response is stored.

metadata.json links each response to its sanitized request endpoint, HTTP
status, selected response headers, observation timestamp and actual value-type
schema. sensitive_structure.json describes the observed connection URL shape
without credentials. Earlier snapshots conservatively redact a few non-secret
strings; the final configs/serverStats/subconfig-protocols snapshots retain
those values. Refer to actual_schema when a string is redacted. These snapshots
must not be used as credential-bearing runtime fixtures or invented protocol
examples. No errors/429 or unavailable-slot-zero examples were observed.

The account currently returned Hysteria2; HTTP/SOCKS5 appeared only in the
subconfig capability response, and the subconfig itself was null. Empty paid
options/cloud resources and two empty location discovery responses are real
observations. Future adapters must handle data beyond these examples.
