# Scheduled refresh input-fence correction

## Scope and ownership

Branch `fix/scheduled-refresh-input-fence` starts from actual main29295706b97e63ea050727227e5b909282e47147 after PR30. This task does not authorize merge. Persistent subscription/provider intent remains SQLite-owned; existing generation publication owns revisions/checkpoints and native proof. Core alone owns host routing/selection. No provider-side mutation, forced VPN change, direct SQL write or manual verified/checkpoint edit is part of this work.

## Evidence and causal diagnosis

The scheduled job1684e94e-06fc-446b-b00f-d730aae69336 ran2026-10-21T16:00:30Z–16:00:58Z and failed `MIHOMO_STAGE_INPUTS_CHANGED_AFTER_VALIDATION` during Xray generation staging. It left candidate12 pending, verified11/digest369a65c8a2aa9218da117d01a25c5b3760c5cafda2c1343a4a9d6ca30ecd292c retained, with no active generation checkpoint. Thus the failure occurs before publication checkpoint ownership, not because of an abandoned recovery owner.

Docker-event inventory jobs ran at16:00:45Z,48Z,51Z–54Z and56Z–57Z. At16:00:57Z inventory created a docker subject for imageab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529, independently resolved to the installed pinned metacubex/mihomo:v1.19.31. Its network mode was none; it had no IP or server override and is now inactive. The native validator uses `docker run --rm --network none --read-only`; inventory observes its transient lifecycle. The existing fingerprint selected every subjects row, including this irrelevant inventory. The actual renderer requires an applicable override and routable subject. A validator's own observable container can therefore invalidate its candidate fence without changing generated routing.

The historic job did not retain pre/post fingerprint categories; its exact full delta cannot be reconstructed or claimed directly measured. The persisted validator subject/time/image/network plus the traced fingerprint/renderer mismatch identify the concrete causal scenario for deterministic regression. Other input-change causes are not asserted absent from all historical jobs. Standalone native validation, shadow rendering/local validation and full shadow Xray/Mihomo staging passed with stable category hashes when inventory did not overlap the validator window.

A subsequent canonical API `subscription_refresh` job9e0ece60-2ea4-4704-8bce-3f36e7b3f7e6 used the scheduled payload and succeeded with applied/promoted/runtime_verified=true. Safe sampling saw expected inventory digest and servers.inventory_state changes before staging and base-config changes at apply. It recovered pending state normally to verified revision13, digest a396549d20b0c8e6ce55ecf97640248f9de7dec67010fa836bd610967f97d060; a single success does not prove the source correction. The initial diagnostic HTTP client timed out at25 seconds, while the same job continued to successful completion at71 seconds; no duplicate job or cancellation was issued.

## Correction and acceptance gates

Limit ordinary subject-route fingerprint inputs to subjects with an existing server or user-mode override, while retaining explicit Xray/vless subjects even without an override, retaining conservative activation/deletion/address/override dependencies and independent explicit-Xray-client inputs. Version the changed fingerprint contract. Never ignore actual route changes, source/base/file changes or invalid/missing context. Safe diagnostics contain category/version/hash values only, not fingerprint payloads, URLs, credentials or subject inventories. No retry may reuse an invalidated candidate.

Source, meaningful regressions, independent QA, exact-HEAD mandatory CI/native receipts, full installer `--deploy --all`, normal lifecycle/source transfer, protected-host preservation and repeated canonical scheduled refresh/live acceptance are separate gates. At initial recording they remain pending. A verified backup read/hash-checked2837 regular archive files and restored three critical files plus SQLite26 with integrity/FK checks; no production restore occurred. The known local traffic-collector readiness guard must be preserved exactly across full host installation before worker/timer resume.

Private evidence under `/root/fwrouter-recovery/refresh-fence-20261022/`: safe context probes, historical validator metadata, scheduled job receipt, backups. Secret-bearing runtime configs and job payloads stay private and are not Git artifacts. External-device FWRouter/Rescue acceptance remains separate from server-originated probes.

## Frozen source and local regression checkpoint

The bounded package versions the fingerprint to3, includes renderer-used provider_ip and filters unowned non-Xray/non-vless subjects without either server/user override record. Conservative ownership/activation/address fields and explicit-Xray inputs remain. It adds category/subcategory SHA256 fence diagnostics and a stable localized RU/EN event. No retry or candidate acceptance rule is weakened. Writer checks passed12 affected fingerprint/profile-generation tests plus7 staged-candidate/exclusive-source/localization tests and Ruff on seven changed Python files. An initial invalid test basetemp argument failed before executing tests; the corrected isolated run passed. Independent QA and full exact-HEAD CI/native/deployment/live acceptance remain pending at this checkpoint.

## Independent QA checkpoint

Frozen source review PASSED. Three full affected files passed307 tests in87.86 seconds; one subprocess-lock test was explicitly deselected locally because it belongs to qualified hosted acceptance, not counted as PASS. Staged-candidate/exclusive-source files passed22 tests. Ruff and diff checks passed. Review traced renderer, reconcile/provenance, staged generation, source/exclusive selection consumers and explicit Xray inputs; no material ownership/fencing/security defect remained. Native qualified CI, deployment and repeated post-install live refresh remain pending. Source hashes and raw isolated output are private evidence, not a live proof.

## Final-review correction before deployment

Root review found that the initial742eda2 filter also removed no-override Xray/vless activity/deletion/address dependencies. The separate explicit-client fingerprint category did not include is_active/is_deleted, while collect_xray_runtime_bindings consumes both flags. The earlier local checks and QA PASS did not cover that boundary. CI39651749828 was cancelled and is not counted as qualifying; this candidate was never installed. The correction retains all implementation_kind=xray or subject_role=vless_client rows in the existing subject_routes category in addition to server/user override subjects. New no-override Xray activation/deletion regressions and independent recheck are required before final qualification.

The corrected frozen candidate passed independent recheck:13 focused cross-consumer/staged-generation tests, Ruff and diff checks. It explicitly preserves no-override Xray/vless subjects in subject_routes and adds direct binding-projection/activation/deletion regressions. The earlier QA PASS is superseded for qualification purposes; cancelled742eda2 CI is not used. The final corrected source requires a new exact-HEAD mandatory CI before installation.
