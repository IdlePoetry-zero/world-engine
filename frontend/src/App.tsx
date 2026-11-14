import { useCallback, useEffect, useState } from 'react'
import { api, type Branch, type Pack, type Session, type World, type WorldView } from './api/client'
import { Icon, type IconName } from './components/Icon'
import { CreateWorldDialog } from './components/CreateWorldDialog'
import { EditorPage } from './pages/EditorPage'
import { HistoryPage } from './pages/HistoryPage'
import { LibraryPage } from './pages/LibraryPage'
import { OperationsPage } from './pages/OperationsPage'
import { PlayPage } from './pages/PlayPage'
import { errorMessage, toError, initials } from './utils'
import './App.css'

export type Page = 'library' | 'play' | 'world' | 'history' | 'operations'
export type Notice = { kind: 'success' | 'error' | 'info'; text: string }

const navigation: { page: Page; label: string; icon: IconName }[] = [
  { page: 'library', label: '世界库', icon: 'layers' },
  { page: 'play', label: '游玩', icon: 'spark' },
  { page: 'world', label: '设定档案', icon: 'book' },
  { page: 'history', label: '历史与分支', icon: 'timeline' },
]

export default function App() {
  const [page, setPage] = useState<Page>('library')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [session, setSession] = useState<Session | null>(null)
  const [worlds, setWorlds] = useState<World[]>([])
  const [packs, setPacks] = useState<Pack[]>([])
  const [selectedWorldId, setSelectedWorldId] = useState<string | null>(null)
  const [branchId, setBranchId] = useState<string | null>(null)
  const [branches, setBranches] = useState<Branch[]>([])
  const [view, setView] = useState<WorldView | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [createOpen, setCreateOpen] = useState(false)

  const selectedWorld = worlds.find((world) => world.world_id === selectedWorldId) ?? null
  const connected = session !== null && error === null

  const flash = useCallback((next: Notice) => {
    setNotice(next)
    window.setTimeout(() => setNotice((current) => current === next ? null : current), 4200)
  }, [])

  const loadWorkspace = useCallback(async () => {
    try {
      // The session response sets the local HttpOnly cookie. Load protected data only after bootstrap.
      const identity = await api.session()
      setSession(identity)
      const [worldResult, packResult] = await Promise.allSettled([api.listWorlds(), api.listPacks()])
      if (worldResult.status === 'fulfilled') {
        setWorlds(worldResult.value.items)
        setSelectedWorldId((current) => current && worldResult.value.items.some((world) => world.world_id === current)
          ? current
          : worldResult.value.items[0]?.world_id ?? null)
      } else throw worldResult.reason
      if (packResult.status === 'fulfilled') setPacks(packResult.value.items)
      setError(null)
    } catch (reason) {
      setError(toError(reason))
    } finally {
      setLoading(false)
    }
  }, [])

  const refreshWorkspace = useCallback(() => { setLoading(true); void loadWorkspace() }, [loadWorkspace])

  useEffect(() => { void loadWorkspace() }, [loadWorkspace])

  useEffect(() => {
    if (!selectedWorldId) return
    let active = true
    const selected = worlds.find((world) => world.world_id === selectedWorldId)
    Promise.allSettled([
      api.listBranches(selectedWorldId),
      api.worldView(selectedWorldId, branchId ?? selected?.active_branch_id ?? undefined),
    ]).then(([branchResult, viewResult]) => {
      if (!active) return
      if (branchResult.status === 'fulfilled') {
        setBranches(branchResult.value.items)
        const current = branchResult.value.items.find((branch) => branch.branch_id === (branchId ?? selected?.active_branch_id)) ?? branchResult.value.items[0]
        if (current && current.branch_id !== branchId) setBranchId(current.branch_id)
      }
      if (viewResult.status === 'fulfilled') setView(viewResult.value)
      const failure = [branchResult, viewResult].find((result) => result.status === 'rejected')
      if (failure?.status === 'rejected') setError(toError(failure.reason))
    })
    return () => { active = false }
  }, [selectedWorldId, branchId, worlds])

  useEffect(() => {
    const listener = (event: Event) => {
      const target = (event as CustomEvent<Page>).detail
      if (target) setPage(target)
    }
    window.addEventListener('world-engine:navigate', listener)
    return () => window.removeEventListener('world-engine:navigate', listener)
  }, [])

  const openWorld = (id: string) => {
    const world = worlds.find((item) => item.world_id === id)
    setSelectedWorldId(id)
    setBranchId(world?.active_branch_id ?? null)
    setPage('play')
    setDrawerOpen(false)
  }

  const createWorld = async (draft: { pack_id: string; title: string; player_name: string }, idempotencyKey: string) => {
    const result = await api.createWorld(draft, idempotencyKey)
    setCreateOpen(false)
    flash({ kind: 'success', text: `世界「${result.title}」已创建。` })
    await loadWorkspace()
    if (result.world_id) {
      setSelectedWorldId(result.world_id)
      setBranchId(result.active_branch_id)
      setPage('play')
    }
  }

  const setBranch = (id: string) => setBranchId(id || null)
  const selectedPage = navigation.find((item) => item.page === page)?.label ?? '模型与运行'

  return (
    <div className="app-shell">
      <div className={`mobile-scrim ${drawerOpen ? 'visible' : ''}`} onClick={() => setDrawerOpen(false)} />
      <aside className={`sidebar ${drawerOpen ? 'open' : ''}`} aria-label="主导航">
        <div className="brand-lockup"><div className="brand-mark" aria-hidden="true"><span /><span /><span /></div><div><div className="brand-name">WORLD ENGINE</div><div className="brand-caption">世界引擎 · 本地工作台</div></div><button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setDrawerOpen(false)}><Icon name="close" /></button></div>
        <button className="workspace-switch" onClick={() => setPage('library')} aria-label="返回世界库"><span className="workspace-glyph"><Icon name="orbit" size={18} /></span><span className="workspace-copy"><strong>个人工作区</strong><small>{session?.principal.display_name ?? '本地身份'}</small></span><Icon name="chevrons" size={16} className="muted-icon" /></button>
        <div className="nav-section-label">工作空间</div>
        <nav className="primary-nav">{navigation.map((item) => <button key={item.page} className={`nav-item ${page === item.page ? 'active' : ''}`} onClick={() => { setPage(item.page); setDrawerOpen(false) }} aria-current={page === item.page ? 'page' : undefined}><Icon name={item.icon} size={18} /><span>{item.label}</span>{item.page === 'library' && worlds.length > 0 && <span className="nav-count">{worlds.length}</span>}</button>)}</nav>
        <div className="nav-section-label nav-section-tools">系统</div><nav className="primary-nav"><button className={`nav-item ${page === 'operations' ? 'active' : ''}`} onClick={() => { setPage('operations'); setDrawerOpen(false) }} aria-current={page === 'operations' ? 'page' : undefined}><Icon name="sliders" size={18} /><span>模型与运行</span></button></nav>
        <div className="sidebar-bottom"><div className={`connection-card ${connected ? 'connected' : ''}`}><span className="connection-dot" /><span><strong>{connected ? '本地引擎已连接' : '等待本地引擎'}</strong><small>{connected ? '连接正常' : loading ? '正在检查 API 状态' : '连接不可用'}</small></span><button className="connection-refresh" aria-label="重新连接" onClick={refreshWorkspace} disabled={loading}><Icon name="refresh" size={15} /></button></div><div className="sidebar-footer"><span>LOCAL FIRST</span><span className="footer-separator" /><span>1.0</span></div></div>
      </aside>

      <main className="main-column">
        <header className="topbar"><div className="topbar-leading"><button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setDrawerOpen(true)}><Icon name="menu" /></button><div className="breadcrumb"><span>世界引擎</span><Icon name="chevron" size={13} /><strong>{selectedPage}</strong></div></div><div className="topbar-actions">{selectedWorld && <button className="context-pill" onClick={() => setPage('play')}><span className="context-led" />{selectedWorld.title}<Icon name="chevron" size={13} /></button>}<span className={`api-status ${connected ? 'online' : 'offline'}`}><i />{connected ? '本地 API 正常' : 'API 离线'}</span><button className="avatar-button" title={session?.principal.display_name ?? '本地身份'}>{initials(session?.principal.display_name ?? 'W E')}</button></div></header>
        <div className="page-frame">
          {page === 'library' && <LibraryPage worlds={worlds} packs={packs} loading={loading} error={error} onSelect={openWorld} onCreate={() => setCreateOpen(true)} onRetry={refreshWorkspace} />}
          {page === 'play' && <PlayPage key={`${selectedWorldId ?? 'none'}:${branchId ?? 'none'}`} world={selectedWorld} view={view} branches={branches} branchId={branchId} loading={Boolean(selectedWorldId && (!view || view.world.world_id !== selectedWorldId || (branchId && view.branch.branch_id !== branchId)))} onSelectWorld={() => setPage('library')} onOpenHistory={() => setPage('history')} onBranchChange={setBranch} onNotice={flash} onRefresh={refreshWorkspace} />}
          {page === 'world' && <EditorPage key={`${selectedWorldId ?? 'none'}:${branchId ?? 'none'}`} world={selectedWorld} branchId={branchId} onSelectWorld={() => setPage('library')} />}
          {page === 'history' && <HistoryPage key={`${selectedWorldId ?? 'none'}:${branchId ?? 'none'}`} world={selectedWorld} branches={branches} branchId={branchId} onBranchChange={setBranch} onRefresh={async () => { if (selectedWorldId) { const result = await api.listBranches(selectedWorldId); setBranches(result.items) } }} onNotice={flash} />}
          {page === 'operations' && <OperationsPage onNotice={flash} />}
        </div>
      </main>
      {createOpen && <CreateWorldDialog packs={packs} onClose={() => setCreateOpen(false)} onSubmit={createWorld} />}
      {notice && <div className={`toast toast-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}><Icon name={notice.kind === 'error' ? 'alert' : notice.kind === 'success' ? 'check' : 'info'} size={17} /><span>{notice.text}</span><button className="toast-close" aria-label="关闭通知" onClick={() => setNotice(null)}><Icon name="close" size={15} /></button></div>}
      {error && <div className="global-error" role="status"><Icon name="cloud-off" size={15} /><span>{errorMessage(error)}</span><button onClick={refreshWorkspace}>重试</button></div>}
    </div>
  )
}

