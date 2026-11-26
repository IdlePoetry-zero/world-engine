import { ApiError } from '../api/client'

export const MEDIA_STATUS_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000] as const

export function isTerminalMediaPollingError(error: unknown) {
  if (!(error instanceof ApiError)) return false
  if (error.retryable === true) return false
  if (error.retryable === false) return true
  if (error.status < 400 || error.status >= 500) return false
  return ![408, 425, 429].includes(error.status)
}

export function mediaStatusRetryDelay(error: unknown, retriesUsed: number): number | null {
  if (isTerminalMediaPollingError(error)) return null
  return MEDIA_STATUS_RETRY_DELAYS_MS[retriesUsed] ?? null
}
