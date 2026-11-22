import { useRef, useState } from 'react'
import { api, ApiError, type ContestAction, type ContestOption } from '../api/client'
import { errorMessage } from '../utils'

const titles = { invited: '等待答复', accepted: '双方已确认', declined: '邀约被婉拒', withdrawn: '对局已撤回', settled: '对局结束' }

export function ContestsPanel({ worldId, branchId, actorId, version, options, onRefresh }: {
  worldId: string
  branchId: string
  actorId: string
  version: string
  options: ContestOption[]
  onRefresh: () => void
}) {
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState('')
  const request = useRef<{ fingerprint: string; key: string } | null>(null)
  const inFlight = useRef(false)

  async function send(action: ContestAction) {
    if (inFlight.current) return
    inFlight.current = true
    setPending(true)
    setFeedback('')
    const fingerprint = JSON.stringify([version, action])
    const key = request.current?.fingerprint === fingerprint ? request.current.key : crypto.randomUUID()
    request.current = { fingerprint, key }
    try {
      await api.changeContest(worldId, { branch_id: branchId, actor_id: actorId, expected_state_version: version, action }, key)
      request.current = null
      onRefresh()
    } catch (error) {
      setFeedback(errorMessage(error))
      if (error instanceof ApiError && error.status === 409) onRefresh()
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }

  return <details className="scene-story-chronicle">
    <summary>在地对局</summary>
    {options.map((option) => <ContestCard key={option.definition_id} option={option} actorId={actorId} pending={pending} send={send} />)}
    {feedback && <p role="alert">{feedback}</p>}
  </details>
}

function ContestCard({ option, actorId, pending, send }: {
  option: ContestOption
  actorId: string
  pending: boolean
  send: (action: ContestAction) => Promise<void>
}) {
  const [rival, setRival] = useState('')
  const [selection, setSelection] = useState('')
  const opponent = option.opponents.find((entry) => entry.id === rival)
  const routine = option.routines.find((entry) => entry.key === selection)
  const active = option.recent.some((row) => row.status === 'invited' || row.status === 'accepted')
  const needsRoutine = option.can_invite && !active || option.recent.some((row) => row.status === 'invited' && row.recipient_id === actorId)

  return <section aria-labelledby={`contest-${option.definition_id}`}>
    <h3 id={`contest-${option.definition_id}`}>{option.title}</h3>
    <p>{option.terms}</p>
    {needsRoutine && <label className="author-simple-field">
      <span>声明技艺</span>
      <select disabled={pending} value={routine?.key ?? ''} onChange={(event) => setSelection(event.currentTarget.value)}>
        <option value="">选择一项登记技艺</option>
        {option.routines.map((entry) => <option key={entry.key} value={entry.key}>{entry.title}</option>)}
      </select>
    </label>}
    {option.can_invite && !active && option.opponents.length > 0 && <div className="character-create-actions">
      <label className="author-simple-field">
        <span>邀请在场人物</span>
        <select disabled={pending} value={opponent?.id ?? ''} onChange={(event) => setRival(event.currentTarget.value)}>
          <option value="">选择受邀者</option>
          {option.opponents.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
        </select>
      </label>
      <button type="button" className="button button-secondary" disabled={pending || !opponent || !routine}
        onClick={() => { if (opponent && routine) void send({operation:'invite', definition_id:option.definition_id, recipient_id:opponent.id, routine:routine.key}) }}>
        发出邀约
      </button>
    </div>}
    <ul>{option.recent.map((row) => <li key={row.contest_id}>
      <strong>{titles[row.status]}</strong>
      {row.status === 'settled' && <p>{row.winner_id === actorId ? '你获胜' : row.winner_id ? '对手获胜' : '平局'} · {row.rounds} 轮</p>}
      {row.status === 'invited' && row.recipient_id === actorId && <>
        <button className="button button-secondary button-small" type="button" disabled={pending || !routine}
          onClick={() => { if (routine) void send({ operation:'reply', contest_id:row.contest_id, routine:routine.key }) }}>确认参赛</button>
        <button className="button button-secondary button-small" type="button" disabled={pending}
          onClick={() => void send({ operation:'reply', contest_id:row.contest_id, routine:null })}>婉拒</button>
      </>}
      {row.can_ask_npc && <button className="button button-secondary button-small" type="button" disabled={pending}
        onClick={() => void send({ operation:'ask_npc', contest_id:row.contest_id })}>请受邀者答复</button>}
      {row.status === 'accepted' && <button className="button button-primary button-small" type="button" disabled={pending}
        onClick={() => void send({ operation:'settle', contest_id:row.contest_id })}>开始并结算对局</button>}
      {(row.status === 'invited' || row.status === 'accepted') && <button className="button button-secondary button-small" type="button" disabled={pending}
        onClick={() => void send({ operation:'withdraw', contest_id:row.contest_id })}>撤回</button>}
    </li>)}</ul>
  </section>
}
