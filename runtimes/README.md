# Runtimes

Docker runtime wrappers used by FWRouter.

## Components

- `mihomo/` - primary transparent egress runtime.
- `xray/` - subscription/client proxy runtime.

Runtime containers are operational dependencies. Their generated configs and traffic/accounting state are stored under `/var/lib/fwrouter-v2`, not in git.

