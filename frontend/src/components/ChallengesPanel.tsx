import { useRef, useState } from 'react'
import { api, ApiError, type ChallengeOption, type WorldState } from '../api/client'
import { errorMessage } from '../utils'

export function ChallengesPanel({ worldId, branchId, actorId, version, options, receipts, onRefresh }: {
  worldId: string
  branchId: string
  actorId: string
  version: string
  options: ChallengeOption[]
  receipts: NonNullable<WorldState['own_report_receipts']>
  onRefresh: () => void
}) {
  const request = useRef<{ id: string; key: string; version: string } | null>(null)
  const busy = useRef(false)
  const [pending, setPending] = useState('')
  const [feedback, setFeedback] = useState('')
  const [settled, setSettled] = useState<Record<string, boolean>>({})

  async function attempt(option: ChallengeOption) {
    if (busy.current || option.can_attempt_skill === false) return
    const id = option.challenge_id
    const previous = request.current
    const action = previous?.id === id && previous.version === version ? previous : { id, version, key: crypto.randomUUID() }
    request.current = action
    busy.current = true
    setPending(id)
    setFeedback('')
    try {
      const result = await api.performChallenge(worldId, id, {
        branch_id: branchId, actor_id: actorId, expected_state_version: action.version,
      }, action.key)
      request.current = null
      setSettled((items) => ({ ...items, [id]: result.passed }))
      onRefresh()
    } catch (error) {
      setFeedback(errorMessage(error))
      if (error instanceof ApiError && error.status === 409) onRefresh()
    } finally {
      busy.current = false
      setPending('')
    }
  }

  let title = '在地考核'
  if (!options.length && receipts.length) title = '我的报告回执'
  else if (receipts.length || options.some((option) => option.report)) title = '在地考核与报告'
  else if (options.some((option) => option.action_label)) title = '现场查验与在地考核'

  return <details className="scene-story-chronicle">
    <summary>{title}</summary>
    <ul>{options.map((option) => {
      const result = option.passed ?? settled[option.challenge_id]
      return <li key={option.challenge_id}>
        <strong>{option.title}</strong>
        <p>{option.description}</p>
        {option.report
          ? <p>交给{option.report.recipient} · {option.report.source}<br/><q>{option.report.statement}</q></p>
          : <p>{option.skill_label}{option.current_level != null ? `当前 ${option.current_level}，需 ${option.pass_level}` : `达到 ${option.pass_level} 可通过`}。</p>}
        {typeof result !== 'boolean' && !option.requires_pass_level && !option.report && <p>此项考核仅结算一次。</p>}
        {typeof result !== 'boolean' && option.can_attempt_skill === false && <p>可先修习所需技能，再回来参加。</p>}
        {typeof result === 'boolean' ? <p>{result ? (option.report ? '已登记' : '已通过') : '未通过'}</p> :
          <button type="button" className="button button-secondary button-small" disabled={Boolean(pending) || option.can_attempt_skill === false}
            aria-label={option.report ? `向${option.report.recipient}登记：${option.title}` : undefined}
            onClick={() => void attempt(option)}>
            {pending === option.challenge_id ? '结算中…' : option.report ? '登记这份报告' : option.fee
              ? `参加并支付 ${option.fee.quantity} ${option.fee.label}` : option.action_label ?? '参加考核'}
          </button>}
      </li>
    })}</ul>
    {receipts.length > 0 && <section aria-label="我的报告回执">
      {options.length > 0 && <strong>我的报告回执</strong>}
      <ul>{receipts.map((row) => <li key={row.id}>{row.summary}</li>)}</ul>
    </section>}
    {feedback && <p role="alert">{feedback}</p>}
  </details>
}
