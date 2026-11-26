import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createRequire } from 'node:module'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../..', import.meta.url))
const frontendRequire = createRequire(new URL('../../package.json', import.meta.url))
const React = frontendRequire('react')
const server = await createServer({
  root,
  configFile: false,
  appType: 'custom',
  server: { middlewareMode: true, ws: false },
})
after(() => server.close())

const { ApiError } = await server.ssrLoadModule('/src/api/client.ts')
const { canEditFailedMessage, canRetryFailedMessageFromReceipt, conversationErrorCode, isBranchBusyConflict, MESSAGE_RECEIPT_AUTO_POLL_LIMIT, shouldContinueMessageReceiptPolling } = await server.ssrLoadModule('/src/components/messageRecovery.ts')
const { RuntimeProfileStatus } = await server.ssrLoadModule('/src/components/RuntimeProfileStatus.tsx')
const { renderToStaticMarkup } = frontendRequire('react-dom/server')

test('pending message receipt polling has a finite automatic limit and can be resumed manually', () => {
  assert.equal(shouldContinueMessageReceiptPolling('running', 0), true)
  assert.equal(shouldContinueMessageReceiptPolling('running', MESSAGE_RECEIPT_AUTO_POLL_LIMIT - 1), true)
  assert.equal(shouldContinueMessageReceiptPolling('running', MESSAGE_RECEIPT_AUTO_POLL_LIMIT), false)
  assert.equal(shouldContinueMessageReceiptPolling('succeeded', 0), false)
  assert.equal(MESSAGE_RECEIPT_AUTO_POLL_LIMIT, 8)
})

test('only known recoverable failed receipts allow same-request retry', () => {
  const item = (error) => ({ request_id: 'request-1', text: '继续前行', status: 'failed', result: { error } })
  assert.equal(conversationErrorCode(item('STORAGE_BUSY')), 'STORAGE_BUSY')
  assert.equal(conversationErrorCode(item({ code: 'PROCESS_INTERRUPTED' })), 'PROCESS_INTERRUPTED')
  assert.equal(canRetryFailedMessageFromReceipt(item('STORAGE_BUSY')), true)
  assert.equal(canRetryFailedMessageFromReceipt(item({ code: 'PROCESS_INTERRUPTED' })), true)
  assert.equal(canRetryFailedMessageFromReceipt(item('BRANCH_BUSY')), false)
  assert.equal(canRetryFailedMessageFromReceipt({ ...item('STORAGE_BUSY'), status: 'running' }), false)
})

test('original text is editable only after a failed request is known to be safe', () => {
  assert.equal(canEditFailedMessage('failed', false), true)
  assert.equal(canEditFailedMessage('pending', false), false)
  assert.equal(canEditFailedMessage('failed', true), false)
})

test('branch busy is recognized only as the typed non-retryable 409 conflict', () => {
  assert.equal(isBranchBusyConflict(new ApiError(409, { code: 'BRANCH_BUSY', retryable: false })), true)
  assert.equal(isBranchBusyConflict(new ApiError(409, { code: 'STALE_VERSION', retryable: false })), false)
  assert.equal(isBranchBusyConflict(new ApiError(503, { code: 'BRANCH_BUSY', retryable: false })), false)
})

test('runtime profile failure renders a page-local retry action', () => {
  const markup = renderToStaticMarkup(React.createElement(RuntimeProfileStatus, {
    profile: null,
    loading: false,
    error: 'settings unavailable',
    onRetry() {},
  }))
  assert.match(markup, /发送已停用/)
  assert.match(markup, /settings unavailable/)
  assert.match(markup, /重试配置读取/)
  assert.match(markup, /role="status"/)
})
