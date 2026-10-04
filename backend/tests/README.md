# Backend tests

Follow the [Test Architecture and CI/CD Foundation](../../knowledge/PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md) for test levels, affected-test selection, isolation, baseline classification and durable evidence. Routine fixes run L0 plus the affected L1/L2 tests; the full suite is reserved for milestone, nightly, release or major shared-contract gates. Tests must not touch production state or runtime unless explicitly designated as live acceptance.
