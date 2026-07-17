// External IP probing helpers used by the user view.
(function () {
  const DEFAULT_IP_CHECK_URL = "https://api.ipify.org?format=json";
  const DEFAULT_IP_CHECK_TIMEOUT_MS = 4500;

  function setText(id, txt) {
    const node = document.getElementById(id);
    if (!node) return;
    const value = txt || "";
    node.textContent = value;
    if (node.classList.contains("pill")) node.hidden = !value;
  }

  function extractIp(text) {
    const source = String(text || "");
    const ipv4 = source.match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/);
    if (ipv4) return ipv4[0];
    const ipv6 = source.match(/\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b/i);
    if (ipv6) return ipv6[0];
    return "";
  }

  function withCacheBust(url, enabled) {
    const base = String(url || "").trim();
    if (!enabled || !base) return base;
    const sep = base.includes("?") ? "&" : "?";
    return `${base}${sep}_ts=${Date.now()}`;
  }

  function normalizeIpCheckUrl(value) {
    const raw = String(value || "").trim();
    if (!raw) return DEFAULT_IP_CHECK_URL;
    if (/generate_204(?:$|[?#])/i.test(raw)) return DEFAULT_IP_CHECK_URL;
    return raw;
  }

  async function loadClientExternalIp(url, targetId, opts) {
    const options = opts || {};
    const currentText = (document.getElementById(targetId)?.textContent || "").trim();
    const currentShown = currentText === "—" ? "" : currentText;
    const timeoutMs = Math.max(1000, Number(options.timeoutMs || DEFAULT_IP_CHECK_TIMEOUT_MS));
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);

    try {
      const targetBase = normalizeIpCheckUrl(url);
      const target = withCacheBust(targetBase, Boolean(options.cacheBust));
      const response = await fetch(target, { cache: "no-store", signal: controller.signal });
      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      let ip = "";

      if (contentType.includes("application/json")) {
        const json = await response.json().catch(() => ({}));
        ip = String(json.ip || json.ipString || json.query || json.origin || json.address || "").trim();
        if (!ip) ip = extractIp(JSON.stringify(json));
      } else {
        const body = await response.text().catch(() => "");
        ip = extractIp(body);
      }

      const resolved = ip || (options.keepCurrentOnFail === false ? "" : (currentShown || ""));
      setText(targetId, resolved);
      return resolved;
    } catch (_) {
      const resolved = options.keepCurrentOnFail === false ? "" : (currentShown || "");
      setText(targetId, resolved);
      return resolved;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function loadClientExternalIpPair(cfg, opts) {
    const options = opts || {};
    const conf = cfg || {};
    const directUrl = normalizeIpCheckUrl(conf.ip_check_direct_url || conf.url);
    const vpnUrl = normalizeIpCheckUrl(conf.ip_check_vpn_url || conf.url);

    if (options.useBackendFallback === true && options.preferBackend === true) {
      const backendPair = await loadBackendExternalIpPair();
      if (backendPair.directIp || backendPair.vpnIp) return backendPair;
    }

    const [directIp, vpnIp] = await Promise.all([
      loadClientExternalIp(directUrl, "serverCurrentIpDirect", {
        cacheBust: Boolean(options.cacheBust),
        keepCurrentOnFail: options.keepCurrentOnFail !== false,
        timeoutMs: Number(conf.timeout_ms || options.timeoutMs || DEFAULT_IP_CHECK_TIMEOUT_MS),
      }),
      loadClientExternalIp(vpnUrl, "serverCurrentIpVpn", {
        cacheBust: Boolean(options.cacheBust),
        keepCurrentOnFail: options.keepCurrentOnFail !== false,
        timeoutMs: Number(conf.timeout_ms || options.timeoutMs || DEFAULT_IP_CHECK_TIMEOUT_MS),
      }),
    ]);

    if (directIp || vpnIp) return { directIp, vpnIp };
    if (options.useBackendFallback !== true) return { directIp: "", vpnIp: "" };

    return loadBackendExternalIpPair();
  }

  async function loadBackendExternalIpPair() {
    try {
      const data = await window.FwrouterUI.fetchApiV2("/ui/external-ip", { cache: "no-store" });
      const fallbackCurrentIp = String(data.current_ip || data.ip || "").trim();
      const fallbackVpnIp = String(data.vpn_ip || "").trim();
      if (fallbackCurrentIp || fallbackVpnIp) {
        setText("serverCurrentIpDirect", fallbackCurrentIp);
        setText("serverCurrentIpVpn", fallbackVpnIp || fallbackCurrentIp);
        return {
          directIp: fallbackCurrentIp,
          vpnIp: fallbackVpnIp || fallbackCurrentIp,
        };
      }
    } catch (_) {
      // Keep empty values when the backend fallback is unavailable.
    }

    return { directIp: "", vpnIp: "" };
  }

  window.FwrouterIpCheck = {
    extractIp,
    loadClientExternalIpPair,
  };
})();
