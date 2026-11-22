import { useRef, useState } from 'react'
import { api, ApiError, type ChallengeOption } from '../api/client'
import { errorMessage } from '../utils'

export function ChallengesPanel({ worldId, branchId, actorId, version, options, onRefresh }: {
  worldId: string
  branchId: string
  actorId: string
  version: string
  options: ChallengeOption[]
  onRefresh: () => void
}) {
  const request = useRef<{ id: string; key: string; version: string } | null>(null)
  const busy = useRef(false)
  const [pending, setPending] = useState('')
  const [feedback, setFeedback] = useState('')
  const [settled, setSettled] = useState<Record<string, boolean>>({})

  async function attempt(option: ChallengeOption) {
    if (busy.current) return
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

  return <details className="scene-story-chronicle">
    <summary>在地考核</summary>
    <ul>{options.map((option) => {
      const result = option.passed ?? settled[option.challenge_id]
      return <li key={option.challenge_id}>
        <strong>{option.title}</strong>
        <p>{option.description}</p>
        <p>{option.skill_label}达到 {option.pass_level} 可通过。</p>
        {typeof result === 'boolean' ? <p>{result ? '已通过' : '未通过'}</p> :
          <button type="button" className="button button-secondary button-small" disabled={Boolean(pending)}
            onClick={() => void attempt(option)}>
            {pending === option.challenge_id ? '结算中…' : option.fee
              ? `参加并支付 ${option.fee.quantity} ${option.fee.label}` : '参加考核'}
          </button>}
      </li>
    })}</ul>
    {feedback && <p role="alert">{feedback}</p>}
  </details>
}
