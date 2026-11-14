import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { api, type Branch, type CharacterEntity, type GameMessage, type Settings, type World, type WorldView } from '../api/client'
import { PageEmpty } from '../components/States'
import { Icon } from '../components/Icon'
import type { Notice, Page } from '../App'
import { errorMessage, initials } from '../utils'

type Mode = 'auto' | 'ask_gm' | 'roleplay' | 'action' | 'author'
const modeLabels: Record<Mode, string> = { auto: '自动', ask_gm: '问 GM', roleplay: '角色说话', action: '行动', author: '作者指令' }
const modeHints: Record<Mode, string> = { auto: '让 GM 判断是询问、对话还是行动。', ask_gm: '查询当前角色可知的信息，不推进世界时间。', roleplay: '以角色身份说话并进入世界交流。', action: '明确提交一项世界行动。', author: '作者权限下的设定编辑或干预。' }

export function PlayPage({ world, view, branches, branchId, loading, onSelectWorld, onOpenHistory, onBranchChange, onNotice, onRefresh }: { world: World | null; view: WorldView | null; branches: Branch[]; branchId: string | null; loading: boolean; onSelectWorld: () => void; onOpenHistory: () => void; onBranchChange: (id: string) => void; onNotice: (notice: Notice) => void; onRefresh: () => void }) {
  const [mode, setMode] = useState<Mode>('auto')
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<GameMessage[]>([])
  const [sending, setSending] = useState(false)
  const [job, setJob] = useState<{ commandId?: string; jobId?: string; status: string } | null>(null)
  const [profile, setProfile] = useState<Pick<Settings, 'speed' | 'depth'> | null>(null)
  const [profileLoading, setProfileLoading] = useState(true)
  const [profileError, setProfileError] = useState<string | null>(null)
  const sessionId = useRef(crypto.randomUUID())
  const state = view?.state
  const playerId = state?.player_id
  const people = useMemo(() => Object.values(state?.entities ?? {}).filter((entity) => entity.kind === 'character') as CharacterEntity[], [state?.entities])
  const player = people.find((person) => person.id === playerId)
  const location = player?.location_id ? state?.entities[player.location_id]?.name : undefined

  useEffect(() => { setMessages([]); setJob(null); sessionId.current = crypto.randomUUID() }, [world?.world_id, branchId])
  useEffect(() => {
    let active = true
    setProfileLoading(true); setProfileError(null)
    void api.getSettings().then((settings) => { if (active) setProfile({ speed: settings.speed, depth: settings.depth }) }).catch((reason) => { if (active) setProfileError(errorMessage(reason)) }).finally(() => { if (active) setProfileLoading(false) })
    return () => { active = false }
  }, [world?.world_id])
  useEffect(() => {
    if (!job?.commandId || !['queued', 'running', 'waiting_input'].includes(job.status)) return
    let active = true
    const timer = window.setInterval(() => {
      void api.command(job.commandId!).then((result) => {
        if (!active) return
        setJob({ commandId: job.commandId, jobId: job.jobId, status: result.status })
        if (result.status === 'succeeded' && result.result) {
          const content = typeof result.result === 'string' ? result.result : result.result.content
          if (content) setMessages((current) => [...current, { id: `cmd-${job.commandId}`, role: 'gm', content, source: 'api' }])
          if (result.committed_through_tick) { onNotice({ kind: 'success', text: `行动已完成 · 世界时间 ${result.committed_through_tick}` }); onRefresh() }
        }
        if (['failed', 'cancelled'].includes(result.status)) onNotice({ kind: 'error', text: result.error?.message ?? `任务${result.status === 'failed' ? '失败' : '已取消'}` })
      }).catch((reason) => { if (active) { setJob({ commandId: job.commandId, jobId: job.jobId, status: 'unavailable' }); onNotice({ kind: 'error', text: errorMessage(reason) }) } })
    }, 2400)
    return () => { active = false; window.clearInterval(timer) }
  }, [job?.commandId, job?.jobId, job?.status, onNotice, onRefresh])

  const send = async () => {
    if (!world || !branchId || !playerId || !profile || !input.trim() || sending) return
    const text = input.trim()
    const requestId = crypto.randomUUID()
    setMessages((current) => [...current, { id: `local-${requestId}`, role: 'player', content: text, source: 'local' }])
    setInput(''); setSending(true)
    try {
      const result = await api.sendMessage(world.world_id, {
        request_id: requestId, world_id: world.world_id, branch_id: branchId,
        session_id: sessionId.current, actor_id: playerId,
        expected_state_version: view?.world.state_version ?? world.state_version,
        mode, text, profile,
      })
      if (result.content) setMessages((current) => [...current, { id: result.message_id, role: 'gm', content: result.content!, source: 'api', kind: result.kind }])
      if (result.job_id || result.command_id) {
        setJob({ commandId: result.command_id, jobId: result.job_id, status: 'queued' })
        if (!result.content) setMessages((current) => [...current, { id: `pending-${result.message_id}`, role: 'system', content: '请求已提交，等待本地引擎处理。', source: 'api' }])
      }
      if (result.world_effect) onRefresh()
    } catch (reason) {
      setMessages((current) => [...current, { id: `error-${requestId}`, role: 'error', content: errorMessage(reason), source: 'api' }])
    } finally { setSending(false) }
  }
  const onInputKey = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() } }

  if (!world) return <PageEmpty eyebrow="进入世界" title="先选择一个世界。" body="从世界库打开现有存档，或创建一个新的世界。当前没有可展示的虚构进度。" action={<button className="button button-primary" onClick={onSelectWorld}>前往世界库 <Icon name="arrow-right" size={16} /></button>} />
  return <section className="play-page page-enter">
    <div className="play-topline"><div><div className="eyebrow"><span className="eyebrow-line" />正在游玩</div><h1>{world.title}</h1></div><div className="play-header-actions"><select aria-label="当前分支" className="branch-select" value={branchId ?? ''} onChange={(event) => onBranchChange(event.target.value)} disabled={!branches.length}>{branches.length === 0 ? <option value="">尚无分支</option> : branches.map((branch) => <option key={branch.branch_id} value={branch.branch_id}>{branch.label || branch.branch_id.slice(0, 8)}</option>)}</select><button className="button button-secondary button-small" onClick={onOpenHistory}><Icon name="timeline" size={15} />历史</button></div></div>
    <div className="play-statusbar"><span className="statusbar-dot" /><span>本地世界</span><span className="status-divider" /><span>{branchId ? `分支 ${branchId.slice(0, 8)}` : '分支待创建'}</span><span className="status-divider" /><span>{view?.branch.current_tick ?? world.current_tick} tick</span><span className="statusbar-spacer" /><span className="status-version">版本 {view?.world.state_version ?? world.state_version}</span></div>
    <div className="play-layout">
      <aside className="play-aside play-aside-left">
        <div className="aside-section"><div className="aside-heading">此刻所在 <Icon name="pin" size={14} /></div>{loading ? <div className="quiet-loading">正在读取世界投影…</div> : location ? <div className="location-card"><div className="location-glyph"><Icon name="compass" size={18} /></div><div><strong>{location}</strong><span>当前位置</span></div></div> : <div className="aside-empty">世界投影尚未提供位置资料。</div>}</div>
        <div className="aside-section"><div className="aside-heading">在场人物 <span className="aside-count">{people.length || '—'}</span></div>{people.length ? <div className="people-list">{people.slice(0, 5).map((person) => <div className="person-row" key={person.id}><span className="person-avatar">{initials(person.name ?? '')}</span><span><strong>{person.name ?? '未命名人物'}</strong><small>{person.status ?? '状态未知'}</small></span><span className="person-presence" /></div>)}</div> : <div className="aside-empty">当前分支没有可显示的人物投影。</div>}</div>
        <div className="aside-link-card"><Icon name="book" size={16} /><span><strong>设定档案</strong><small>查看本世界的实体投影</small></span><button className="link-arrow" aria-label="打开设定档案" onClick={() => window.dispatchEvent(new CustomEvent<Page>('world-engine:navigate', { detail: 'world' }))}><Icon name="arrow-right" size={15} /></button></div>
      </aside>
      <section className="conversation-panel" aria-label="GM 对话">
        <div className="conversation-head"><div><span className="conversation-mark"><Icon name="spark" size={15} /></span><div><strong>GM 对话</strong><small>由当前世界状态与角色视角驱动</small></div></div><button className="icon-button quiet-button" title="复制对话" onClick={() => { void navigator.clipboard?.writeText(messages.map((message) => `${message.role}: ${message.content}`).join('\n\n')); onNotice({ kind: 'info', text: '已复制当前对话。' }) }} disabled={!messages.length}><Icon name="copy" size={16} /></button></div>
        <div className="conversation-body" aria-live="polite">
          {loading && <div className="conversation-blank"><span className="blank-symbol"><Icon name="spinner" size={22} /></span><strong>正在读取世界状态</strong><span>GM 回复只会在收到后端结果后显示。</span></div>}
          {!loading && messages.length === 0 && <div className="conversation-blank"><span className="blank-symbol"><Icon name="message" size={22} /></span><strong>这一页，留给即将发生的事。</strong><span>输入一句话开始。尚未发送的内容不会推进世界时间。</span><div className="starter-prompts"><button onClick={() => { setMode('ask_gm'); setInput('我现在身处哪里？') }}>我现在身处哪里？</button><button onClick={() => { setMode('action'); setInput('看看周围有什么线索。') }}>看看周围有什么线索</button></div></div>}
          {messages.map((message) => <article key={message.id} className={`message-row message-${message.role}`}><div className="message-avatar">{message.role === 'player' ? <Icon name="user" size={15} /> : message.role === 'gm' ? <Icon name="spark" size={15} /> : <Icon name="alert" size={15} />}</div><div className="message-content"><div className="message-meta"><strong>{message.role === 'player' ? '你' : message.role === 'gm' ? 'GM' : '系统提示'}</strong>{message.kind && <span>{message.kind}</span>}{message.source === 'local' && <small>尚未确认送达</small>}</div><p>{message.content}</p></div></article>)}
          {sending && <div className="typing-indicator"><span /><span /><span /><em>正在发送到本地引擎</em></div>}
          {job && <div className="job-strip"><span className="job-pulse" /><span>后台任务 {job.jobId?.slice(0, 8) ?? job.commandId?.slice(0, 8) ?? ''}</span><strong>{jobStatusLabel(job.status)}</strong></div>}
        </div>
        <div className="composer-wrap"><div className="mode-switcher" role="group" aria-label="输入模式">{(['auto', 'ask_gm', 'roleplay', 'action', 'author'] as Mode[]).map((value) => <button key={value} className={`mode-option ${mode === value ? 'selected' : ''}`} onClick={() => setMode(value)} title={modeHints[value]} aria-pressed={mode === value}>{modeLabels[value]}</button>)}</div><div className="composer"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onInputKey} aria-label="发送给 GM" placeholder={mode === 'ask_gm' ? '想向 GM 了解什么？' : mode === 'author' ? '输入作者指令…' : '描述你想说的话或采取的行动…'} rows={2} disabled={!branchId || !playerId || sending || !profile} /><div className="composer-footer"><span><kbd>Enter</kbd> 发送 <span className="kbd-divider">·</span> <kbd>Shift + Enter</kbd> 换行</span><button className="send-button" onClick={() => void send()} disabled={!input.trim() || sending || !branchId || !playerId || !profile} aria-label="发送消息">{sending ? <Icon name="spinner" size={17} /> : <Icon name="arrow-up" size={17} />}</button></div></div><div className="composer-note"><Icon name="shield" size={12} /><span>查询与假设不会推进世界时间；行动结果以提交后的状态为准。</span><span className="profile-chip">{profileLoading ? '读取配置…' : profile ? `${profile.speed} · ${profile.depth}` : `发送已停用 · ${profileError ?? '无法读取运行配置'}`}</span></div></div>
      </section>
      <aside className="play-aside play-aside-right">
        <div className="aside-section"><div className="aside-heading">角色状态 <Icon name="user" size={14} /></div>{player ? <div className="player-card"><div className="player-portrait">{initials(player.name ?? '')}</div><strong>{player.name ?? '未命名人物'}</strong><span>{player.status ?? '状态未知'}</span><div className="player-stat-list"><StatLine label="位置" value={location ?? '未知'} /><StatLine label="资源" value={Object.keys(state?.resources ?? {}).length ? `${Object.keys(state?.resources ?? {}).length} 项` : '尚无数据'} /></div></div> : <div className="aside-empty">当前视角角色信息未包含在授权投影中。</div>}</div>
        <div className="aside-section"><div className="aside-heading">世界资源 <Icon name="box" size={14} /></div>{state && Object.keys(state.resources ?? {}).length ? <div className="resource-list">{Object.values(state.resources).slice(0, 4).map((resource) => <div className="resource-row" key={resource.resource_id}><span>{resource.resource_id}</span><strong>{resource.balance}</strong></div>)}</div> : <div className="aside-empty">尚无可显示的资源账目。</div>}</div>
        <div className="aside-section aside-snapshot"><div className="aside-heading">调用概览 <Icon name="pulse" size={14} /></div><p>运行用量与模型信息仅在后台返回时显示。</p><button className="text-button" onClick={() => window.dispatchEvent(new CustomEvent<Page>('world-engine:navigate', { detail: 'operations' }))}>打开模型后台 <Icon name="arrow-right" size={14} /></button></div>
      </aside>
    </div>
  </section>
}

function StatLine({ label, value }: { label: string; value: string }) { return <div className="stat-line"><span>{label}</span><strong>{value}</strong></div> }

function jobStatusLabel(status: string) { return ({ queued: '等待中', running: '处理中', waiting_input: '等待输入', succeeded: '已完成', failed: '失败', cancelled: '已取消', unavailable: '状态未知' } as Record<string, string>)[status] ?? status }
