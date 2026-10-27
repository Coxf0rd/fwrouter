# Host API client registry

File: `backend/fwrouter_api/services/api_client_registry.py`

Owner: fixed protected host policy and the existing stable lease, pending
publication, atomic replacement/readback, selector/fence and explicit forward
recovery primitives. Registry contents remain outside SQLite. The API's
`selector_mode()` remains strict: a missing selector never admits HTTP callers.

Origin validation accepts only exact canonical HTTPS forms with syntactically
valid lowercase DNS labels or canonical IP literals. It does not normalize,
resolve DNS, or contact a host. Root-admin `set-origin` uses the existing
exclusive lease and expected policy revision, and is limited to an absent
selector and absent permanent fence. A changed origin increments one revision;
a same-origin operation is a no-op. Client/credential records are retained.

Origin publication uses the established durable pending target protocol with a
distinct `registry_origin` purpose. Explicit recovery still requires the
selector and fence to be absent, including when target bytes already match the
prepared policy. Normal registry credential updates retain their existing
post-cutover recovery path. Any pending purpose blocks HTTP admission.

See [the contract](../../../docs/API_CLIENT_REGISTRY.md) for root-admin
bootstrap, credential custody, cutover ordering, supported limits and
source-only qualification boundaries.
