# Home Assistant integration

FWRouter owns the small Home Assistant package and action client under this
directory. Home Assistant remains an external runtime: its container lifecycle,
configuration validation, reload, and restart are not managed by FWRouter.

`packages/fwrouter_control.yaml` reads the existing routing, selector, and
`/api/v2/ui/router-summary` projections. It keeps logical IDs as machine
attributes while displaying `current_server_name`; applied Direct takes
precedence over retained fixed/auto intent. Auto selection provenance is
exposed from the existing summary projection.

`scripts/fwrouter_action.py best` consumes the nested selector result. It
accepts only a confirmed `selected` or explicit `noop` outcome, verifies the
exact active logical ID and current effective route, and reports the operation
reason/source and caller-attribution status separately from the effective-route
change state. A selected auto logical server may coexist with an unchanged
Direct or fixed effective route; this is reported as such, not as an effective
route change.

Deploy only these files with:

```bash
installer/install.sh --deploy --component homeassistant
```

Then validate the mounted config with
`docker exec homeassistant python -m homeassistant --script check_config --config /config`.
Restart Home Assistant separately to load package changes. The installer does
not contact, reload, or restart the external service.
