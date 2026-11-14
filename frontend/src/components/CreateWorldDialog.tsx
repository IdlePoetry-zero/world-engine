import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import type { Pack } from '../api/client'
import { Icon } from './Icon'

export type CreateWorldDraft = { pack_id: string; title: string; player_name: string }

export function CreateWorldDialog({ packs, onClose, onSubmit }: { packs: Pack[]; onClose: () => void; onSubmit: (draft: CreateWorldDraft, idempotencyKey: string) => Promise<void> }) {
  const [packId, setPackId] = useState(packs[0]?.pack_id ?? '')
  const [title, setTitle] = useState('')
  const [playerName, setPlayerName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const key = useRef(crypto.randomUUID())

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!packId || !title.trim() || !playerName.trim()) return
    setSubmitting(true); setError('')
    try { await onSubmit({ pack_id: packId, title: title.trim(), player_name: playerName.trim() }, key.current) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '创建请求失败，请重试。') }
    finally { setSubmitting(false) }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section className="create-dialog" role="dialog" aria-modal="true" aria-labelledby="create-title">
        <div className="dialog-topline"><span className="eyebrow"><span className="eyebrow-line" />新建存档</span><button className="icon-button" onClick={onClose} aria-label="关闭"><Icon name="close" /></button></div>
        <div className="dialog-heading"><div className="dialog-glyph"><Icon name="orbit" size={23} /></div><h2 id="create-title">让一个世界开始。</h2><p>选择一份已发布的设定包，再为你的存档与角色命名。</p></div>
        {packs.length === 0 ? <div className="pack-empty"><div className="pack-empty-icon"><Icon name="archive" size={20} /></div><strong>当前没有可用的设定包</strong><span>后台尚未发布创建模板。这里不会用演示数据代替真实世界。</span></div> : <form onSubmit={(event) => void submit(event)}>
          <label className="field-label" htmlFor="pack-choice">世界设定包</label><select id="pack-choice" value={packId} onChange={(event) => setPackId(event.target.value)}>{packs.map((pack) => <option key={pack.pack_id} value={pack.pack_id}>{pack.title} · v{pack.version}</option>)}</select>
          {packId && <div className="pack-description">{packs.find((pack) => pack.pack_id === packId)?.description}</div>}
          <label className="field-label" htmlFor="world-title">世界名称</label><input id="world-title" autoFocus value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：雾港之后" maxLength={80} required />
          <label className="field-label" htmlFor="player-name">你的角色名</label><input id="player-name" value={playerName} onChange={(event) => setPlayerName(event.target.value)} placeholder="角色在世界里的名字" maxLength={60} required />
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="button button-primary dialog-submit" disabled={submitting || !packId}>{submitting ? <><Icon name="spinner" size={16} />提交中…</> : <>创建世界 <Icon name="arrow-right" size={16} /></>}</button>
        </form>}
        <div className="dialog-foot"><Icon name="lock" size={13} />创建任务由本地引擎处理；离开页面不会让未提交的世界生效。</div>
      </section>
    </div>
  )
}
