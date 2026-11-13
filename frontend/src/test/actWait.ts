import { act } from 'react'

/** Poll an async UI assertion while keeping React state updates inside act(). */
export async function waitForAct(
  assertion: () => void | Promise<void>,
  timeoutMs = 1000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let lastError: unknown
  do {
    try {
      await act(async () => { await assertion() })
      return
    } catch (error) {
      lastError = error
    }
    await act(async () => {
      await new Promise<void>(resolve => setTimeout(resolve, 20))
    })
  } while (Date.now() < deadline)
  throw lastError
}
