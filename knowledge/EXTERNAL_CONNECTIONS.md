# External Connections

External connections are user-managed systems that FWRouter can display, call, or use without owning their lifecycle. Add them in UI: `Settings -> Подключения -> Добавить подключение`.

## Connection Types

- `external_management`
  External automation calls FWRouter API. It does not carry traffic.
- `external_vpn_module`
  External VPN/runtime provides transparent egress endpoints. FWRouter may use it as VPN dataplane adapter only when it is alive.
- `external_network_source`
  External system describes client inventory, interface, or CIDR. This is registration/display today; provider-specific inventory wiring is separate backend work.

## External Management API

Use this for Home Assistant, scripts, bots, dashboards, or any client that changes FWRouter intent.

A UI-created record gets stable identity values:

```json
{
  "external_system_id": "<system-id>",
  "requested_by": "external_client:<system-id>",
  "collector": "external_connection:<system-id>"
}
```

Custom records may also declare `replacement_target`:

- `mihomo`: the external VPN module is intended to replace the managed Mihomo VPN dataplane. This is active when the module has working transparent TCP redir and UDP TProxy endpoints.
- `xray`: the external runtime is registered as an Xray-like explicit client replacement contract. FWRouter does not automatically proxy the built-in `/xray/*` API to it without a dedicated compatible adapter.

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
  "controller_url": "http://127.0.0.1:9090"
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
  "requested_by": "external_client:<system-id>",
  "collector": "external_connection:<system-id>",
  "samples": [
    {
      "counter_key": "<system-id>:<subject-id>:vpn",
      "subject_id": "<existing-fwrouter-subject-id>",
      "path": "vpn",
      "rx_bytes": 0,
      "tx_bytes": 0,
      "metadata": {
        "external_system_id": "<system-id>",
        "connection_type": "external_vpn_module",
        "source": "external_runtime_api"
      }
    }
  ]
}
```

The backend validates `metadata.external_system_id` against `Settings -> Подключения`. Unknown records are rejected; `external_management` records cannot submit traffic samples.

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
client_inventory_url=http://127.0.0.1:8080/clients, interface_name=tailscale0, client_cidr=100.64.0.0/10, healthcheck_url=http://127.0.0.1:8080/health
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
