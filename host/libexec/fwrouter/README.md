# FWRouter Libexec

This directory stores backend-owned contracts for dataplane enforcement, scoped egress, boot preflight, and host-side collectors.

Scripts here define the interface between the control plane and the host enforcement/collector layer:

- `dataplane-check.sh`
- `dataplane-apply.sh`
- `dataplane-rollback.sh`
- `dataplane-common.sh`
- `traffic-collect.sh`
- `traffic-collect-api.sh`
- `fwrouter-boot-preflight.sh`
- `fwrouter-wait-port.sh`
- `fwrouter-xray-sub-gateway.py`

The backend generates manifest-based apply artifacts and expects the host layer to read those JSON manifests and return structured results.

`traffic-collect.sh` defines the traffic-accounting contract: it returns structured JSON counter samples, while the backend stores snapshots, computes deltas, and aggregates `traffic_monthly`.
