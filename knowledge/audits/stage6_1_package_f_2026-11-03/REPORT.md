# Stage 6.1 Package F — Application Lifecycle & State Ownership

Status: historical source acceptance; no merge or deployment is implied. Current PR qualification and remaining gates are tracked in the owner-local canonical roadmap.

The application lifecycle coordinates startup/shutdown around existing process owners; scheduler policies remain module-owned, JobManager registration is manager-owned, and prewarm remains a distinct worker. Cache keys, bounds, TTL, invalidation, identity fencing, and freshness semantics remain unchanged; same-thread same-key recursion now fails fast and leader `BaseException` outcomes reach waiting followers.

Single-process/single-state-root operation remains the supported boundary. A nonreturning custom loader can still leave followers waiting; no timeout or stale-value fallback was added. See [architecture map](../../PROJECT_MAP/ARCHITECTURE.md), [finding dispositions](../stage6_1_package_g_2026-11-03/FINDING_DISPOSITIONS.md) for the maintained contracts; execution history remains in Git.
