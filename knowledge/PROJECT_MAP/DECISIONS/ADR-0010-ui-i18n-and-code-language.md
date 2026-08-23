# ADR-0010: UI i18n And Code Language

## Status

Accepted

## Context

FWRouter is operated primarily in Russian, but the source code must stay maintainable for tooling and future contributors. Before this decision, user-facing Russian text was mixed into UI controllers, labels, and backend-message adapters. That made UI cleanup risky and caused English backend messages to leak into the interface when a translation was missing.

## Decision

- Source code identifiers, comments, and implementation notes use English.
- Comments stay short and are added only when they clarify non-obvious behavior.
- User-facing UI text is addressed through `FwrouterI18n` keys.
- Russian remains the default UI locale.
- English entries are kept in the same dictionary for development, review, and future locale switching.
- Backend status or error messages shown in the UI pass through `translateBackendMessage`, backed by the same dictionary.

## Consequences

- New UI strings should be added as i18n keys instead of inline literals.
- Existing inline literals can be migrated incrementally when the owning module changes.
- API response contracts stay language-neutral; localization belongs to the frontend boundary.
- Documentation in this repository stays English, while the shared local-server documentation tree carries the synchronized operator knowledge map.
