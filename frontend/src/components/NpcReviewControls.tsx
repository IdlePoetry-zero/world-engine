import { useState } from 'react'
import { api, type NpcReviewPreview } from '../api/client'
import { errorMessage } from '../utils'

export function NpcReviewControls({ worldId, branchId, stateVersion, actorId, destinations, hasActiveGoal, onCommitted }: {
  worldId: string
  branchId: string
  stateVersion: string
  actorId: string
  destinations: { id: string; name?: string | null }[]
  hasActiveGoal: boolean
  onCommitted: () => void
}) {
  const [count, setCount] = useState(1)
  const [interval, setInterval] = useState(7)
  const [destination, setDestination] = useState('')
  const [description, setDescription] = useState('')
  const [preview, setPreview] = useState<NpcReviewPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const valid = Number.isInteger(count) && count >= 1 && count <= 1_000_000
    && (count === 1 || (Number.isSafeInteger(interval) && interval >= 1))
    && (destination ? description.trim().length > 0 : hasActiveGoal)

  const submit = async () => {
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    try {
      if (preview) {
        await api.commitNpcReview(worldId, { preview_id: preview.preview_id })
        onCommitted()
      } else {
        setPreview(await api.previewNpcReview(worldId, {
          branch_id: branchId, expected_state_version: stateVersion, actor_id: actorId,
          review_count: count, review_interval_days: count > 1 ? interval : 7,
          goal_destination_id: destination || undefined, goal_description: destination ? description.trim() : undefined,
        }))
      }
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy(false)
    }
  }

  return <details className="character-create-field">
    <summary>安排自主目标复审</summary>
    <p>可以沿用现有目标，或为 NPC 指定新的目的地。世界时间推进一天后，模型首次评估下一步。</p>
    {preview ? <div role="status"><p>最多 {preview.review_schedule.max_runs} 次
      {preview.review_schedule.repeat_ticks && `，此后每隔 ${interval} 个世界日`}。确认时世界时间不推进。</p>
      {preview.goal && <p>{preview.goal.description} · 亲自抵达{destinations.find((place) => place.id === preview.goal?.conditions[0]?.target_id)?.name || preview.goal.conditions[0]?.target_id}</p>}
    </div>
      : <div className="character-create-grid">
        {destinations.length > 0 && <label className="character-create-field"><span>目标目的地</span><select value={destination} onChange={(event) => setDestination(event.target.value)}>
          <option value="">{hasActiveGoal ? '沿用现有目标' : '请选择地点'}</option>
          {destinations.map((place) => <option key={place.id} value={place.id}>{place.name || place.id}</option>)}
        </select></label>}
        {destination && <label className="character-create-field"><span>新目标描述</span><textarea rows={2} maxLength={1000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>}
        <label className="character-create-field"><span>最多复审次数</span><input type="number" min={1} max={1_000_000} step={1} value={Number.isNaN(count) ? '' : count} onChange={(event) => setCount(event.target.valueAsNumber)} /></label>
        {count > 1 && <label className="character-create-field"><span>复审间隔（世界日）</span><input type="number" min={1} step={1} value={Number.isNaN(interval) ? '' : interval} onChange={(event) => setInterval(event.target.valueAsNumber)} /></label>}
      </div>}
    {error && <p role="alert">{error}</p>}
    <div className="character-preview-actions">
      {preview && <button className="button button-secondary" type="button" disabled={busy} onClick={() => { setPreview(null); setError(null) }}>返回修改</button>}
      <button className="button button-primary" type="button" disabled={!valid || busy} onClick={() => void submit()}>{busy ? '处理中…' : preview ? '确认复审安排' : '预览复审安排'}</button>
    </div>
  </details>
}
