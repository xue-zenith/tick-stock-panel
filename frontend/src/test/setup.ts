// jsdom can expose an opaque origin in some Node/Vitest combinations, which
// leaves localStorage undefined. Keep storage-backed tests deterministic.
// React 18 requires this marker when tests use createRoot directly. Setting it
// once in the shared setup keeps every test file on the same act contract.
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

// Framer Motion may call scrollTo while measuring keyframes. jsdom exposes the
// method but intentionally throws; a no-op is the correct test-environment
// contract because scrolling is outside the component behavior under test.
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'scrollTo', { configurable: true, value: () => {} })
}

// Reading Node 25+'s unconfigured localStorage getter emits an experimental
// warning. Inspect the descriptor instead, then install the deterministic shim
// without touching the getter.
const nativeStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')?.value
if (!nativeStorage) {
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
