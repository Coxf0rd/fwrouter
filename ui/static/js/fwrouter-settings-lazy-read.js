// Bounded cache for opt-in Settings reads. No request runs until read() is called.
(function () {
  function createLazyReadCache({ limit = 40, ttlMs = 60000 } = {}) {
    const entries = new Map();
    function touch(key, entry) {
      entries.delete(key);
      entries.set(key, entry);
      while (entries.size > limit) entries.delete(entries.keys().next().value);
      return entry;
    }
    return {
      peek(key) { return entries.get(String(key)); },
      shouldReadOnDisclosure(key) { return !entries.get(String(key))?.error; },
      clear() { entries.clear(); },
      read(key, loader, force = false) {
        const normalized = String(key);
        let entry = entries.get(normalized);
        if (!force && entry?.promise) return entry.promise;
        if (!force && entry?.loadedAt && Date.now() - entry.loadedAt < ttlMs) return Promise.resolve(entry.value);
        entry = touch(normalized, entry || { value: null, loadedAt: 0, promise: null });
        entry.promise = Promise.resolve().then(loader).then((value) => {
          entry.value = value;
          entry.loadedAt = Date.now();
          return value;
        }).finally(() => { entry.promise = null; });
        return entry.promise;
      },
    };
  }
  window.FwrouterSettingsLazyRead = { createLazyReadCache };
})();
