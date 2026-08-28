# External Connections

External connections are user-managed systems that FWRouter can display, call, or use without owning their lifecycle. Add them in UI through `Settings -> Connections -> Add connection`.

Connections are persisted in the `external_connections` registry table. `settings.custom_external_systems` is a compatibility response/legacy-migration shape, not a storage source for new writes; saving ordinary display settings must not create or delete registered connections. Each connection has a stable `connection_id`; `system_id` remains a compatibility/display identifier.

## Developer Workflow

Use this flow when you add a new external service, client, or runtime to a FWRouter installation.

1. Create a UI record in `Settings -> Connections`.
   The record is the stable contract anchor. Pick the role first: management API client, VPN module, network source, or display-only entry.
2. Copy the generated JSON contract from the connection details modal or call:

   ```text
   GET /api/v2/ui/external-connections/<connection-id>/contract
   ```

3. Configure the external system manually using the contract values:
   `connection_id`, `requested_by`, `collector`, endpoints, capabilities, and collector rules.
4. Decide how FWRouter receives state:
   use `api_push` when the external system can call FWRouter on changes, `http_poll`/`file_read`/`command_probe` only when FWRouter must fetch state itself.
5. Test collection without applying traffic:

   ```text
   POST /api/v2/ui/external-connections/<connection-id>/collect
   Body: {"dry_run": true}
   ```

6. Apply mutable changes through the connection details modal or:

   ```text
   PATCH /api/v2/ui/external-connections/<connection-id>
   ```

   If the type or replacement target is wrong, delete and recreate the record. Those fields define the integration contract.

The external system is still owned by the developer or operator. FWRouter does not install it, restart it, reload it, or rewrite its config unless a dedicated managed runtime exists for that module.

## Connection Types

- `external_management`
  External automation calls FWRouter API. It does not carry traffic. Many management clients may be registered. Logs update last-seen/action context, but logs are not the source of truth.
- `external_vpn_module`
  External VPN/runtime provides transparent egress endpoints. FWRouter may use it as VPN dataplane adapter only when it is alive. Only one active external VPN module may own one replacement target such as `mihomo` or `xray`.
- `external_network_source`
  External system describes client inventory, interface, or CIDR. Multiple connections of the same provider are allowed; their IDs, collector state, generated state, and imported subject prefixes must stay independent.
- `display_only`
  External object shown in the admin panel without API or dataplane behavior.

`external` and `custom` mean the same lifecycle in this context: the user owns the runtime. `custom` means a persisted registry connection. Provider contracts such as Tailscale may be registered in `fwrouter_api.services.external_provider_registry`, but a concrete connection instance should exist only after UI/API creation or a one-time migration of an existing installation.

## Runtime And Generated State

The persistent registry is the only source of concrete external connection instances. Provider contracts are capabilities; they do not create runtime state by themselves. Generic subject taxonomy exposes only generic external subject classes such as `external_network_client` and `explicit_external_client`; concrete provider fields stay in provider registry contracts, adapter/parser code, or subject metadata/detail.

Per-connection generated state lives in `external_connection_generated_state` and is keyed only by `connection_id`. Creating or updating a connection regenerates that row idempotently. Deleting a connection cascade-deletes the generated row, clears that connection's collector scheduler state, and clears only live-probe cache entries scoped to that `connection_id`.

External ingress inventory is also instance-scoped. A provider discovery request is resolved to enabled `external_network_source` connections with matching `runtime_type`; if no such connection exists, FWRouter returns a warning and does not run a provider probe or import subjects. Imported external-ingress subjects use a `connection_id`-scoped subject prefix and store `metadata.connection_id`, so two connections of the same provider do not share subjects or stale-state decisions.

Saving UI display settings, including visibility, must not create, remove, or overwrite `external_connections`. Settings responses may include `custom_external_systems` for compatibility, but that shape is derived from the registry.

## Data Delivery

