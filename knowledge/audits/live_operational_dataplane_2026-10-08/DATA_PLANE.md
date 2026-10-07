# Live data-plane evidence — 2026-10-08

## Scope and safety

This is a point-in-time, server-local measurement against source baseline
`6ba7f6f003ef67cbe350bea1e2d092e34c3eb660`. It does not claim performance from
remote client into FWRouter. The initial small-response window preceded the
separate test-client creation. No route, provider, service, or runtime
configuration changed during that initial window. Tailscale remained active.
The initial traffic was HTTPS GET to three public small-response endpoints,
sent directly through `enp1s0` with proxy environment bypassed or through the
loopback Mihomo mixed proxy at `127.0.0.1:5201`.

The live default route to `1.1.1.1` selected `enp1s0` and source
`46.181.174.27`. Both Mihomo controller `127.0.0.1:5200` and mixed proxy
`127.0.0.1:5201` were loopback-bound. Mihomo, Xray, API, and Xray subscription
gateway systemd units were active. The running Xray container used existing
immutable image digest
`sha256:60c138250e2dca6e54259a1333188692fdf2560cb2dae7f6610b3ee92f6c3ff1`
(Xray 26.2.6). The bindings artifact is mode 0600 at
`/var/lib/fwrouter-v2/xray/fwrouter-bindings.json`.

## Initial small-response comparison

The reproducible runner sent 8 serial requests per path, distributed across
`www.gstatic.com/generate_204`, `www.cloudflare.com/cdn-cgi/trace`, and
`example.com/`, then one two-request concurrency wave per path. All 20 requests
succeeded (per path: gstatic 4×204, Cloudflare trace 4×200, example.com 2×200).
Aggregate response bodies totaled 4,011 bytes. There were no retries or
timeouts. This was an idle, very small payload sample; p95/p99 have only
eight serial observations and are descriptive, not stable tail estimates.

| Path and target | n | DNS p50 | TCP phase p50 | TLS/proxy phase p50 | Request-to-first-byte p50 | Total p50 | Total p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Direct WAN, `example.com` | 2 | 61.9 ms | 87.0 ms | 101.7 ms | 94.3 ms | 345.5 ms | 349.4 ms |
| Direct WAN, `www.cloudflare.com` | 3 | 61.8 ms | 86.6 ms | 105.0 ms | 91.1 ms | 348.1 ms | 354.9 ms |
| Direct WAN, `www.gstatic.com` | 3 | 65.0 ms | 63.8 ms | 73.4 ms | 64.1 ms | 260.5 ms | 273.8 ms |
| Direct WAN, all targets | 8 | 61.9 ms | 85.9 ms | 101.7 ms | 89.6 ms | 345.1 ms | 354.9 ms |
| Mihomo 5201, `example.com` | 2 | 0.02 ms | 0.13 ms | 128.9 ms | 112.8 ms | 242.2 ms | 244.1 ms |
| Mihomo 5201, `www.cloudflare.com` | 3 | 0.04 ms | 0.18 ms | 123.4 ms | 104.1 ms | 226.7 ms | 231.5 ms |
| Mihomo 5201, `www.gstatic.com` | 3 | 0.04 ms | 0.21 ms | 113.5 ms | 98.1 ms | 212.0 ms | 238.6 ms |
| Mihomo 5201, all targets | 8 | 0.03 ms | 0.17 ms | 123.4 ms | 104.1 ms | 226.7 ms | 244.1 ms |

The table derives non-overlapping phases from curl's cumulative timers:
`TCP = connect - name lookup`; `TLS/proxy = appconnect - connect`;
`request-to-first-byte = starttransfer - appconnect`. Direct TLS phase is the
client TLS handshake. On the proxy path, DNS and TCP describe only local setup
to the loopback proxy; target DNS is performed upstream and is not separately
observable in curl. Proxy TLS/proxy phase and first-byte time include HTTP
CONNECT and upstream work. These results compare the two client access
contours for this window; these initial GETs were not controller-attributed.
Small per-target samples make percentiles descriptive only.

Two-request concurrent wave results were 2/2 successful on both paths. With
only two observations, they do not support percentile conclusions. The captured
wave values are in `dataplane_measurements.json`.

The median curl-reported download rates (604 B/s direct, 938 B/s Mihomo) reflect
small responses and connection setup, not path capacity.

## Attribution and additional bounded evidence

The source provides a read-only subject proxy GET at
`POST /api/v2/subjects/{subject_id}/proxy-get-check`. It uses the effective
subject policy and exact applied Xray-to-Mihomo handoff, and does not write
`server_ping_state`. A local read-only projection/binding join found existing
active Xray subjects in both target classes (10 global-auto and 68 fixed); the
two candidates are represented only by opaque SHA-256 prefixes in the runner.
No identity, UUID, alias, or credential is included here.

The dedicated test-client create reached `runtime_verified=true`. An initial
observer digest mismatch was cleared by the root's read-only equivalence review:
all 78 pre-existing bindings and the non-user routing/outbound state matched.
The test identity is represented here only by a SHA-256 prefix; its private
UUID/email are not included.

An isolated Xray 26.2.6 client ran from the existing pinned image digest, with
0.5 CPU, 128 MiB RAM, 64 PID cap, no added public listener, and SOCKS listeners
bound only to loopback. The generated client config was mode 0600 and removed;
the owned container was stopped and removed.

