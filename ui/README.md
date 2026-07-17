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

## Suggested Nginx Shape

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