Each connection can declare `integration_mode`:

- `api_push`
  The external system sends updates to FWRouter API when its state changes. The backend does not poll. This is the default.
- `http_poll`
  FWRouter manually or periodically reads JSON from an HTTP endpoint.
- `command_probe`
  FWRouter runs an allowlisted `script_id` without shell. Arbitrary commands from UI are not allowed.
- `file_read`
  FWRouter reads a JSON file below `/var/lib/fwrouter-v2/external-collectors/`.

`refresh_mode` controls when collection runs:

- `on_change` - external push only, no background polling.
- `manual` - collector runs only through API/UI manual refresh.
- `interval` - backend scheduler runs the collector by `collector_config.interval_seconds`. Minimum is 30 seconds, default is 300 seconds. Successful ticks are not logged; failures are deduped.

Manual check:

```text
POST /api/v2/ui/external-connections/<connection-id>/collect
Body: {"dry_run": true}
```

Collector execution rules:

- `api_push` always uses `refresh_mode=on_change`; the backend does not poll it.
- `manual` runs only from UI/API.
- `interval` is checked by the backend scheduler. The collector's own `interval_seconds` is clamped to 30..86400 seconds and defaults to 300 seconds.
- Hidden custom connections are skipped by the interval scheduler.
- Collector responses are limited to 256 KiB and must be JSON object or JSON list.
- `file_read` can read only under `/var/lib/fwrouter-v2/external-collectors/`.
- `command_probe` runs only an allowlisted `script_id`; arbitrary shell commands from UI are not accepted.
- Traffic samples are applied only when `collector_config.apply_traffic=true` and the collection run is not `dry_run`.

## Field Reference

Common accepted fields:

```json
{
  "system_id": "external-vpn-provider-a",
  "connection_id": "external-vpn-provider-a",
  "label": "External VPN Provider A",
  "connection_type": "external_vpn_module",
  "location": "host",
  "address": "127.0.0.1",
  "runtime_type": "provider-a",
  "replacement_target": "mihomo",
  "integration_mode": "api_push",
  "refresh_mode": "on_change",
  "endpoints": {},
  "capabilities": {},
  "collector_config": {}
}
```

Allowed `location`: `docker`, `host`, `ip`, `manual`.

Allowed `replacement_target`: empty, `mihomo`, `xray`. Use `mihomo` for a transparent VPN dataplane replacement. Use `xray` only for the explicit-client runtime contract; full automatic replacement of every built-in Xray management route still requires compatible adapter code.

Allowed `endpoints` keys:

```text
controller_url, http_proxy_url, socks_proxy_url, tcp_redir_port, udp_tproxy_port,
full_tcp_redir_port, full_udp_tproxy_port, healthcheck_url, selector_state_url,
selector_failover_url, client_inventory_url, subscription_base_url, traffic_stats_url,
client_api_url, reload_url, interface_name, client_cidr
```

Allowed `capabilities` keys:

```text
supports_tcp, supports_udp, supports_transparent_proxy, supports_http_proxy,
supports_socks_proxy, supports_selector_api, supports_client_inventory,
supports_client_api, supports_subscription_api, supports_traffic_stats,
supports_reload
```

For `external_vpn_module`, watchdog uses an external selector API only when `capabilities.supports_selector_api=true`, `endpoints.selector_state_url`, and `endpoints.selector_failover_url` are all configured. `selector_state_url` must return a JSON object with `selection_mode`/`mode` (`auto` or `manual`) and `active_target_id`/`active_server_id`. `selector_failover_url` accepts POST JSON with `apply`, `reason`, `requested_by`, `exclude_target_id`, `candidate_limit`, `timeout_ms` and returns `ok`, `applied`, `active_after` or `selected_target_id`.

Allowed `collector_config` base keys:

```text
interval_seconds, timeout_seconds, apply_traffic, trigger
```

Mode-specific `collector_config` keys:

