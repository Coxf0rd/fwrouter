# Boot Flow

## Current checkpoint — 2026-10-15

The CI milestone is CLOSED following actual squash/main validation (PR5, main run38363962787). PR8 merged as dabf9f4; PR10 merged/deployed as ffe506e. Production remains ffe506e with the original78 identities restored and API/Gateway recovered. PR11 isolation/redaction fixes are source-only, not deployed: Synthetic Functional Acceptance PASS, Production State Acceptance PARTIAL, controlled external E2E NOT RUN, overall Production Acceptance BLOCKED. These are recorded receipts, not new live probes. Earlier dated checkpoints below are historical where superseded; they do not reopen CI or describe the current held/recovery state.

Use the [active roadmap](../ROADMAP.md) and [decision register](DECISIONS/ROADMAP_DECISION_REGISTER_2026-10-15.md) for priorities, dependencies and remaining evidence. Operational Performance requires a fresh audit after PR8/10/11 before replaying old optimization packages. Full Debian host/systemd/reboot and positive external E2E remain separate unqualified gates. No implementation or production action is authorized by this documentation reconciliation.


## Bundled deployment service inventory

This inventory describes the bundled deployment, not mandatory platform dependencies. Enable runtime-specific services, followers and timers only for the installed/configured capabilities. Core startup must not require absent Xray/Mihomo or their followers; alternative and multiple adapters follow their declared lifecycle contracts.

- `fwrouter-mihomo.service`
- `fwrouter-xray.service`
- `fwrouter-api.service`
- `fwrouter-xray-sub-gateway.service`
- `fwrouter-subscription-refresh.timer`
- `fwrouter-maintenance.timer`
- `fwrouter-jobs-retention-dry-run.timer`
- `fwrouter-traffic-collect.timer`

## Startup Order

1. `network-online.target`
2. `fwrouter-api.service`
3. Optional managed runtimes such as `fwrouter-mihomo.service` and `fwrouter-xray.service`
4. Optional follower services such as `fwrouter-xray-sub-gateway.service`
6. timers and regular jobs

Race-condition protections:

- `fwrouter-api.service` has `After/Wants=network-online.target` only, so the core control plane can start without optional runtimes
- Mihomo startup waits for `127.0.0.1:5200`
- backend startup recovery for `selective`/`vpn` briefly waits for the active `vpn_dataplane` adapter before apply, so it does not materialize a degraded contour before Mihomo or an external VPN dataplane is ready
- the subscription gateway waits for `127.0.0.1:5000`
- Xray startup requires `docker network inspect "$FWROUTER_DOCKER_PROXY_NETWORK"`; default `fwrouter_proxy`

## Bundled host prerequisites

The following describes the current Linux-router deployment. Native tools, Docker, TUN and policy-routing prerequisites are capability-specific; this is not qualification of all optional/alternative module combinations.

- `/dev/net/tun`
- `nft` and `ip` commands
- Docker daemon
- persistent and runtime directories
- `sysctl` with `src_valid_mark=1`, `ip_forward=1`, `rp_filter=0`
- routing table alias `100 fwrouter_vpn`

## What Backend Must Create

- `/var/lib/fwrouter-v2/*` bootstrap directories
- `/var/log/fwrouter/*`
- `/run/fwrouter-v2`
- live owned `nftables` table and `ip rule/ip route` when missing after reboot
- Mihomo selector restore and intended routing recovery
- scoped LAN/Tailscale subject rules when SQLite intent says `direct/selective/vpn` but live `fwrouter_classify` lacks subject-specific rules

## What Does Not Survive Reboot

- `nftables` table `inet fwrouter_v2`, unless a separate system firewall unit recreates it
- `ip rule`
- `ip route` in the custom table
- live listener/process state of containers
- correspondence between live `fwrouter_classify` and persisted per-client overrides after manual/emergency dataplane reload

## What Must Be Persistent

- `/etc/sysctl.d/99-fwrouter-routing.conf`
- `/etc/iproute2/rt_tables.d/fwrouter.conf`
- systemd units and timers
- generated configs in `/var/lib/fwrouter-v2/generated/`
- last-good snapshots and SQLite state

## Directories

Created ahead of time or by bootstrap:

- `/var/lib/fwrouter-v2`
- `/var/lib/fwrouter-v2/generated`
- `/var/lib/fwrouter-v2/jobs`
- `/var/lib/fwrouter-v2/cache`
- `/var/lib/fwrouter-v2/state`
- `/var/log/fwrouter`

