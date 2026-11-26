import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../..', import.meta.url))
const server = await createServer({
  root,
  configFile: false,
  appType: 'custom',
  server: { middlewareMode: true, ws: false },
})
after(() => server.close())
const { isTerminalMediaPollingError, mediaStatusRetryDelay } = await server.ssrLoadModule('/src/components/mediaStatusPolling.ts')
const { ApiError } = await server.ssrLoadModule('/src/api/client.ts')

test('media status polling stops for terminal access errors and honors explicit retryability', () => {
  assert.equal(isTerminalMediaPollingError(new ApiError(403, { code: 'ACCESS_DENIED' })), true)
  assert.equal(isTerminalMediaPollingError(new ApiError(404, { code: 'NOT_FOUND' })), true)
  assert.equal(isTerminalMediaPollingError(new ApiError(503, { retryable: false })), true)
  assert.equal(isTerminalMediaPollingError(new ApiError(503, { retryable: true })), false)
})

test('temporary media status failures get bounded exponential retry delays', () => {
  const temporary = new ApiError(503, {})
  assert.deepEqual([0, 1, 2, 3].map((retries) => mediaStatusRetryDelay(temporary, retries)), [1_000, 2_000, 4_000, 8_000])
  assert.equal(mediaStatusRetryDelay(temporary, 4), null)
  assert.equal(mediaStatusRetryDelay(new TypeError('network error'), 0), 1_000)
  assert.equal(mediaStatusRetryDelay(new ApiError(403, { code: 'ACCESS_DENIED' }), 0), null)
})
