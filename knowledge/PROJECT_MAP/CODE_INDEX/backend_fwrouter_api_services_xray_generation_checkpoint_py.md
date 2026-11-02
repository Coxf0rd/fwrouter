# `backend/fwrouter_api/services/xray_generation_checkpoint.py`

## Responsibility

Read contracts for generation checkpoints and the existing admission fence
value object. Checkpoint decoding retains the established ownership, mode,
link, size, inode and schema checks, including fail-closed unknown modes. This
module does not write, archive, advance or remove a checkpoint.

The frozen fence view preserves its fields and `__dict__`/dataclass replacement
interface. Its dictionary-valued snapshots remain shallow values as before;
this extraction does not introduce deep immutability or a new proof format.

## Authority boundary

The coordinator retains operation admission, phase transitions, source and
selection fencing, native readback/publication, last-good and forward recovery.
A parsed checkpoint or constructed view is not proof of ownership/readiness.
Pure checkpoint-bound selection/projection helpers only interpret supplied
values; they do not acquire a generation or publish state.

The coordinator retains historical read symbols for supported private callers
and test patch seams. Native observer consumers that rely on those seams keep
the narrow compatibility boundary instead of gaining a second owner.

## Verification

Disposable parser/value tests and generation recovery suites exercise malformed
and supported modes, file safety, selection fallback and frozen view behavior.
Cold imports are tested separately from execution and native acceptance.

`build_native_validation_proof` constructs the unchanged Xray/Mihomo receipt-hash projection from supplied staged values. Checkpoint IO and phase ownership remain in the coordinator.
