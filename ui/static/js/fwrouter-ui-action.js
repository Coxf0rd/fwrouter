// Explicit UI action lifecycle wrapper for FWRouter pages.
(function () {
  const STATES = Object.freeze({
    IDLE: "IDLE",
    RUNNING: "RUNNING",
    SUCCESS: "SUCCESS",
    FAILED: "FAILED",
    TIMEOUT: "TIMEOUT",
  });

  const DEFAULT_RESULT_FLASH_MS = 4500;
  const DEFAULT_RESULT_ICON_MS = 30000;

  function ui() {
    return window.FwrouterUI || {};
  }

  function t(key, params) {
    return window.FwrouterI18n?.t(key, params) || key;
  }

  function asNode(value) {
    if (!value) return null;
    if (typeof value === "string") return document.getElementById(value);
    return value;
  }

  function uniqueNodes(values) {
    const result = [];
    const seen = new Set();
    (Array.isArray(values) ? values : [values]).forEach((value) => {
      const node = asNode(value);
      if (!node || seen.has(node)) return;
      seen.add(node);
      result.push(node);
    });
    return result;
  }

  function createActionTargets(options) {
    const opts = options || {};
    const button = asNode(opts.button);
    const scope = asNode(opts.scope);
    const resultTarget = asNode(opts.resultTarget) || button || scope;
    const indicator = asNode(opts.indicator);
    const messageTarget = asNode(opts.messageTarget);
    const disable = uniqueNodes([...(Array.isArray(opts.disable) ? opts.disable : []), button]);

    return {
      button,
      scope,
      resultTarget,
      indicator,
      messageTarget,
      disable,
    };
  }

  function setTargetState(targets, state) {
    [targets.button, targets.scope, targets.resultTarget, targets.indicator, targets.messageTarget]
      .filter(Boolean)
      .forEach((node) => {
        node.dataset.actionState = state;
      });
  }

  function clearTargetState(targets) {
    [targets.button, targets.scope, targets.resultTarget, targets.indicator, targets.messageTarget]
      .filter(Boolean)
      .forEach((node) => {
        delete node.dataset.actionState;
      });
  }

  function normalizeMessageSpec(spec, params) {
    if (!spec) return null;
    if (typeof spec === "string") return { key: spec, params: params || {} };
    if (typeof spec === "object") {
      return {
        key: String(spec.key || ""),
        params: { ...(params || {}), ...(spec.params || {}) },
      };
    }
    return null;
  }

  function setMessage(targets, spec, params) {
    const normalized = normalizeMessageSpec(spec, params);
    if (!normalized || !normalized.key || !targets.messageTarget) return;

    if (targets.messageTarget.id && ui().setDynamicStatus) {
      ui().setDynamicStatus(targets.messageTarget.id, normalized.key, normalized.params);
      return;
    }

    targets.messageTarget.textContent = t(normalized.key, normalized.params);
  }

  function setPlainMessage(targets, key, params) {
    if (!key || !targets.messageTarget) return;
    if (targets.messageTarget.id && ui().setText) {
      ui().setText(targets.messageTarget.id, t(key, params || {}));
      return;
    }
    targets.messageTarget.textContent = t(key, params || {});
  }

  function clearMessage(targets) {
    if (!targets.messageTarget) return;
    if (targets.messageTarget.id && ui().setText) {
      ui().setText(targets.messageTarget.id, "");
      return;
    }
    targets.messageTarget.textContent = "";
  }

  function clearResultTimers(node) {
    if (!node) return;
    window.clearTimeout(Number(node.dataset.actionResultFlashTimer || 0));
    window.clearTimeout(Number(node.dataset.actionResultIconTimer || 0));
    delete node.dataset.actionResultFlashTimer;
    delete node.dataset.actionResultIconTimer;
  }

  function clearResultClasses(node) {
    if (!node) return;
    clearResultTimers(node);
    node.classList.remove("is-success-scope", "is-error-scope", "has-result-icon");
    node.removeAttribute("data-result-icon");
  }

  function setPending(targets, pending) {
    if (ui().setPendingStateMany) {
      ui().setPendingStateMany(targets.disable, pending);
    } else if (ui().setPendingState) {
      targets.disable.forEach((node) => ui().setPendingState(node, pending));
    } else {
      targets.disable.forEach((node) => {
        node.disabled = Boolean(pending);
        node.classList.toggle("is-pending", Boolean(pending));
        if (pending) node.setAttribute("aria-busy", "true");
        else node.removeAttribute("aria-busy");
      });
    }

    if (targets.scope) {
      targets.scope.classList.toggle("is-pending-scope", Boolean(pending));
      targets.scope.setAttribute("aria-busy", pending ? "true" : "false");
    }

    if (targets.indicator) {
      targets.indicator.classList.toggle("is-pending", Boolean(pending));
      targets.indicator.setAttribute("aria-busy", pending ? "true" : "false");
    }
  }

  function flashResult(targets, state, options) {
    const target = targets.resultTarget;
    if (!target) return;

    const opts = options || {};
    const success = state === STATES.SUCCESS;
    const failed = state === STATES.FAILED || state === STATES.TIMEOUT;
    if (!success && !failed) return;

    const resultFlashMs = Number(opts.resultFlashMs || DEFAULT_RESULT_FLASH_MS);
    const resultIconMs = Number(opts.resultIconMs || DEFAULT_RESULT_ICON_MS);

    clearResultClasses(target);
    target.dataset.resultIcon = success ? "\u2713" : "\u00d7";
    target.classList.add("has-result-icon");
    target.classList.add(success ? "is-success-scope" : "is-error-scope");
    target.dataset.actionResultFlashTimer = String(window.setTimeout(() => {
      target.classList.remove("is-success-scope", "is-error-scope");
      delete target.dataset.actionResultFlashTimer;
    }, resultFlashMs));
    target.dataset.actionResultIconTimer = String(window.setTimeout(() => {
      target.classList.remove("has-result-icon");
      target.removeAttribute("data-result-icon");
      delete target.dataset.actionResultIconTimer;
    }, resultIconMs));
  }

  function startAction(targetsOrOptions, options) {
    const targets = createActionTargets(targetsOrOptions);
    const opts = options || targetsOrOptions || {};
    setTargetState(targets, STATES.RUNNING);
    clearResultClasses(targets.resultTarget);
    setPending(targets, true);
    setMessage(targets, opts.pendingMessage);
    return targets;
  }

  function finishAction(targetsOrOptions, result, options) {
    const targets = targetsOrOptions?.disable ? targetsOrOptions : createActionTargets(targetsOrOptions);
    const opts = options || {};
    setPending(targets, false);
    setTargetState(targets, STATES.SUCCESS);
    if (opts.successMessage) setMessage(targets, opts.successMessage, { result });
    flashResult(targets, STATES.SUCCESS, opts);
    return { state: STATES.SUCCESS, result };
  }

  function isTimeoutError(error) {
    const message = String(error?.message || "");
    return (
      error?.code === "ACTION_TIMEOUT" ||
      message === t("job.timeout") ||
      message === t("state.apply_unconfirmed")
    );
  }

  function failAction(targetsOrOptions, error, options) {
    const targets = targetsOrOptions?.disable ? targetsOrOptions : createActionTargets(targetsOrOptions);
    const opts = options || {};
    const state = isTimeoutError(error) ? STATES.TIMEOUT : STATES.FAILED;
    const message = ui().actionMessage ? ui().actionMessage(error) : ui().translateBackendMessage?.(error?.message) || String(error?.message || "");

    setPending(targets, false);
    setTargetState(targets, state);
    if (state === STATES.TIMEOUT && opts.timeoutMessage) {
      setMessage(targets, opts.timeoutMessage, { message });
    } else if (opts.failedMessage) {
      setMessage(targets, opts.failedMessage, { message });
    } else if (message) {
      setPlainMessage(targets, "status.error_prefix", { message });
    }
    flashResult(targets, state, opts);
    return { state, error };
  }

  function resetAction(targetsOrOptions, options) {
    const targets = targetsOrOptions?.disable ? targetsOrOptions : createActionTargets(targetsOrOptions);
    const opts = options || {};
    setPending(targets, false);
    clearTargetState(targets);
    if (opts.clearMessage) clearMessage(targets);
    if (opts.clearResult !== false) clearResultClasses(targets.resultTarget);
    return { state: STATES.IDLE };
  }

  async function runAction(options) {
    const opts = options || {};
    const targets = startAction(opts, opts);
    let response;
    let jobResult;
    let confirmResult;
    let refreshResult;

    try {
      if (typeof opts.action !== "function") {
        throw new TypeError("FwrouterUIAction.runAction requires action callback");
      }

      response = await opts.action();

      const jobId = typeof opts.job === "function"
        ? String(opts.job(response) || "").trim()
        : String(response?.job?.job_id || "").trim();
      if (jobId && ui().pollJob) {
        jobResult = await ui().pollJob(jobId, {
          timeoutMs: opts.jobTimeoutMs,
          delayMs: opts.jobDelayMs,
          onProgress(status, job) {
            if (typeof opts.onProgress === "function") {
              const progressMessage = opts.onProgress(status, job);
              if (progressMessage) setMessage(targets, progressMessage);
            }
          },
        });
      }

      if (typeof opts.confirm === "function") {
        confirmResult = await opts.confirm(response, jobResult);
      }

      if (typeof opts.refresh === "function") {
        refreshResult = await opts.refresh(response, jobResult, confirmResult);
        if (refreshResult?.resultTarget) {
          targets.resultTarget = asNode(refreshResult.resultTarget) || targets.resultTarget;
        }
      }

      const final = finishAction(targets, { response, jobResult, confirmResult, refreshResult }, opts);
      return { ...final, response, jobResult, confirmResult, refreshResult };
    } catch (error) {
      const failed = failAction(targets, error, opts);
      throw Object.assign(error, { actionState: failed.state });
    } finally {
      setPending(targets, false);
    }
  }

  window.FwrouterUIAction = {
    STATES,
    runAction,
    startAction,
    finishAction,
    failAction,
    resetAction,
    createActionTargets,
  };
})();
