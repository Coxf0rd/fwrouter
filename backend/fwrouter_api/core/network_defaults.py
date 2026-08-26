from __future__ import annotations


DEFAULT_PROTECTED_IPV4_NETWORKS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "224.0.0.0/4",
)
DEFAULT_PROTECTED_IPV6_NETWORKS = (
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
DEFAULT_RULES_EXTRA_PROTECTED_NETWORKS = (
    "0.0.0.0/8",
    "240.0.0.0/4",
)
DEFAULT_TRUSTED_CLIENT_IPV4_NETWORKS = (
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "192.168.0.0/16",
)
DEFAULT_TRUSTED_CLIENT_IPV6_NETWORKS = (
    "fc00::/7",
    "fe80::/10",
)
DEFAULT_LAN_INTERFACE_DENY_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "tailscale",
    "virbr",
    "lo",
)
DEFAULT_LOCAL_LAN_HOSTS = {
    "fwrouter.lan": "FWRouter UI via local ingress",
    "homes.lan": "Home Assistant via local ingress",
}
