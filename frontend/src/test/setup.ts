// jsdom can expose an opaque origin in some Node/Vitest combinations, which
// leaves localStorage undefined. Keep storage-backed tests deterministic.
if (typeof globalThis.localStorage === 'undefined') {
  const values = new Map<string, string>()
  const localStorageShim: Storage = {
    get length() { return values.size },
    clear() { values.clear() },
    getItem(key) { return values.get(String(key)) ?? null },
    key(index) { return [...values.keys()][index] ?? null },
    removeItem(key) { values.delete(String(key)) },
    setItem(key, value) { values.set(String(key), String(value)) },
  }
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: localStorageShim,
  })
}