```text
http_poll: url, status_url, data_url
command_probe: script_id, extra_args
file_read: path
api_push: no mode-specific keys
```

Immutable after creation: `connection_id`, `connection_type`, `replacement_target`.

Editable after creation: `label`, `location`, `address`, `runtime_type`, `endpoints`, `capabilities`, `integration_mode`, `refresh_mode`, `collector_config`, `description`.

## Registration And Updates

Ask the backend to validate and normalize a draft before saving it:

```text
POST /api/v2/ui/external-connections/preview
```

Example body:

```json
{
  "label": "Headscale",
  "connection_type": "external_network_source",
  "location": "host",
  "runtime_type": "headscale",
  "integration_mode": "http_poll",
  "refresh_mode": "interval",
  "collector_config": {
    "url": "http://127.0.0.1:8080/status",
    "interval_seconds": 300,
    "timeout_seconds": 5,
    "apply_traffic": false
  }
}
```

External management client example:

```json
{
  "connection_id": "home-assistant",
  "system_id": "home-assistant",
  "label": "Home Assistant",
  "connection_type": "external_management",
  "location": "ip",
  "address": "http://192.168.1.20:8123",
  "runtime_type": "home-assistant",
  "integration_mode": "api_push",
  "refresh_mode": "on_change",
  "endpoints": {
    "controller_url": "http://192.168.1.20:8123"
  },
  "capabilities": {
    "supports_selector_api": true
  },
  "collector_config": {
    "interval_seconds": 300,
    "timeout_seconds": 5,
    "apply_traffic": false
  }
}
```

External VPN module example:

```json
{
  "connection_id": "sing-box-runtime",
  "system_id": "sing-box-runtime",
  "label": "Sing-box runtime",
  "connection_type": "external_vpn_module",
  "location": "host",
  "address": "127.0.0.1",
  "runtime_type": "sing-box",
  "replacement_target": "mihomo",
  "integration_mode": "api_push",
  "refresh_mode": "on_change",
  "endpoints": {
    "tcp_redir_port": "16080",
    "udp_tproxy_port": "16081",
    "healthcheck_url": "http://127.0.0.1:9090/health",
    "selector_state_url": "http://127.0.0.1:9090/selector",
    "selector_failover_url": "http://127.0.0.1:9090/selector/failover"
  },
  "capabilities": {
    "supports_tcp": true,
    "supports_udp": true,
    "supports_transparent_proxy": true,
    "supports_traffic_stats": true
  },
  "collector_config": {
    "interval_seconds": 300,
    "timeout_seconds": 5,
    "apply_traffic": false
  }
}
```

External network source example:

```json
{
  "connection_id": "headscale",
  "system_id": "headscale",
  "label": "Headscale",
  "connection_type": "external_network_source",
  "location": "host",
  "address": "127.0.0.1:8080",
  "runtime_type": "headscale",
  "integration_mode": "http_poll",
  "refresh_mode": "interval",
  "endpoints": {
    "client_inventory_url": "http://127.0.0.1:8080/clients",
    "healthcheck_url": "http://127.0.0.1:8080/health"
  },
  "capabilities": {
    "supports_client_inventory": true
  },
  "collector_config": {
    "url": "http://127.0.0.1:8080/status",
    "interval_seconds": 300,
    "timeout_seconds": 5,
    "apply_traffic": false
  }
}
```

Save:

```text
PUT /api/v2/ui/external-connections/<connection-id>
```

Patch allowed fields:

```text
PATCH /api/v2/ui/external-connections/<connection-id>
```

After creation, `connection_id`, `connection_type`, and `replacement_target` are immutable because they define the contract. `system_id` is a compatibility/display identifier and must not be used as identity for new integrations. Delete and recreate the record to change immutable fields. Editable fields are label, `system_id`, location/address, runtime_type, endpoints, capabilities, integration/refresh mode, and collector_config. Rejected payloads return `ok=false` with field-level details in `error.fields`.

