# Invariants

- `/srv/fwrouter` is the git/source root. Live paths under `/opt`, `/etc`, `/usr/local`, `/var/lib`, `/var/log`, and `/run` are deployment/runtime targets.
- FWRouter core is the routing authority. Mihomo is an egress adapter, not the network policy engine.
- `fwrouter:global` represents FWRouter own traffic and must stay direct-safe. It must not become a normal user-facing VPN subject.
- Client-plane subjects are `lan`, `external_network_client`, and `explicit_external_client`; provider-specific legacy subject names are normalized at service/migration boundaries.
- System/control subjects are `host`, `docker`, and `fwrouter`.
- Xray clients remain forced VPN through their explicit ingress path.
- Host and Docker traffic are direct by default; explicit scoped VPN is valid only when a stable matcher exists.
- Domain-aware selective routing requires router-owned DNS materialization through `dnsmasq` and nft sets.
- Live kernel dataplane state is rebuildable runtime state. SQLite intent and generated/last-good artifacts are the durable source of truth.
- Apply/rollback must remain idempotent and must not leave duplicate `ip rule` entries.
- Startup recovery must recreate missing live dataplane state after reboot without rewriting intended routing to direct.
- Unit tests must not touch live host dataplane, live probes, systemd, Docker runtime, or the production SQLite state unless the test is explicitly marked as live acceptance.
- Project documentation in git must be English. Local non-English notes belong outside the repo in the owner-local decisions tree.
- Provider API reachability, explicit provider-reported remote state, and local VPN/apply/connectivity evidence are distinct. Timeout, unreachable, 5xx, 429, missing, or ambiguous responses are UNKNOWN; they never alone assert remote DOWN or authorize a member switch.
- Emergency Direct is a temporary effective runtime override. Persistent desired VPN, provider intent, selection, and provenance remain intact. Run connectivity probes outside the writer guard; revalidate intent, revision, runtime incarnation, and eligibility before apply/readback and verified re-entry.
- Default tests use isolated state and bounded resources. Every implementation report states which levels ran and why: affected L0–L4 by contract, L5 for shared/domain contract changes, L6 only at scheduled/manual, milestone, release or explicit policy gates, and L7 only in disposable staging/release acceptance (never normal CI). Follow the [Test Architecture and CI/CD Foundation](TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md).
- Preserve a minimal CPU/RAM/SSD footprint as an architectural invariant; performance fixes require measurements and before/after evidence.
- Health schema inspection observes the existing database read-only; initialization/migration remain explicit startup/installer operations. Technical-log queries sanitize every returned record after bounded newest-record selection; optimization must preserve filters, ordering and redaction.
