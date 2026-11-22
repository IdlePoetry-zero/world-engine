/* oxlint-disable react/only-export-components */
import type { CallAttemptRecord, CallRecord, TransportFailureKind } from '../api/client'

const TRANSPORT_LABELS: Record<TransportFailureKind, string> = {
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

function transportFailureLabel(kind: unknown): string {
  if (typeof kind !== 'string' || kind.length === 0) return '未记录'
  return TRANSPORT_LABELS[kind as TransportFailureKind] ?? '其他网络/请求错误'
}

function retryabilityLabel(value: unknown): string {
  return value === true ? '可重试' : value === false ? '不可重试' : '未记录'
}

function billingPossibilityLabel(value: unknown): string {
  return value === true ? '可能计费' : value === false ? '未计费' : '未知'
}

type FailureView = {
  attemptNumber: string
  attemptContext: string
  phase: string
  retryable: string
  possiblyBilled: string
  attemptSummary: string
  attempts: CallAttemptRecord[]
}

function failureView(call: CallRecord): FailureView | null {
  const trace = call.trace
  const attempts = Array.isArray(trace?.attempts) ? trace.attempts : []
  const traceKind = trace?.transport_failure_kind
  const hasKind = traceKind != null || attempts.some((attempt) => attempt?.transport_failure_kind != null)
  const isFailedCall = call.status === 'provider_error' || call.status === 'stream_failed'
  if (!hasKind && !isFailedCall) return null

  const diagnosticAttempt = [...attempts].reverse().find((attempt) => attempt?.transport_failure_kind != null)
    ?? (isFailedCall ? attempts.at(-1) : undefined)
  const diagnosticAttemptIndex = diagnosticAttempt ? attempts.indexOf(diagnosticAttempt) : -1
  const isHistorical = call.status === 'completed' && diagnosticAttemptIndex >= 0 && diagnosticAttemptIndex < attempts.length - 1
  const kind = traceKind ?? diagnosticAttempt?.transport_failure_kind
  return {
    attemptNumber: diagnosticAttempt?.attempt_number != null ? String(diagnosticAttempt.attempt_number) : '未记录',
    attemptContext: isHistorical ? '历史失败尝试' : '最近失败尝试',
    phase: transportFailureLabel(kind),
    retryable: retryabilityLabel(diagnosticAttempt?.retryable),
    possiblyBilled: billingPossibilityLabel(diagnosticAttempt?.possibly_billed),
    attemptSummary: attempts.length === 0 ? '未记录' : attempts.map((attempt, index) => {
      const number = attempt.attempt_number ?? index + 1
      return `#${number} ${transportFailureLabel(attempt.transport_failure_kind)} (该次尝试可重试：${retryabilityLabel(attempt.retryable)}；该次尝试计费：${billingPossibilityLabel(attempt.possibly_billed)})`
    }).join(' | '),
    attempts,
  }
}

export function transportFailureCsvFields(call: CallRecord): [attemptNumber: string, attemptContext: string, phase: string, retryable: string, possiblyBilled: string, attemptSummary: string] {
  const view = failureView(call)
  return view ? [view.attemptNumber, view.attemptContext, view.phase, view.retryable, view.possiblyBilled, view.attemptSummary] : ['', '', '', '', '', '']
}

export function TransportFailure({ call, compact = false }: { call: CallRecord; compact?: boolean }) {
  const view = failureView(call)
  if (!view) return null

  if (compact) {
    return <small data-testid="transport-failure-summary">{view.attemptContext} #{view.attemptNumber} · 阶段 {view.phase} · 该次可重试 {view.retryable} · 该次计费 {view.possiblyBilled}</small>
  }

  const attempts = view.attempts
  return <section aria-label="传输失败诊断" data-testid="transport-failure-details">
    <p><strong>{view.attemptContext}：</strong>#{view.attemptNumber}　<strong>故障阶段：</strong>{view.phase}　<strong>该次尝试可重试：</strong>{view.retryable}　<strong>该次尝试计费：</strong>{view.possiblyBilled}</p>
    {attempts.length > 0 && <ul aria-label="调用尝试">
      {attempts.map((attempt, index) => <li key={attempt.attempt_number ?? index}>
        尝试 {attempt.attempt_number ?? index + 1} · 阶段 {transportFailureLabel(attempt.transport_failure_kind)} · 该次尝试可重试 {retryabilityLabel(attempt.retryable)} · 该次尝试计费 {billingPossibilityLabel(attempt.possibly_billed)}
      </li>)}
    </ul>}
  </section>
}