Delete a custom record:

```text
DELETE /api/v2/ui/external-connections/<connection-id>
```

Auto-discovered records, such as a discovered external network source, are not deleted through this endpoint. Hide them in UI; they disappear when the underlying runtime data disappears.

Validation failure shape:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_EXTERNAL_CONNECTION",
    "message": "External connection payload failed validation.",
    "fields": {
      "collector_config.url": "required"
    }
  }
}
```

Collector accepts a JSON object or list. Universal object shape:

```json
{
  "status": "ok",
  "details": {},
  "clients": [
    {
      "id": "stable-client-id",
      "label": "Laptop",
      "address": "100.64.1.20",
      "metadata": {}
    }
  ],
  "traffic_samples": [
    {
      "counter_key": "external:client:vpn",
      "subject_id": "lan:aa-bb",
      "path": "vpn",
      "rx_bytes": 0,
      "tx_bytes": 0,
      "metadata": {}
    }
  ]
}
```

In this pass the collector applies traffic samples only when `collector_config.apply_traffic=true` and the run is not `dry_run`. Automatic import of new `subjects` from `clients` is intentionally not enabled yet: it affects routing and needs a separate explicit provider contract.

## External Management API

Use this for Home Assistant, scripts, bots, dashboards, or any client that changes FWRouter intent.

A UI-created record gets stable identity values:

```json
{
  "connection_id": "<connection-id>",
  "requested_by": "external_client:<connection-id>",
  "collector": "external_connection:<connection-id>"
}
```

Custom records may also declare `replacement_target`:

- `mihomo`: the external VPN module is intended to replace the managed Mihomo VPN dataplane. This is active when the module has working transparent TCP redir and UDP TProxy endpoints.
- `xray`: the external runtime is registered as an explicit-client runtime replacement contract. This is a generic role marker for a user-managed client core; FWRouter exposes the JSON contract, identity and traffic accounting hooks, but does not automatically proxy built-in `/xray/*` API calls to it without a dedicated compatible adapter.

The full JSON contract for a registered UI/API-created record is available through:

```text
GET /api/v2/ui/external-connections/<connection-id>/contract
```

Required attribution for management API calls:

```json
{
  "requested_by": "external_client:my-automation",
  "management_context": {
    "source_type": "external_client",
    "client_name": "my-automation",
    "channel": "local_api",
    "action": "set_global_mode:vpn",
    "actor": "operator"
  }
}
```

Useful endpoints:

- `POST /api/v2/routing/global`
  Body: `{"mode":"direct|selective|vpn", ...attribution}`
- `POST /api/v2/selector/vpn-auto/switch`
  Body: `{"confirm_switch":true, ...attribution}`
- `POST /api/v2/routing/global/fixed-server`
  Body: `{"server_id":"<server-id>", "confirm_switch":true, ...attribution}`
- `DELETE /api/v2/routing/global/fixed-server?confirm_switch=true&requested_by=external_client:<name>&management_client_name=<name>&management_action=<action>`

See `EXTERNAL_MANAGEMENT.md` for curl examples.

## External VPN Module Contract

Use this when an external runtime should replace managed Mihomo as VPN egress. FWRouter does not install, restart, reload, or configure that runtime.

Required endpoints:

```json
{
  "tcp_redir_port": "16080",
  "udp_tproxy_port": "16081"
}
```

Optional endpoints:

```json
{
  "full_tcp_redir_port": "16082",
  "full_udp_tproxy_port": "16083",
  "healthcheck_url": "http://127.0.0.1:9090/health",
  "controller_url": "http://127.0.0.1:9090",
  "client_inventory_url": "http://127.0.0.1:9090/clients",
  "subscription_base_url": "http://127.0.0.1:9090/sub",
  "traffic_stats_url": "http://127.0.0.1:9090/stats",
  "reload_url": "http://127.0.0.1:9090/reload"
}
```

Activation rules:

- the UI connection type must be `external_vpn_module`;
- the connection must be visible;
- `tcp_redir_port` and `udp_tproxy_port` must be present;
- if `healthcheck_url` is set, it must return HTTP success and JSON status like `ok`, `ready`, `running`, `healthy`, or `degraded`;
- if `healthcheck_url` is absent, FWRouter checks that `127.0.0.1:<tcp_redir_port>` accepts TCP connections.

HTTP/SOCKS fields such as `http_proxy_url` and `socks_proxy_url` may be documented in the JSON contract, but the transparent nft dataplane does not use them.

An external VPN module does not have to be Mihomo or Xray. FWRouter treats it as the `external_vpn_module` role: when it provides transparent TCP redirect and UDP TProxy endpoints, the backend can use it as a VPN egress adapter without provider-specific code.

If the external runtime reports traffic accounting itself, the sample should be bound to the UI record:

```json
{
  "requested_by": "external_client:<connection-id>",
  "collector": "external_connection:<connection-id>",
  "samples": [
    {
      "counter_key": "<connection-id>:<subject-id>:vpn",
      "subject_id": "<existing-fwrouter-subject-id>",
      "path": "vpn",
      "rx_bytes": 0,
      "tx_bytes": 0,
      "metadata": {
        "connection_id": "<connection-id>",
        "connection_type": "external_vpn_module",
        "source": "external_runtime_api"
      }
    }
  ]
}
```

The backend validates `metadata.connection_id` against `Settings -> Connections`. Unknown records are rejected; `external_management` records cannot submit traffic samples. Legacy `external_system_id` may still appear in stored/enriched metadata for compatibility, but it is not accepted as input identity.

Traffic accounting samples from external systems are not watchdog health signals by default. If an external VPN module reports its own response counter as fallback evidence, the sample metadata must explicitly declare the role:

```json
{
  "metadata": {
    "watchdog_signal": "adapter_response",
    "connection_type": "external_vpn_module"
  }
}
```

External management clients and external network sources must not send this role.

Watchdog uses the active VPN dataplane adapter, not a hardcoded runtime name. When a ready `external_vpn_module` is the active adapter, watchdog can treat its authoritative traffic samples as runtime evidence and will not call Mihomo selector APIs. Automatic server failover is still adapter-specific: managed Mihomo can switch `vpn-auto`; an external runtime needs its own compatible failover/selector adapter before FWRouter can change nodes inside that external system. Without that adapter, a confirmed traffic stall is logged as `WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE` instead of trying to switch Mihomo.

For `replacement_target=xray`, the contract exposes an `explicit_client_runtime` block. It describes expected optional endpoints such as `client_inventory_url`, `subscription_base_url`, `traffic_stats_url` and `reload_url`. These fields let a developer wire a compatible external client core consciously; FWRouter still needs adapter code before it can replace every built-in Xray management route automatically.

Example UI endpoints line:

```text
tcp_redir_port=16080, udp_tproxy_port=16081, full_tcp_redir_port=16082, full_udp_tproxy_port=16083, healthcheck_url=http://127.0.0.1:9090/health
```

Expected healthcheck response:

```json
{
  "status": "ok",
  "runtime_type": "sing-box",
  "selected_node": "auto",
  "version": "1.x"
}
```

## External Network Source

Use this for systems that should describe clients or networks to FWRouter.

Supported registration fields:

```text
client_inventory_url=http://127.0.0.1:8080/clients, interface_name=wg0, client_cidr=100.64.0.0/10, healthcheck_url=http://127.0.0.1:8080/health
```

Suggested inventory response:

```json
{
  "status": "ok",
  "clients": [
    {
      "id": "stable-client-id",
      "label": "Laptop",
      "address": "100.64.1.20",
      "metadata": {}
    }
  ]
}
```

This registers the source and shows the contract in UI. Making a new source produce real FWRouter subjects still requires backend provider wiring.
