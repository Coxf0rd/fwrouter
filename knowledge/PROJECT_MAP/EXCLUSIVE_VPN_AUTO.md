# Exclusive subscription for VPN-auto

The operator may select one saved subscription as the exclusive global VPN-auto source. This is separate persistent intent from provider-managed mode. It is stored as one stable source reference in the existing SQLite settings store; replacing it is atomic. Clearing it restores the ordinary global Auto pool without rewriting server preferences.

Eligibility follows canonical entry/member ownership to subscription source. An ordinary exclusive source contributes its eligible logical servers. An enabled provider-managed source contributes only its logical provider root; its internal members remain candidates within that root. Retained provider-managed ordinary entries remain informational legacy entries.

The restriction applies to the global VPN-auto selector and generated VPN-auto group, including startup selection and watchdog. It does not remove ordinary fixed targets from Xray or change Global/VPN-auto semantics. Admin reflects Auto exclusion separately from full target availability: excluded ordinary entries retain names, visibility, Health and fixed routing, while their Auto/priority controls are disabled. Provider legacy rows retain their stronger managed/nonselectable status.

Settings reads and UI render/tab/locale changes are read-only. Applying exclusive intent uses existing jobs, shared writer guard, common generated configuration validation/apply and exact selector readback. Existing provider data is sufficient; exclusive enable does not discover, mutate, or switch a provider member. A failed or unconfirmed apply must preserve last-good runtime and expose the pending persistent intent honestly.

Confirmed provider outage keeps the exclusive and provider intent, uses the existing bounded provider recovery, then verified Emergency Direct after exhaustion. There is no fallback to another subscription. Verified VPN re-entry uses the existing state machine. Tests exercise failures and recovery in isolated mocks, never by forcing a production outage.

Source/Tests/Commit/Deploy/Live evidence belongs in the canonical roadmap and the dated acceptance report. Native validation is distinct from endpoint handshake verification.

Inventory `vpn_auto=True` retains its existing stored-preference membership filter. It is not a promise of effective eligibility. The `auto_eligible` projection, selector candidate IDs and generated VPN-auto targets define the effective pool. `vpn_auto_excluded` identifies Auto-only restrictions even for currently-off ordinary preferences; `selectable` independently retains fixed-target availability.
