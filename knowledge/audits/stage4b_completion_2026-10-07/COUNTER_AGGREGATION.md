# Transparent nft Counter Read Aggregation

Scope: one read-only replacement for five sequential nftables chain listings in `inspect_transparent_path_counters()`. Source baseline under review: `49591c2`. No nftables/runtime state was changed.

## Measurement

Commands were run serially against the current owned table, with three samples each:

```text
nft -a -nn list chain inet fwrouter_v2 <chain>
```

The old bundle invokes that command once for each of five required chains. Native-call bundle times were `289.2 ms`, `286.2 ms`, and `305.7 ms` (median `289.2 ms`, max `305.7 ms`); combined stdout was `6,448 bytes` per bundle.

```text
nft -t -a -nn -j list table inet fwrouter_v2
```

The new single-call times were `70.1 ms`, `66.3 ms`, and `68.1 ms` (median `68.1 ms`, max `70.1 ms`); stdout was `98,130 bytes` each time. The terse table response is larger than the five chain responses, but completed in about one quarter of their measured time. The new full projection function, including JSON parsing and aggregation, measured `80.1 ms`, `80.3 ms`, and `79.3 ms` (median `80.1 ms`, max `80.3 ms`) with exactly one nft invocation per run. These are three serial host measurements, not a load test; counter values can naturally change between invocations.

An observed projection SHA-256 was `18e296d94b618175f1b97c4b72d0f9358c4eeb065b7721c8d7fee515219b37b8` (27 output keys). It is a point-in-time runtime fingerprint, not a stable expected value.

## Semantic equivalence and fallback

The existing projection's line parser was kept as a fallback. A deterministic fixture feeds the same counter values and ordered comments to table and chain representations; the complete projections compare equal. It covers:

- comment-prefix substring matching, including text before the prefix;
- duplicate matching comments, retaining the first matching comment with a counter;
- a matching comment without a counter, which remains zero unless a later matching rule has a counter;
- malformed counter fields and malformed expressions, falling back only for that chain;
- a valid table response missing a required chain, falling back only for that chain;
- unavailable or malformed table output, falling back to the five per-chain reads;
- failed fallback reads, which retain zero counters.

No timeout was added: the existing per-chain fallback also has no timeout, so changing only the table call's timeout would create inconsistent behavior.

## Test evidence

- Before the final malformed-counter case was added, the complete existing `test_dataplane_global.py` file passed: `19 passed`.
- Focused counter suite: `4 passed, 16 deselected`; the malformed-field node was then extended to cover non-integer and negative packet/byte values and rerun alone: `1 passed, 19 deselected`.
- `py_compile`, targeted `git diff --check`, and `installer/check-clean-tree-surface.sh`: PASS.
- No L6/L7 suite was run.

## Boundary

This is source/test evidence only. The change is not deployed by this audit artifact. Health/readback semantics and the two-second existing status cache are unchanged.