Runtime-only:

- `/run/fwrouter-v2`
- debug dumps inside `/var/lib/fwrouter-v2/debug`

## Reboot Verification

```bash
systemctl status --no-pager fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
systemctl is-enabled fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
systemctl is-enabled fwrouter-subscription-refresh.timer fwrouter-maintenance.timer fwrouter-jobs-retention-dry-run.timer fwrouter-traffic-collect.timer
ip rule show
ip route show table all
nft list ruleset
sysctl net.ipv4.ip_forward net.ipv4.conf.all.src_valid_mark net.ipv4.conf.all.rp_filter net.ipv4.conf.default.rp_filter
ss -ltnup | grep -E '127.0.0.1:5000|127.0.0.1:5200|:5202|:5055'
/opt/fwrouter-api/scripts/check_boot_persistence.sh
```

## If Scoped Client Rules Disappear After Backend Restart

Scenario: SQLite/API show client `desired_mode=selective` or `vpn`, but the live chain contains only `goto fwrouter_direct comment "global direct v1"`.

Expected protection:

- `bootstrap.recover_startup_scoped_subject_routing()` reads active LAN/Tailscale subjects from SQLite
- it then reads `nft list chain inet fwrouter_v2 fwrouter_classify`
- if a persisted scoped subject is absent from the live chain, backend runs normal `set_subject_admin_mode(..., requested_by="startup-scoped-subject-recovery")`
- re-apply must rebuild a subject-aware manifest; `global=direct` must not be considered pure-direct runtime while per-client `selective`/`vpn` exists

## API lifecycle scale follow-up — 2026-10-13

See [runtime stabilization audit](../audits/api_runtime_stability_2026-10-13/REPORT.md). Source-only changes preserve native writers and existing startup recovery gates: readonly health uses the existing2s singleflight; scheduler references survive stop timeouts; prewarm closes admission on stop; all stop signals precede a shared join budget. Partial startup exceptions execute cleanup. This is not host/systemd stop qualification. Production API and dependent timers remain held pending verified fix rollout; do not infer service activation from the enabled-unit reference above.

The corrective lifecycle now also drains already-started JobManager workers within that same14s maximum budget. Mutation-triggered prewarm cleanup is unconditional; disabling startup schedulers does not disable ownership of work created by API mutations. Jobs/native writers are not force-cancelled. Xray client directive collection shares a fresh operation-local routing/default-enforcement snapshot, avoiding one common rules preflight per client; subsequent invocation, CAS and native publication proof remain fresh. Hosted full-startup qualification and live activation remain separate gates.

Controller readiness after a Mihomo restart now probes only /version with a bounded request; it explicitly does not prove policy. Fresh selector restoration health/readback and final generation/incarnation/revision publication proof remain mandatory. This avoids redundant full rules readback in startup and does not cache mutation proof. Hosted scale and live acceptance remain pending.

The adapter may retain one bounded safe YAML parse keyed by exact current config text and loader identity. Current bytes are still read on every health invocation, and native selectors/rules plus policy/receipt proof are recomputed. This is parse reuse only, not cached applied-state or native proof; file mtime/size cannot validate it.

## Controlled stabilization rollout — 2026-10-14

Feature-source4ffd686 is installed backend/docs only after hosted full scope PASS. Live startup still fails120s readiness and20s shutdown; API/dependents held. Native generation checkpoint remainsxray_applied with8 UUID replacements among78, so final identity/native publication is not accepted. Native routing/DNS/Rescue observed preserved. Further activation/recovery requires diagnosis of nonempty profile convergence and checkpoint-owned safe restore; do not restore insecure archived generator. See the runtime stability LIVE_ACCEPTANCE report.


### Nonempty profile planner and stale recovery qualification

Read-only checkpoint forensics finds DB's original78 identities preserved but native candidate8 replacements/public snapshots uncommitted. Stale selection revision/Mihomo incarnation and missing inventory bindings prohibit blind resume or file restore. New checkpoints retain transition config digests and recovery requires them before native mutation. Nonempty hosted reproduction38478166408 reaches the redundant per-client effective-state/preflight loop; the prospective planner now resolves persisted identities and performs enrichment once with its shared snapshots, retaining final source/CAS/native proof. Qualification is pending; production API/workers/timers remain held, not repaired by a source commit.


### Profile identity and recovery boundary — 2026-10-14

