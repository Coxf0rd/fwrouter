# Ruff Complex Imports, Definitions and Contracts — Package 2

## Authority and source boundary

The user explicitly authorized continuing Package2 in the same open PR21 without waiting for Package1 merge. This supersedes the template's new-branch/merged-preflight requirements; no separate branch/PR is created. Starting source is `5b0793447edbb870e978544e33dac7867cf3e7fd`, qualified by requiredPRCI39079703330(1894PASS/0FAIL/0SKIP) and independentQA. Main remains4f0c43ce with successful actualmainCI39074290982. No merge, production inspection/deploy/recovery/restart, realDB/credentials or native/network changes are authorized or performed.

## Baseline and ownership findings

Same441tracked maintained Python files, pinnedRuff0.17.0,rootconfiguration,F/E9,--ignore-noqa,--no-cache:707findings. F401683,F4035,F4051,F5412,F8112,F84114. Private/untracked evidence and38historical knowledge scripts are excluded as before; no config or suppression changes.

All five wildcard imports are from the pure orchestration constants module into the central facade and four split implementation modules. Split modules need small explicit dependency sets; the central facade also owns public mutation exports and late-bound injection attributes used by handlers and monkeypatch seams. Unused import syntax alone therefore does not prove a name can be removed. The remaining F841/F811 audit found no new confirmed runtime defect; required validation/setup/refresh/native-assert calls must remain. The L7 post-delete rows read is an unresolved assertion-contract question and is left untouched.

## First grouped implementation and qualification limits

The initial18file package replaces wildcard dependencies, removes audited redundant bindings/read projections and duplicateimports, and qualifies genuine module-level reexports explicitly. Central/public interfaces, original native/CAS/fencing/recovery behavior and allrequiredcalls must remain unchanged. actual broad isolated exact run selected303nodes and produced296PASS/7FAIL/0SKIP. Seven failures are permission/capability denials(temp-path/subprocess) in that runner, retained as NOTQUALIFIED rather than PASS; no isolation weakening. Separate import/behavior targeted group7PASS and realXrayreconcile1PASS provide scoped evidence. FullrequiredhostedCI remains necessary and does not claim everyunselectedtestfunction passed.

## Independent review correction

IndependentQA returned CHANGES_REQUIRED for a material compatibility defect in the first candidate:17existingpublic facade attrs disappeared and the proposed__all__ both narrowed publicwildcardexports and exposedunderscorehelpers. The subset contract test didnotdetectthis. Root rejectedbreakingfacadechanges in a linttask. The correction must restore exactbaselinepublicmoduleattrs/from-star surface while preserving private directinjectionattrs without addingthemto wildcardAPI. Testfirst exactnamespace/objectidentity evidence must bindthis to baseline5b07934. Originalfreeze/reviewevidence is preserved; newcandidate requiresnewhashes/review andCI. No intermediatePASS is reused.

## Final qualification

Pending correctedsourcefreeze, independentreview, exactHEADmandatoryCI andreceiptinspection. Finalrun/PR/SHA/resourcecounts will be recorded in localFINAL_HANDOFF andthePR after qualification, preserving this pre-run sourcecheckpoint. HistoricalPackage1reports remainunchangedexcept their append-only continuation history. No nextroadmapstage starts.

## Corrected frozen source acceptance before hosted CI

The facade correction restores the exact88publicname surface reconstructed independently from5b07934. Actualfrom-star imports export exactlythose88names;32privatehelpers remain directmoduleattrs forlateboundconsumers butarenotaddedtowildcardAPI. IndependentASTconsumer sweepfound86referencedattrs and0missing. Sourcecontracts andobjectidentity/importorder tests pass. Thereareno breakingpublicAPI changes authorized bythislinttask.

Independentcorrectedreview verified18/18hashes, all18changedfiles strictF/E9, fulltest_apply_orchestrator5PASS/0FAIL/0SKIP, directattrs/star namespace andsideeffects. Priorbroad303node run retains296PASS/7profiledenialsNOTQUALIFIED; finalhostedrequiredCIwillqualifyitsdeclaredplanonly. Sideeffectcalls retained:validation,providerconfiguration,packetcapabilityrefresh, nativeassertions, terminalevents/logs. RemovedXraytemporarymap isunused; runtimefilesystemstat/HApostwaitread values didnot contributeoutputorproof. Theseare source-observed redundancy improvements, notmeasuredproduction savings.

Finalbaseline707→386: F401683→382(301resolved,includinglegitimateexports explicitlycontracted),F4035→0,F4051→0,F8112→1,F84114→1,F5412unchanged. Total321resolved inthisextension; consolidatedoriginal951→386(565). Thisdoesnotclaim382remainingF401areallunused; centralfacade/publiccompatibilityworkremains. OneF811mihomo_configshadowedimportretainedbecausecontainingfilehasotherAPIdebt; oneF841L7rowreadretainedpendingexpectedpostdelete/recoverycontract; twoF541remaininfileswithambiguousimports. Allremainingfindingsarelistedinthelinkedregister; no blanketignore,noqa,tool/version/resourcechanges.

The finalexactHEADrequiredCI, source/plan/profile/native/JUnitcleanup/resourceverification is stillpending atthistrackedpre-runcheckpoint. FinalrunmetadatawillbeaddedtolocalFINAL_HANDOFFandPRbodywithoutadocumentation-onlyCIloop. No production/runtimechanges, useralonemerges.
