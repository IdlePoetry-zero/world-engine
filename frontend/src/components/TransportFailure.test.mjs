import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'

const frontendRequire = createRequire(new URL('../../package.json', import.meta.url))
const React = frontendRequire('react')
const { renderToStaticMarkup } = frontendRequire('react-dom/server')
const jsxRuntimeUrl = pathToFileURL(frontendRequire.resolve('react/jsx-runtime')).href
const componentDirectory = path.dirname(fileURLToPath(import.meta.url))
const frontendDirectory = path.resolve(componentDirectory, '../..')
const tempDirectory = await mkdtemp(path.join(tmpdir(), 'transport-failure-test-'))
const tscPath = path.resolve(frontendDirectory, 'node_modules/typescript/bin/tsc')
const compile = spawnSync(process.execPath, [tscPath, '--ignoreConfig', '--jsx', 'react-jsx', '--module', 'ESNext', '--target', 'ES2022', '--moduleResolution', 'Bundler', '--types', 'vite/client', '--skipLibCheck', '--outDir', tempDirectory, path.join(componentDirectory, 'TransportFailure.tsx')], { cwd: frontendDirectory, encoding: 'utf8' })
if (compile.status !== 0) throw new Error(`TransportFailure.tsx test compilation failed: ${compile.stderr || compile.stdout}`)
const compiledPath = path.join(tempDirectory, 'components', 'TransportFailure.js')
const compiled = (await readFile(compiledPath, 'utf8')).replace('from "react/jsx-runtime"', `from "${jsxRuntimeUrl}"`)
const componentModule = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const { TransportFailure, transportFailureCsvFields } = componentModule
await rm(tempDirectory, { recursive: true, force: true })

test('renders a known transport phase while leaving billing unknown', () => {
  const call = {
    request_id: 'fixture-known-phase', role: 'npc_decision', status: 'provider_error',
    trace: {
      transport_failure_kind: 'connect_timeout',
      attempts: [{ attempt_number: 1, outcome: 'error', retryable: true, possibly_billed: null, transport_failure_kind: 'connect_timeout' }],
    },
  }
  const markup = renderToStaticMarkup(React.createElement(TransportFailure, {
    call,
  }))

  assert.match(markup, /连接超时/)
  assert.match(markup, /可重试/)
  assert.match(markup, /计费：<\/strong>未知/)
  assert.doesNotMatch(markup, /未计费/)
  assert.deepEqual(transportFailureCsvFields(call), ['1', '最近失败尝试', '连接超时', '可重试', '未知', '#1 连接超时 (该次尝试可重试：可重试；该次尝试计费：未知)'])
})

test('shows unknown for old records and future kinds without exposing their raw value', () => {
  const oldMarkup = renderToStaticMarkup(React.createElement(TransportFailure, {
    call: { request_id: 'fixture-old-record', role: 'npc_decision', status: 'provider_error', trace: {} },
    compact: true,
  }))
  const futureKind = 'https://secret.example Authorization: Bearer fixture-secret'
  const futureMarkup = renderToStaticMarkup(React.createElement(TransportFailure, {
    call: {
      request_id: 'fixture-future-kind', role: 'npc_decision', status: 'provider_error',
      trace: { transport_failure_kind: futureKind, attempts: [{ retryable: null, possibly_billed: null, transport_failure_kind: futureKind }] },
    },
  }))

  assert.match(oldMarkup, /阶段 未记录/)
  assert.match(oldMarkup, /该次可重试 未记录/)
  assert.match(oldMarkup, /该次计费 未知/)
  assert.deepEqual(transportFailureCsvFields({ request_id: 'fixture-old-record', role: 'npc_decision', status: 'provider_error', trace: {} }), ['未记录', '最近失败尝试', '未记录', '未记录', '未知', '未记录'])
  assert.match(futureMarkup, /其他网络\/请求错误/)
  assert.doesNotMatch(futureMarkup, /secret\.example|Authorization|fixture-secret/)
  assert.match(futureMarkup, /计费 未知/)
})

test('maps every fixed transport kind and preserves explicit false values', () => {
  const labels = {
    connect_timeout: '连接超时',
    connect: '连接失败',
    read_timeout: '读取超时',
    write_timeout: '写入超时',
    pool_timeout: '连接池等待超时',
    timeout: '网络超时',
    remote_protocol: '协议断开',
    network: '其他网络错误',
    request: '请求错误',
  }
  for (const [kind, label] of Object.entries(labels)) {
    const markup = renderToStaticMarkup(React.createElement(TransportFailure, {
      call: { request_id: `fixture-${kind}`, role: 'npc_decision', status: 'provider_error', trace: { transport_failure_kind: kind } },
      compact: true,
    }))
    assert.ok(markup.includes(label), `${kind} should display ${label}`)
  }

  const falseValueMarkup = renderToStaticMarkup(React.createElement(TransportFailure, {
    call: {
      request_id: 'fixture-explicit-false', role: 'npc_decision', status: 'provider_error',
      trace: { attempts: [{ attempt_number: 2, retryable: false, possibly_billed: false, transport_failure_kind: 'read_timeout' }] },
    },
  }))
  assert.match(falseValueMarkup, /不可重试/)
  assert.match(falseValueMarkup, /未计费/)
})

test('scopes billing labels to the failed attempt before a later failure or success', () => {
  const mixedFailureCall = {
    request_id: 'fixture-multiple-failed-attempts', role: 'npc_decision', status: 'provider_error',
    trace: { transport_failure_kind: 'connect', attempts: [
      { attempt_number: 1, outcome: 'error', retryable: true, possibly_billed: null, transport_failure_kind: 'read_timeout' },
      { attempt_number: 2, outcome: 'error', retryable: true, possibly_billed: true, transport_failure_kind: 'write_timeout' },
      { attempt_number: 3, outcome: 'error', retryable: true, possibly_billed: false, transport_failure_kind: 'connect' },
    ] },
  }
  const failedMarkup = renderToStaticMarkup(React.createElement(TransportFailure, { call: mixedFailureCall }))
  assert.match(failedMarkup, /最近失败尝试：<\/strong>#3/)
  assert.match(failedMarkup, /该次尝试计费：<\/strong>未计费/)
  assert.match(failedMarkup, /计费 未知/)
  assert.match(failedMarkup, /计费 可能计费/)
  assert.deepEqual(transportFailureCsvFields(mixedFailureCall).slice(0, 5), ['3', '最近失败尝试', '连接失败', '可重试', '未计费'])

  const retriedSuccessCall = {
    request_id: 'fixture-success-after-transport-retry', role: 'npc_decision', status: 'completed',
    usage: { input_tokens: 10, output_tokens: 8, total_tokens: 18 },
    cost: { state: 'reported', amount: '0.05', currency: 'USD' },
    trace: { attempts: [
      { attempt_number: 1, outcome: 'error', retryable: true, possibly_billed: false, transport_failure_kind: 'connect_timeout' },
      { attempt_number: 2, outcome: 'response_received', retryable: false, possibly_billed: true, usage: { total_tokens: 18 } },
    ] },
  }
  const successMarkup = renderToStaticMarkup(React.createElement(TransportFailure, { call: retriedSuccessCall, compact: true }))
  assert.match(successMarkup, /历史失败尝试 #1/)
  assert.match(successMarkup, /该次计费 未计费/)
  assert.doesNotMatch(successMarkup, /调用未计费/)
  assert.deepEqual(transportFailureCsvFields(retriedSuccessCall).slice(0, 5), ['1', '历史失败尝试', '连接超时', '可重试', '未计费'])
})