Protected read-only evidence resolves the8-credential delta: the provider JSON profile gained3 VLESS members (7→10), changing its full canonical-profile hash/server ID before activation. Seven public uuid5 credentials and one internal credential follow that new ID;70 others remain unchanged. Source does not silently alias distinct logical profiles or rewrite production identities. A stable-identity migration needs an authoritative continuity contract and fresh owned publication, not display-name/primary-endpoint guessing.

The legacy xray_applied checkpoint has stale selection/Mihomo fences and no persisted transition digest. New transition-proof regressions and candidate-copy/immutability regressions belong to the routine L3 suite, not its narrow qualified test_xray.py carve-outs. Committed-projection fallback requires binding/mode/public projection and artifact parity before its later native-config identity check; an unexecuted fallback has no observed first refusal. Production API/dependents remain held; current hosted readiness qualification is reported in the linked runtime audit.


### First strict nonempty hosted startup — 38548698209

ca1cd8a hosted fixture passes with82 preloaded identities (70 public,10 internal including the virtual vpn-auto target,2 fixed), all identities retained and7 snapshots verified. Real Reality TCP is observed via native HEAD and endpoint VPN source;24 concurrent reads/cleanup pass. Startup44.8415s is strictly below45s but near the limit; reproducibility is pending. This does not repair original production78 identities or authorize restarting held API/dependents. See REPORT.md for scoped metrics and unimplemented continuity/recovery contract.


### PR8 durable identity migration / recovery source contract (2026-10-14)

Schema25 separates content-addressed provider inventory from persistent logical-node credential intent. Public token-scoped and internal VPN-auto credentials retain UUID/email through admitted profile aliases. Strict automatic growth requires canonical ordered witnesses and source ownership; incompatible/legacy replacement is marked migration-required, not silently registered with new credentials. Reviewed private manifest preview/apply is an offline operator path before normal generation. Schema upgrade, intent migration and native generation are separate operations.

A migrated legacy checkpoint cannot resume directly. Explicit SHA/ledger-bound supersession stages and validates a fresh generation, preserves the old checkpoint archive, and replaces the active marker atomically under fresh source/selection/incarnation/native fences. Recovery can close only proven current candidate/projection state; a mixed identity preimage is not a healthy rollback target. Previous schema24 code is incompatible with schema25 persistent mapping. No live operation has been performed by this source change. Hosted native qualification and startup margin remain pending; do not start production API/dependent units on this documentation alone.

## Staged Xray startup publication receipt — 2026-10-15

Authoritative persisted subscription recovery executes under the existing Xray writer guard. The staged finalizer may return schema1 `staged_publication_receipt` only after Xray/Mihomo native proof, profile snapshot publication and durable checkpoint closure. Bootstrap consumes a structurally valid successful receipt to avoid a second Mihomo reconcile and duplicate profile promotion. Legacy/nonstaged/malformed/failed results retain full final reconciliation. Pending revision clearing still requires authoritative inventory, complete success and its original CAS predicate. This is a same-call completion result, not a persistent readiness cache or permission to skip native proof. Hosted timing qualification is pending.

## Provider re-entry native policy preparation (PR8, 2026-10-15)

Emergency Direct recovery must establish current Mihomo config/native/UDP receipt parity before capturing the final recovery snapshot. The existing fenced reconciler prepares the runtime while the durable Direct override remains active. Provider binding, credential ownership, desired mode, exclusive source, pending marker, target/path, pool and selection provenance must remain identical across preparation. A native incarnation or selection revision change requires the same reconciler operation/job receipt: exact previous revision to next revision, current active config hash/incarnation, verified native proof and successful publication. Unreceipted or foreign transitions are rejected; the later recovery snapshot is fresh. A mismatch or unverified prepare retains Direct. Endpoint probes remain outside the writer guard; subsequent claim, Core apply, post-probe and terminal publication retain exact fresh CAS/incarnation fencing. Targeted hosted run38692443202 confirms reentry/schema/browser and fencing cases; final mandatory PR CI remains pending. No production activation is implied.

Durable subscription credentials are scoped by client token. Successful account deletion must tombstone those exact credentials and retain retirement history atomically with the final account removal, after verified native cleanup. Recreating a legacy slug/token must not resurrect retired UUIDs. Failed native reconciliation or final transaction rollback must not revoke the retained account credentials. Internal VPN-auto and unrelated accounts are independent scopes.
