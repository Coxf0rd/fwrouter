(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FwrouterClientListSetting = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const ETAG_PATTERN = /^"cli1-[0-9a-f]{64}"$/;
  const RESOURCE_PATH = "/api/v2/ui/settings/client-list";

  function legacyDisplayPayload(value) {
    const payload = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return Object.fromEntries(Object.entries(payload).filter(([key]) => key !== "show_inactive" && key !== "_show_inactive_contract"));
  }

  function create(options) {
    const opts = options || {};
    const request = opts.fetch || (typeof fetch === "function" ? fetch.bind(globalThis) : null);
    const secureOrigin = opts.secureOrigin !== undefined
      ? Boolean(opts.secureOrigin)
      : (typeof location !== "undefined" && location.protocol === "https:");
    const onChange = typeof opts.onChange === "function" ? opts.onChange : () => {};
    const onSaved = typeof opts.onSaved === "function" ? opts.onSaved : async () => {};
    let token = null;
    let busy = false;
    let epoch = 0;
    let readSequence = 0;
    let current = null;
    let etag = null;
    let resourceMetadata = null;
    let observationConfirmed = false;
    let desired = null;
    let conflict = false;
    let uncertain = false;
    let message = "settings.client_list.loading";

    function snapshot() {
      return Object.freeze({
        busy,
        loaded: observationConfirmed && typeof current === "boolean" && Boolean(etag),
        observationConfirmed,
        current,
        desired,
        credentialPresent: Boolean(token),
        etag,
        compatibilityStatus: resourceMetadata?.compatibility_status || null,
        provenance: resourceMetadata?.provenance || null,
        conflict,
        uncertain,
        message,
      });
    }

    function publish(nextMessage) {
      if (nextMessage) message = nextMessage;
      onChange(snapshot());
      return snapshot();
    }

    function errorStatus(status) {
      if (status === 401) return "settings.client_list.credential_rejected";
      if (status === 403) return "settings.client_list.forbidden";
      if (status === 412) return "settings.client_list.conflict";
      if (status === 428) return "settings.client_list.refresh_required";
      if (status === 422) return "settings.client_list.invalid_value";
      if (status === 409) return "settings.client_list.incompatible_state";
      if (status === 503) return "settings.client_list.busy";
      return "settings.client_list.request_failed";
    }

    async function parseResource(response) {
      const payload = await response.json();
      const resource = payload?.data?.client_list;
      const responseEtag = response.headers?.get?.("ETag");
      if (payload?.ok !== true || !resource || typeof resource.show_inactive !== "boolean" || typeof responseEtag !== "string" || !ETAG_PATTERN.test(responseEtag) || resource.etag !== responseEtag) {
        throw Object.assign(new Error("invalid_resource"), { kind: "invalid_resource" });
      }
      return { resource, etag: responseEtag };
    }

    async function observe(options = {}) {
      if (busy) return snapshot();
      return observeCurrent(options);
    }

    async function observeCurrent({ resetDesired = false } = {}) {
      const readEpoch = epoch;
      const sequence = ++readSequence;
      const isCurrent = () => readEpoch === epoch && sequence === readSequence;
      observationConfirmed = false;
      if (!request) return publish("settings.client_list.request_failed");
      let response;
      try {
        response = await request(RESOURCE_PATH, {
          method: "GET",
          cache: "no-store",
          credentials: "omit",
          redirect: "error",
          headers: etag ? { "If-None-Match": etag } : {},
        });
        if (!isCurrent()) return snapshot();
        if (response.status === 304 && typeof current === "boolean" && etag) {
          if (!ETAG_PATTERN.test(etag) || response.headers?.get?.("ETag") !== etag) {
            return publish("settings.client_list.invalid_response");
          }
          observationConfirmed = true;
          if (resetDesired) desired = current;
          if (resetDesired) {
            conflict = false;
            uncertain = false;
          }
          return publish(uncertain
            ? "settings.client_list.outcome_unknown"
            : (conflict ? "settings.client_list.conflict" : "settings.client_list.ready"));
        }
        if (!response.ok) {
          return publish(errorStatus(response.status));
        }
        const parsed = await parseResource(response);
        if (!isCurrent()) return snapshot();
        current = parsed.resource.show_inactive;
        resourceMetadata = parsed.resource;
        etag = parsed.etag;
        observationConfirmed = true;
        if (desired === null || resetDesired) desired = current;
        if (resetDesired) {
          conflict = false;
          uncertain = false;
        }
        return publish(uncertain
          ? "settings.client_list.outcome_unknown"
          : (conflict ? "settings.client_list.conflict" : "settings.client_list.ready"));
      } catch (error) {
        if (!isCurrent()) return snapshot();
        return publish(error?.kind === "invalid_resource"
          ? "settings.client_list.invalid_response"
          : "settings.client_list.request_failed");
      }
    }

    function lock(nextMessage = "settings.client_list.locked") {
      token = null;
      epoch += 1;
      readSequence += 1;
      if (busy) {
        uncertain = true;
        observationConfirmed = false;
      }
      return publish(nextMessage);
    }

    function unlock(candidate) {
      if (busy) return snapshot();
      const value = String(candidate || "").trim();
      if (!secureOrigin) return publish("settings.client_list.https_required");
      if (!value) return publish("settings.client_list.credential_required");
      token = value;
      return publish("settings.client_list.unverified");
    }

    function setDesired(value) {
      if (busy) return snapshot();
      if (typeof value !== "boolean") return publish("settings.client_list.invalid_value");
      desired = value;
      if (desired !== current) {
        conflict = false;
        if (!uncertain) message = "settings.client_list.pending";
      } else if (!uncertain) {
        message = token ? "settings.client_list.ready" : "settings.client_list.locked";
      }
      onChange(snapshot());
      return snapshot();
    }

    async function save() {
      if (busy) return snapshot();
      if (!token) return publish("settings.client_list.credential_required");
      if (!secureOrigin) return publish("settings.client_list.https_required");
      if (typeof desired !== "boolean") return publish("settings.client_list.invalid_value");
      if (!etag || !observationConfirmed) return publish("settings.client_list.refresh_required");
      const saveEpoch = epoch;
      const isCurrent = () => saveEpoch === epoch;
      busy = true;
      readSequence += 1;
      observationConfirmed = false;
      publish("settings.client_list.saving");
      try {
        let response;
        try {
          response = await request(RESOURCE_PATH, {
            method: "PATCH", cache: "no-store", credentials: "omit", redirect: "error",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "If-Match": etag },
            body: JSON.stringify({ show_inactive: desired }),
          });
        } catch (_) {
          if (!isCurrent()) return snapshot();
          uncertain = true;
          await observeCurrent();
          if (!isCurrent()) return snapshot();
          return publish("settings.client_list.outcome_unknown");
        }
        if (!isCurrent()) return snapshot();
        if (response.status === 401 || response.status === 403) {
          token = null;
          return publish(errorStatus(response.status));
        }
        if (response.status === 412) {
          conflict = true;
          await observeCurrent();
          if (!isCurrent()) return snapshot();
          return publish("settings.client_list.conflict");
        }
        if (!response.ok) return publish(errorStatus(response.status));
        try {
          const parsed = await parseResource(response);
          if (!isCurrent()) return snapshot();
          current = parsed.resource.show_inactive;
          resourceMetadata = parsed.resource;
          desired = current;
          etag = parsed.etag;
          observationConfirmed = true;
        } catch (_) {
          if (!isCurrent()) return snapshot();
          uncertain = true;
          await observeCurrent();
          if (!isCurrent()) return snapshot();
          return publish("settings.client_list.outcome_unknown");
        }
        conflict = false;
        uncertain = false;
        try {
          await onSaved({ show_inactive: current, etag });
        } catch (_) {
          // Owner persistence is confirmed; projection refresh is separate.
        }
        if (!isCurrent()) return snapshot();
        return publish("settings.client_list.saved");
      } finally {
        busy = false;
        publish();
      }
    }

    function useServerValue() {
      if (busy) return snapshot();
      if (!observationConfirmed || typeof current !== "boolean" || !etag) {
        return publish("settings.client_list.refresh_required");
      }
      desired = current;
      conflict = false;
      uncertain = false;
      return publish("settings.client_list.server_value_selected");
    }

    return Object.freeze({ observe, lock, unlock, setDesired, save, useServerValue, state: snapshot });
  }

  function mount(ids, options) {
    if (typeof document === "undefined") return null;
    const nodes = {};
    Object.entries(ids || {}).forEach(([key, id]) => { nodes[key] = document.getElementById(id); });
    if (!nodes.root || !nodes.credential || !nodes.unlock || !nodes.lock || !nodes.checkbox || !nodes.refresh || !nodes.save || !nodes.useServer || !nodes.status || !nodes.current) return null;

    const t = options?.t || ((key) => key);
    const secureOrigin = window.location?.protocol === "https:" && window.isSecureContext === true;
    const render = (state) => {
      nodes.checkbox.checked = state.desired === true;
      nodes.checkbox.disabled = state.busy || !state.loaded || !state.credentialPresent;
      nodes.save.disabled = state.busy || !state.loaded || !state.credentialPresent || (state.desired === state.current && !state.uncertain && !state.conflict);
      nodes.refresh.disabled = state.busy;
      nodes.unlock.disabled = state.busy;
      nodes.unlock.hidden = state.credentialPresent;
      nodes.lock.hidden = !state.credentialPresent;
      nodes.credential.disabled = state.busy || state.credentialPresent;
      nodes.current.textContent = state.loaded
        ? t(state.current ? "settings.client_list.value_enabled" : "settings.client_list.value_disabled")
        : t("settings.client_list.current_unknown");
      nodes.status.textContent = t(state.message);
      nodes.status.dataset.state = state.conflict ? "conflict" : (state.uncertain ? "unknown" : "normal");
      nodes.useServer.hidden = !state.observationConfirmed || (!state.conflict && !state.uncertain && state.desired === state.current);
      nodes.useServer.disabled = state.busy || !state.observationConfirmed;
      nodes.root.dataset.authorization = state.credentialPresent ? "entered_unverified" : "locked";
    };
    const controller = create({ ...(options || {}), secureOrigin, onChange: render });
    nodes.unlock.addEventListener("click", () => {
      const result = controller.unlock(nodes.credential.value);
      nodes.credential.value = "";
      render(result);
    });
    nodes.lock.addEventListener("click", () => controller.lock());
    nodes.checkbox.addEventListener("change", () => controller.setDesired(Boolean(nodes.checkbox.checked)));
    nodes.refresh.addEventListener("click", () => { void controller.observe(); });
    nodes.save.addEventListener("click", () => { void controller.save(); });
    nodes.useServer.addEventListener("click", () => controller.useServerValue());
    window.addEventListener("pagehide", () => controller.lock());
    window.addEventListener("beforeunload", () => controller.lock());
    window.addEventListener("hashchange", () => controller.lock());
    window.addEventListener("popstate", () => controller.lock());
    document.addEventListener("fwrouter:view", (event) => {
      if (event?.detail?.view !== "settings") controller.lock();
    });
    document.addEventListener("fwrouter:locale", () => render(controller.state()));
    render(controller.state());
    void controller.observe({ resetDesired: true });
    return controller;
  }

  return Object.freeze({ create, mount, legacyDisplayPayload, RESOURCE_PATH });
});
