# UI

Static FWRouter operator/user interface.

## Monorepo Role

- `index.html`
- CSS under `static/css/`
- browser logic under `static/js/`
- country flags and UI images under `static/`
- deployed UI path: `/opt/fwrouter-ui`

## Contract

- The UI talks to the backend API.
- It does not store source-of-truth state.
- It should stay static and deployable by file copy unless the project intentionally adds a build step.

## Runtime Model

- UI is served as a static site from `/opt/fwrouter-ui`.
- Browser requests are handled by live backend routes under `/api/v2`.
- Nginx/Nginx Proxy Manager may serve the static files and reverse proxy API calls, but source-of-truth state remains in the backend.

## Historical Nginx transport example — trusted management boundary required

This retained example shows transport wiring, **not a secure standalone or
public deployment configuration**. `listen 5500` binds all local interfaces;
`server_name _` is not access control. The broad `/api/v2/` proxy reaches the
management listener, whose ordinary endpoints have no global HTTP
authentication. A scoped API-client credential does not restrict that whole
listener. The example is unsafe for untrusted reachability unless a separately
enforced trusted management boundary protects it.

Before using a UI/API proxy, its owner must establish the actual caller and
network/proxy access boundary. The source installer does not install this
snippet; no deployed Nginx/NPM mapping was inspected or changed by this note.
Public management exposure needs a separately reviewed security policy. The
subscription Gateway's narrow allowlist does not protect this broad UI proxy.
See the [current endpoint/admission matrix](../knowledge/audits/stage6_1_package_g_2026-11-03/ENDPOINT_AUTHORITY_MATRIX.md)
and [security boundaries](../knowledge/PROJECT_MAP/SECURITY_AND_PERMISSIONS.md).
The original example below is preserved as history, not a recommendation to
expose it without those prerequisites.

```nginx
server {
    listen 5500;
    server_name _;

    root /opt/fwrouter-ui;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/v2/ {
        proxy_pass http://127.0.0.1:5000/api/v2/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Notes

- LAN/Tailscale display names are saved through backend subject alias APIs.
- VPN subscription URL is server-backed; when the backend redacts the stored URL, the UI shows that the subscription is saved on the server.
- VPN-auto rows use `server_id` for actions and display the logical topology health summary; member details are loaded by the explicit logical-server API. Servers with `vpn_auto_priority < 0` remain manual/global-only and are not shown in the user VPN-auto picker.
