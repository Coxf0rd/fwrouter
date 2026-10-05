# Applied dataplane status validation

Date: 2026-10-05. Source baseline: `1397f50cccbc768fad9fa24fc80f7bacbe820e76`.

## Finding and change

On an applied-manifest status cache miss, `_runtime_check_paths()` previously
returned `applied.nft` as the candidate argument to `dataplane-check.sh`. A
nonempty argument enters that script's candidate path: it creates a temporary
validation input (with a leading `delete table inet fwrouter_v2` when the live
table exists) and runs `nft -c -f`. This repeats candidate syntax validation
for an already-applied file on Health/runtime status reads.

The applied-manifest branch now returns an empty candidate and the applied
manifest. The candidate-manifest fallback still passes an existing
`candidate.nft` for validation; a missing candidate file retains the prior
empty-path behavior. Applied status still runs the live table, required-chain and
policy-routing checks, transparent-counter reads, applied-marker parity check,
and global-mode readback. The existing short cache and explicit refresh
behavior were not changed. This removes candidate syntax validation only; it
does not remove all native status work or establish a whole-Health latency
gain.

## Bounded evidence

- A private SQLite snapshot was made from a `mode=ro` connection using the
  SQLite backup API: 21,291,008 bytes, 5,198 pages at 4,096 bytes/page, 92.403
  ms. It was discarded at process exit. No app, startup lifecycle, scheduler,
  provider, or Health path was run against it; the previously observed `dig`
  calls make that broader native profile unsuitable under this task's boundary.
- The private copy of `applied.nft`, reused unchanged for the native component
  sample was 490,844 bytes. The same check input shape used by the shell script
  is 490,874 bytes including the 30-byte delete-table prefix.
- `nft -c -f` check-only against that private input: **3/3 exit 0**, median
  515.088 ms (min 514.939 ms, max 515.094 ms). Stdout/stderr and ruleset
  contents were discarded. No apply, config write, network request, DNS lookup,
  or provider call was made by this benchmark.
- Before: one candidate-validation invocation and one approximately 490,874
  byte temporary validation input per applied-status cache miss when the live
  table exists. After: zero candidate-validation invocations and zero such
  temporary input bytes on that branch, as verified by the runner-argument
  contract test and the shell script's empty-candidate branch condition. This
  is a stage-level comparison; no post-deploy/API before-after measurement is
  claimed.

## Tests and delivery gates

- Focused L1 status contract tests: 4 passed. They cover applied manifest with
  both applied/candidate files, applied manifest with missing applied file,
  candidate-manifest validation, absence of manifests, the empty candidate
  runner argument, preserved marker-drift projection, and script failure.
- Existing apply-side contract: `test_apply_pipeline_hot_swaps_global_mode_classify_chain`, 1 passed; candidate validation remains in the apply path.
- Selected runtime-summary regression anchors: 3 passed (applied artifacts,
  exposed capability, and active mode mismatch projection). These were run as
  L5 cross-domain anchors; this was not a complete L5 cohort.
- L0 gate-manifest validation: `PASS`, 150 test files covered.
- Isolated L4 smoke: `PASS`; temporary database and target, no provider/public
  network; native validation was `not_requested` by this profile.
- No L6/L7, commit, deploy, API restart, or post-deploy live verification.

## `/servers` attribution boundary

One additional source-call profile was run against a private SQLite backup and
one normal synchronous handler call (`inventory_state=active`, limit 1000).
The measurement wrapped each SQLite execute before fetch completion, unlike the
earlier report's fetch-only timer:

| Stage | Observed |
|---|---:|
| Handler function, before HTTP middleware/queue | 151.0 ms |
| SQLite application queries | 50 SELECTs, 295 rows, 7.151 ms combined execute+fetch |
| SQLite opens/setup | 38 opens, 32.023 ms combined |
| `server_inventory.list_servers` | 119.553 ms |
| Runtime topology helper | 112.834 ms; persisted topology DB stage 3.557 ms |
| Native runtime | 2 loopback `GET /proxies`, 110,936 bytes each, 5.430 / 4.273 ms |
| Per-row conversion / enrichment | 30 rows: 1.546 / 29.957 ms combined |
| Response model dump and JSON encoding | 4.071 ms, 208,081 bytes |

These nested helper timings overlap and must not be summed. Database open/setup
is separate from the 50 SELECT execution-plus-fetch intervals. The one-call
runner blocked subprocesses, non-loopback HTTP, other methods, and delay/probe
paths; it observed zero provider calls or external probes. The temporary
SQLite backup was discarded. A sanitized reproducer is
[`scripts/attribute_servers_isolated.py`](scripts/attribute_servers_isolated.py).

This service/handler call does not include ASGI worker queue, socket wait, or
browser scheduling. The browser audit separately observed a 5.56-second
resource-start-to-requestStart interval for one delayed role request and
approximately 0.18–0.21-second `/servers` responses in the main page. No
matched request-queue, thread-pool, or GIL attribution was collected; the cause
of the historical burst remains open. The two `/proxies` GETs were retained as
observations, not treated as a material bottleneck or changed.

## Source / Tests / Commit / Deploy / Live

Source and affected tests are present in the working tree. Commit and deploy
were not performed. The native check-only sample is bounded read-only evidence,
not live acceptance of the changed API source.
