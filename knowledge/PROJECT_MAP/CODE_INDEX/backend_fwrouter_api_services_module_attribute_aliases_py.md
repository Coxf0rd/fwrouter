# `backend/fwrouter_api/services/module_attribute_aliases.py`

## Purpose and boundary

A small compatibility-only module attribute adapter preserves established
`setattr`/monkeypatch/restore paths while implementation modules import their
collaborators directly. It is not an orchestration framework, dependency
container, configuration registry, persistent state owner or runtime API.

`install_attribute_aliases()` attaches an explicit alias table after a public
compatibility module has initialized. `attribute_alias_view()` creates an
unregistered narrow view for an existing compatibility attribute. Reads use the
primary consumer binding; assignments/restoration update only declared local
bindings. The helper does not scan loaded modules, discover collaborators,
execute operations or mutate source-owner modules automatically.

## Supported consumers

- Apply and Mihomo public compatibility surfaces declare their own alias maps.
- The Xray coordinator declares historical private collaborator patch points;
  canonical planning, observation, view and staging functions remain independent.
- `mihomo_reconcile.config` retains the known historical compatibility lookups.
- Implementation operation paths do not read public facades or these views.

Keep maps limited to actual historical consumers; copying every similarly named
owner binding would broaden monkeypatch effects and conceal ownership. Public
exports and object identity are separate compatibility contracts, tested along
with dispatch/failure behavior.

## Deliberate limitations

Direct writes into `module.__dict__` bypass Python attribute hooks and are not a
supported patch/configuration mechanism; no such supported consumer was found.
Multiple assignments are sequential, not an atomic hot-patch transaction. This
does not introduce a supported concurrent runtime reconfiguration mechanism.
The compatibility facade eagerly wires its declared consumers before installing
legacy aliases, so patching before first dispatch still works. Import startup
cost has not been measured; no performance benefit is claimed.

No native publication, CAS/fencing, SQLite, credential, audit or recovery owner is
moved here. New module consumers must use canonical internal owner interfaces.
