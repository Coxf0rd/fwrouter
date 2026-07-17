## Project Knowledge Map

Before making non-trivial changes, read:

- `решения/README.md`
- `решения/QUICK_START_FOR_AGENTS.md`
- relevant files inside `решения/CODE_INDEX/`
- relevant architecture files inside `решения/`

The `решения/` directory is the persistent project knowledge map. Keep it synchronized with the code.

## Source vs Live Deployment

Work in the git source tree at `/srv/fwrouter`.

Do not treat live paths as the source of truth:

- `/opt/fwrouter-*`
- `/etc/systemd/system/fwrouter-*`
- `/usr/local/libexec/fwrouter`
- `/usr/local/sbin/fwrouter-*`

After changing source files, commit the git version first, then deploy the affected component into live paths with `/srv/fwrouter/installer/install.sh`.

Examples:

```bash
cd /srv/fwrouter
/srv/fwrouter/installer/check-clean-tree-surface.sh
git status
sudo /srv/fwrouter/installer/install.sh --component backend
sudo systemctl restart fwrouter-api.service
```

For UI-only changes deploy `--component ui`; for systemd/libexec/sbin changes deploy `--component host` and run `systemctl daemon-reload` when unit files changed.

When changing code, configs, systemd units, nftables logic, policy routing, install scripts, API, CLI, mihomo/xray integration, or boot behavior, update only the affected documentation files in `решения/`.

Do not rewrite the whole documentation unnecessarily. Update it точечно.