| Xray client profile | Samples | Result | Curl total | HTTP bytes | Interpretation |
|---|---:|---|---:|---:|---|
| Raw private bridge WS to existing Xray inbound | 1 | HTTP 204 | 216.0 ms | 0 | Server-local Xray ingress and subject handoff succeeded. |
| Configured public TLS/WSS profile, certificate verification enabled | 1 | HTTP 204 | 425.2 ms | 0 | Server-local self-dial through public TLS/WSS endpoint succeeded. |

Curl timing measures target HTTPS through the local SOCKS proxy. It does not
separate the outer WSS handshake from the inner target TLS handshake. The raw
WS profile had cumulative DNS/connect/appconnect/TTFB/total values of
0.023/0.149/116.961/215.887/215.998 ms; derived TCP, target-TLS,
request-to-first-byte phases were 0.126/116.812/98.926 ms. Public TLS/WSS had
cumulative values 0.095/0.486/321.824/425.091/425.241 ms and derived phases
0.391/321.338/103.267 ms. The curl appconnect delta is the inner target TLS
phase; outer WSS setup remains included in total/TTFB and cannot be separated
here. One sample per profile does not support a percentile. Neither profile
represents a remote client's WAN-to-router performance.

The existing read-only exact-binding diagnostic also succeeded once per route
class: global-auto HTTP 204 in 272 ms, fixed-route override HTTP 204 in 262 ms.
These establish that the sampled existing handoffs can complete a GET. The
fixed handoff GET is not a full fixed-route remote-client ingress benchmark.

The control agent also verified the dedicated test subject's fixed-route
override and 79/79 Xray runtime state, but the one planned public TLS/WSS data
probe did not start: the public-only client harness waited for an unused raw
SOCKS listener. It exited before issuing a GET and removed its temporary
container/config. Per root direction there was no retry. Therefore the
fixed-route public TLS/WSS client path remains **unmeasured**; the harness
readiness check was corrected for future use. The dedicated client was then
deleted successfully with runtime verification and parity checks; no further
data-plane request used that identity.

### Capped transfer, controller attribution, and ICMP

Two concurrent 262,144-byte downloads were run on each of Direct WAN and
Mihomo 5201. All four returned HTTP 200, for 1 MiB total. The requested
`curl --limit-rate 128K` cap was not enforced on the sub-second Mihomo wave:
curl reported 733,666 and 822,848 B/s per flow, about 1.56 MB/s combined for a
brief sample. This exceeded the requested aggregate 1 MB/s bound. No further
concurrent bulk transfer was run. The result is not a sustained capacity
measurement.

During the Mihomo wave, authenticated controller snapshots matched both curl
source ports and the requested target. Both chains had four hops, contained
`vpn-global` and the same current selected member fingerprint, and had no
DIRECT hop. Direct WAN flows had no matching Mihomo controller records. This
confirms the sampled 5201 downloads used the active global-auto proxy member
for that window. Only fingerprints, boolean classification, and byte counts
were retained; controller data for unrelated connections was discarded.

Direct ICMP to `1.1.1.1` over `enp1s0` sent 5 packets and reported 0% loss;
RTT min/avg/max/mdev was 57.392/57.477/57.598/0.070 ms. This is ICMP-only and
does not establish UDP or TCP loss. Per-flow TCP_INFO retransmissions, UDP
loss, sustained traffic, and remote-client ingress remain unmeasured.

An approved API-restart overlap watcher took one idle baseline pair. Its
32-second marker wait ended before the restart marker was created; no GETs were
sent during restart, so overlap impact is unmeasured. The subsequent approved
restart reached critical readiness 44.614 seconds after restart start, but its
ephemeral marker was removed with the systemd runtime directory before an
extension could observe it. Root stopped further probes; no post-recovery
substitute was run. The baseline pair returned HTTP 204 on both paths: Direct
total 271.753 ms and Mihomo total 213.821 ms. This single pair is context only,
not a comparison. The observer's `systemctl show --value` field ordering was
parsed incorrectly, so API PID/RSS/CPU fields are unavailable; host
memory/load and global TCP counters are retained, with unrelated traffic
caveats. Evidence is in `api_restart_overlap.json` and
`API_DEPLOY_RESTART_RAW.json`.

## Resource and loss observations

During the initial sampling window, the 200 ms `/proc` sampler observed peak
RSS of approximately 148,000 KiB for Mihomo, 42,000 KiB for Xray, and 25,500
KiB for curl. These are sampled process peaks, not benchmark-attributable
increments; existing background work and the observer are included. Available
memory and load samples are retained in the JSON evidence. The sampler's
overhead was not independently calibrated; it performs bounded procfs reads
every 200 ms.

Host-wide TCP `RetransSegs` delta was 0 during the initial window. `EstabResets`
rose by 12 and `OutRsts` by 16, but those kernel counters include unrelated
system traffic; they do not identify these requests or prove a path fault.
Per-flow TCP_INFO retransmission data and UDP loss were not collected.

## Reproduction

Run from this live host as root:

```bash
python3 /srv/fwrouter/knowledge/audits/live_operational_dataplane_2026-10-08/measure_dataplane.py
```

This makes new live requests and replaces `dataplane_measurements.json` with
current output. `measure_attribution.py` records the bounded two-flow transfer
already described above; its observed sub-second rate exceeded the requested
cap, so do not rerun it. `measure_xray_client.py` requires the dedicated private
input and starts an owned temporary container; it has already run and cleaned
up. Neither script stores response bodies or credentials.
