import { useEffect, useRef, useState } from 'react'
import type { HistoryEvent, World, Branch } from '../api/client'
import { api } from '../api/client'
import { errorMessage } from '../utils'
import { Icon } from '../components/Icon'
import { InlineState, PageEmpty } from '../components/States'
import type { Notice } from '../App'

export function HistoryPage({ world, branches, branchId, onBranchChange, onRefresh, onNotice }: { world: World | null; branches: Branch[]; branchId: string | null; onBranchChange: (id: string) => void; onRefresh: () => Promise<void>; onNotice: (notice: Notice) => void }) {
  const [events, setEvents] = useState<HistoryEvent[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [branchLabel, setBranchLabel] = useState('')
  const [creating, setCreating] = useState(false)
  const branchRequestKey = useRef<string | null>(null)

  useEffect(() => {
    if (!world || !branchId) { setEvents([]); setCursor(null); return }
    let active = true; setLoading(true); setError(null); setEvents([]); setCursor(null)
    api.history(world.world_id, branchId).then((result) => { if (active) { setEvents(result.items); setCursor(result.next_cursor ?? null) } }).catch((reason) => { if (active) setError(errorMessage(reason)) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [world?.world_id, branchId])

  const loadMore = async () => {
    if (!world || !branchId || !cursor || loadingMore) return
    setLoadingMore(true)
    try { const result = await api.history(world.world_id, branchId, cursor); setEvents((current) => [...current, ...result.items]); setCursor(result.next_cursor ?? null) }
    catch (reason) { onNotice({ kind: 'error', text: errorMessage(reason) }) }
    finally { setLoadingMore(false) }
  }

  const createBranch = async () => {
    if (!world || !branchId || !branchLabel.trim()) return
    const label = branchLabel.trim()
    branchRequestKey.current ??= crypto.randomUUID()
    setCreating(true)
    try {
      const created = await api.createBranch(world.world_id, { checkpoint: events.at(-1)?.event_id ?? branchId, label }, branchRequestKey.current)
      branchRequestKey.current = null
      onBranchChange(created.branch_id); setBranchLabel(''); await onRefresh()
      onNotice({ kind: 'success', text: '分支创建请求已提交。' })
    } catch (reason) { onNotice({ kind: 'error', text: errorMessage(reason) }) }
    finally { setCreating(false) }
  }

  if (!world) return <PageEmpty eyebrow="历史档案" title="选择一个世界以查看历史。" body="事件与分支只会在后端返回后显示。" />
  return <section className="history-page page-enter">
    <div className="history-heading"><div><div className="eyebrow"><span className="eyebrow-line" />时间留下的痕迹</div><h1>历史与分支</h1><p>每条记录来自当前世界分支的事件日志。</p></div><div className="history-actions"><select className="branch-select" aria-label="选择历史分支" value={branchId ?? ''} onChange={(event) => onBranchChange(event.target.value)}>{branches.length ? branches.map((branch) => <option key={branch.branch_id} value={branch.branch_id}>{branch.label || branch.branch_id.slice(0, 8)}</option>) : <option value="">暂无分支</option>}</select></div></div>
    <div className="history-layout"><div className="timeline-panel"><div className="timeline-panel-head"><div><span className="panel-overline">EVENT LEDGER</span><h2>事件记录</h2></div><span className="history-count">{loading ? '同步中' : `${events.length} 条记录`}</span></div>
      {error ? <InlineState icon="alert" title="无法读取历史" description={error} action={<button className="button button-secondary button-small" onClick={() => { if (world && branchId) { setLoading(true); void api.history(world.world_id, branchId).then((result) => { setEvents(result.items); setCursor(result.next_cursor ?? null); setError(null) }).catch((reason) => setError(errorMessage(reason))).finally(() => setLoading(false)) } }}>重试</button>} /> : loading ? <div className="event-loading"><Icon name="spinner" />正在读取事件日志…</div> : events.length ? <><div className="timeline-list">{events.map((event) => <article className="timeline-event" key={event.event_id}><div className="timeline-rail"><span className="timeline-dot" /></div><div className="timeline-event-body"><div className="event-meta"><span className="event-seq">#{event.sequence}</span><span className="event-time">{event.tick} tick</span><span className="event-kind">{event.type}</span></div><h3>{eventTitle(event.type)}</h3><p>{eventSummary(event.payload)}</p><details><summary>查看事件载荷</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></div></article>)}</div>{cursor && <button className="button button-secondary load-more" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? <Icon name="spinner" size={15} /> : <Icon name="chevrons" size={15} />}{loadingMore ? '正在读取…' : '读取更早记录'}</button>}</> : <div className="history-empty"><div className="empty-orbit small-orbit"><span className="empty-orbit-core" /><i /><i /></div><strong>这条时间线上还没有事件。</strong><span>世界提交事件后会出现在这里。查询不会创建世界事件。</span></div>}
    </div><aside className="branch-panel"><div className="branch-panel-head"><div><span className="panel-overline">TIMELINE FORK</span><h2>分支</h2></div><span className="branch-total">{branches.length}</span></div>{branches.length ? <div className="branch-list">{branches.map((branch) => <button className={`branch-card ${branch.branch_id === branchId ? 'current' : ''}`} key={branch.branch_id} onClick={() => onBranchChange(branch.branch_id)}><span className="branch-card-icon"><Icon name="branch" size={16} /></span><span><strong>{branch.label || '未命名分支'}</strong><small>{branch.branch_id.slice(0, 8)} · {branch.current_tick} tick</small></span>{branch.branch_id === branchId && <span className="current-branch-tag">当前</span>}</button>)}</div> : <div className="branch-none">当前世界尚未返回分支记录。</div>}<div className="new-branch-form"><label htmlFor="branch-name">从当前检查点创建分支</label><div className="branch-input-row"><input id="branch-name" value={branchLabel} onChange={(event) => { if (event.target.value !== branchLabel) branchRequestKey.current = null; setBranchLabel(event.target.value) }} placeholder="分支名称" /><button className="button button-primary button-icon-only" onClick={() => void createBranch()} disabled={!branchId || !branchLabel.trim() || creating} aria-label="创建分支">{creating ? <Icon name="spinner" size={15} /> : <Icon name="plus" size={16} />}</button></div><small>创建分支会保存一个独立的历史前缀。</small></div><div className="branch-note"><Icon name="info" size={14} /><span>分支之间的事件不会自动合并。重试行动应从检查点创建新分支。</span></div></aside></div>
  </section>
}

function eventTitle(type: string) { return type.split('.').at(-1)?.replace(/\.v\d+$/, '').replaceAll('_', ' ') ?? type }
function eventSummary(payload: unknown) { if (!payload || typeof payload !== 'object') return '事件载荷暂无摘要。'; const entries = Object.entries(payload as Record<string, unknown>).slice(0, 3); return entries.map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ') || '事件载荷暂无摘要。' }
