# Security And Permissions

## Actual API trust surfaces — 2026-11-03

The [complete endpoint authority matrix](../audits/stage6_1_package_g_2026-11-03/ENDPOINT_AUTHORITY_MATRIX.md)
is the source-level inventory of caller, admission and authority for each HTTP
method/path and the separate offline/root operations. Its JSON inventory and
route-contract tests detect drift; neither is a production exposure scan.

- The shipped API launcher binds `127.0.0.1:5000`. Most management/observation
  endpoints have **no per-request authentication**. “Trusted-local” is a caller
  and deployment assumption, not Unix peer-UID verification or a guarantee
  supplied by a loopback destination. A caller that reaches this listener may
  reach privileged management actions. Untrusted local processes, browser or
  proxy reachability require an explicit deployment trust assessment.
- Selected presentation writes and secure-event reads have their own host
  registry/legacy-policy, scope, credential, origin/Host and lease contracts.
  API-client scopes restrict those admitted surfaces; possession of a scoped
  credential does not sandbox access to the entire management listener.
  Policy-unselected and legacy behavior are distinct supported cases, not
  registry-only semantics silently imposed on every route.
- Public Xray capability subscriptions, including the supported legacy UUID
  subscription URL, are separate from management credentials. The shipped
  Gateway is a narrow GET/HEAD subscription proxy; it is not general API
  authentication. Capability URL material must never enter logs or Git.
- Control-plane import has a separate protected host verifier and request-grant
  capability. Offline database/native/registry/installer commands retain their
  own root, filesystem, maintenance, source and writer checks. A CLI name or a
  service's root UID is not interchangeable with any one of these admissions.
- `requested_by`, attribution/correlation headers and display tags grant no
  authority. Existing identity leases, revoke/drain and response-publication
  boundaries remain surface-specific; see the matrix for their exact owners.

A broad reverse-proxy mapping to the management listener would cross this trust
boundary. The retained [UI Nginx transport example](../../ui/README.md) has
`listen 5500` and a broad `/api/v2/` proxy without its own access controls; it
is explicitly historical and requires a separately enforced trusted management
boundary, rather than serving as a secure public deployment template. Repository source does not establish deployed NPM mappings, firewall
reachability, DNS rebinding protections or who can call localhost. Those facts
are **NOT VERIFIED** here. Public management exposure/global authentication is
a separately reviewed policy change; this package does not introduce it or
claim the trusted-local residual has disappeared. See the [closure dispositions](../audits/stage6_1_package_g_2026-11-03/FINDING_DISPOSITIONS.md).

## Required Privileges

FWRouter needs root-level privileges for host networking operations:

- `nft`
- `ip rule`
- `ip route`
- `sysctl`
- `iptables` DNS capture rules
- systemd unit installation/reload
- Docker Compose runtime management

The Mihomo container requires:

- `NET_ADMIN`
- `NET_RAW`
- `/dev/net/tun`

## Secrets And Local State

The git source tree must not contain:

- `.env`
- `.venv`
- SQLite databases
- generated runtime artifacts
- logs
- backups
- subscription secrets
- private keys or certificates

These are host-local deployment/runtime concerns.

## Control-Plane Safety

- FWRouter own traffic stays direct-safe by default.
- Protected management/control-plane destinations must not be captured into VPN loops.
- External-management attribution is audit metadata, not authorization; admission is defined by the actual endpoint and owner contract in the matrix above.
- Bypass mode requires explicit confirmation and is applied through a job.
