import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type Branch, type Pack, type Session, type World, type WorldView } from './api/client'
import { Icon, type IconName } from './components/Icon'
import { CreateWorldDialog, type CreateWorldDraft } from './components/CreateWorldDialog'
import { useModalKeyboard } from './components/useModalKeyboard'
import { HistoryPage } from './pages/HistoryPage'
import { LibraryPage } from './pages/LibraryPage'
import { errorMessage, toError, initials } from './utils'
import './App.css'

export type Page = 'library' | 'play' | 'world' | 'history' | 'operations'
export type Notice = { kind: 'success' | 'error' | 'info'; text: string }

const EditorPage = lazy(() => import('./pages/EditorPage').then(({ EditorPage }) => ({ default: EditorPage })))
const OperationsPage = lazy(() => import('./pages/OperationsPage').then(({ OperationsPage }) => ({ default: OperationsPage })))
const PlayPage = lazy(() => import('./pages/PlayPage').then(({ PlayPage }) => ({ default: PlayPage })))

const navigation: { page: Page; label: string; icon: IconName }[] = [
  { page: 'library', label: '世界库', icon: 'layers' },
  { page: 'play', label: '游玩', icon: 'spark' },
  { page: 'world', label: '设定档案', icon: 'book' },
  { page: 'history', label: '历史与分支', icon: 'timeline' },
]

export default function App() {
  const [page, setPage] = useState<Page>('library')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 900px)').matches)
  const drawerRef = useRef<HTMLElement>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [worlds, setWorlds] = useState<World[]>([])
  const [packs, setPacks] = useState<Pack[]>([])
  const [packStatus, setPackStatus] = useState<'loading' | 'ready' | 'failed'>('loading')
  const packRequest = useRef(0)
  const [selectedWorldId, setSelectedWorldId] = useState<string | null>(null)
  const [branchId, setBranchId] = useState<string | null>(null)
  const [branchList, setBranchList] = useState<{ worldId: string; items: Branch[]; failed?: boolean } | null>(null)
  const [branchRefresh, setBranchRefresh] = useState(0)
  const [view, setView] = useState<WorldView | null>(null)
  const [viewFailure, setViewFailure] = useState<{ key: string; error: Error } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const mobileDrawerOpen = mobile && drawerOpen
  useModalKeyboard(drawerRef, { active: mobileDrawerOpen, inertBackground: false, busy: false, onClose: () => setDrawerOpen(false) })
  const activeSelection = useRef<{ worldId: string | null; branchId: string | null }>({ worldId: selectedWorldId, branchId })

  useEffect(() => { activeSelection.current = { worldId: selectedWorldId, branchId } }, [selectedWorldId, branchId])
  useEffect(() => {
    const query = window.matchMedia('(max-width: 900px)')
    const onChange = (event: MediaQueryListEvent) => {
      setMobile(event.matches)
      if (!event.matches) setDrawerOpen(false)
    }
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const selectedWorld = worlds.find((world) => world.world_id === selectedWorldId) ?? null
  const selectedBranches = branchList?.worldId === selectedWorldId ? branchList : null
  const branches = selectedBranches?.items ?? []
  const expectedBranchId = branchId ?? selectedWorld?.active_branch_id ?? null
  const viewMatchesSelection = Boolean(view && view.world.world_id === selectedWorldId && (!expectedBranchId || view.branch.branch_id === expectedBranchId))
  const viewKey = `${selectedWorldId ?? ''}:${expectedBranchId ?? ''}`
  const selectedViewError = viewFailure?.key === viewKey ? viewFailure.error : null
  const connected = session !== null && error === null
  const connectionLabel = connected ? '本地 API 正常' : loading ? '正在连接 API' : error instanceof ApiError ? 'API 请求失败' : 'API 离线'

  const flash = useCallback((next: Notice) => {
    setNotice(next)
    window.setTimeout(() => setNotice((current) => current === next ? null : current), 4200)
  }, [])

  const loadPacks = useCallback(async () => {
    const requestId = ++packRequest.current
    setPackStatus('loading')
    try {
      const result = await api.listPacks()
      if (requestId !== packRequest.current) return
      setPacks(result.items)
      setPackStatus('ready')
    } catch {
      if (requestId === packRequest.current) setPackStatus('failed')
    }
  }, [])

  const loadWorkspace = useCallback(async () => {
    try {
      // The session response sets the local HttpOnly cookie. Load protected data only after bootstrap.
      const identity = await api.session()
      setSession(identity)
      const [worldResult] = await Promise.allSettled([api.listWorlds(), loadPacks()])
      if (worldResult.status === 'fulfilled') {
        setWorlds(worldResult.value.items)
        setSelectedWorldId((current) => current && worldResult.value.items.some((world) => world.world_id === current)
          ? current
          : worldResult.value.items[0]?.world_id ?? null)
      } else throw worldResult.reason
      setError(null)
    } catch (reason) {
      setError(toError(reason))
      setPackStatus((status) => status === 'loading' ? 'failed' : status)
    } finally {
      setLoading(false)
    }
  }, [loadPacks])

  const refreshWorkspace = useCallback(() => { setLoading(true); void loadWorkspace() }, [loadWorkspace])

  useEffect(() => { const timer = window.setTimeout(() => void loadWorkspace(), 0); return () => window.clearTimeout(timer) }, [loadWorkspace])

  useEffect(() => {
    if (!selectedWorldId) return
    let active = true
    const selected = worlds.find((world) => world.world_id === selectedWorldId)
    const requestedBranchId = branchId ?? selected?.active_branch_id ?? undefined
    const requestedKey = `${selectedWorldId}:${requestedBranchId ?? ''}`
    Promise.allSettled([
      api.listBranches(selectedWorldId),
      api.worldView(selectedWorldId, requestedBranchId),
    ]).then(([branchResult, viewResult]) => {
      if (!active) return
      if (branchResult.status === 'fulfilled') {
        setBranchList({ worldId: selectedWorldId, items: branchResult.value.items })
        const current = branchResult.value.items.find((branch) => branch.branch_id === (branchId ?? selected?.active_branch_id)) ?? branchResult.value.items[0]
        if (current && current.branch_id !== branchId) {
          activeSelection.current = { worldId: selectedWorldId, branchId: current.branch_id }
          setBranchId(current.branch_id)
        }
      } else setBranchList({ worldId: selectedWorldId, items: [], failed: true })
      if (viewResult.status === 'fulfilled') { setView(viewResult.value); setViewFailure(null) }
      else setViewFailure({ key: requestedKey, error: toError(viewResult.reason) })
    })
    return () => { active = false }
  }, [selectedWorldId, branchId, worlds, branchRefresh])

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
    activeSelection.current = { worldId: id, branchId: world?.active_branch_id ?? null }
    setSelectedWorldId(id)
    setBranchId(world?.active_branch_id ?? null)
    setPage('play')
    setDrawerOpen(false)
  }

  const createWorld = async (draft: CreateWorldDraft, idempotencyKey: string) => {
    const result = await api.createWorld(draft, idempotencyKey)
    setCreateOpen(false)
    flash({ kind: 'success', text: `世界「${result.title}」已创建。` })
    await loadWorkspace()
    if (result.world_id) {
      activeSelection.current = { worldId: result.world_id, branchId: result.active_branch_id }
      setSelectedWorldId(result.world_id)
      setBranchId(result.active_branch_id)
      setPage('play')
    }
  }

  const setBranch = (id: string) => {
    if (!branches.some((branch) => branch.branch_id === id)) return
    activeSelection.current = { ...activeSelection.current, branchId: id }
    setBranchId(id)
  }
  const refreshBranchesAndSelect = useCallback(async (worldId: string, createdBranchId: string, label: string, sourceBranchId: string) => {
    if (activeSelection.current.worldId !== worldId || activeSelection.current.branchId !== sourceBranchId) return
    const result = await api.listBranches(worldId)
    if (activeSelection.current.worldId !== worldId || activeSelection.current.branchId !== sourceBranchId) return
    setBranchList({ worldId, items: result.items })
    if (!result.items.some((branch) => branch.branch_id === createdBranchId)) {
      throw new Error('分支已提交，但刷新结果中尚未出现新分支。')
    }
    if (activeSelection.current.worldId !== worldId || activeSelection.current.branchId !== sourceBranchId) return
    activeSelection.current = { worldId, branchId: createdBranchId }
    setBranchId(createdBranchId)
    flash({ kind: 'success', text: `分支「${label}」已创建并打开。` })
  }, [flash])
  const selectedPage = navigation.find((item) => item.page === page)?.label ?? '模型与运行'

  return (
    <div className="app-shell">
      <div className={`mobile-scrim ${mobileDrawerOpen ? 'visible' : ''}`} onClick={() => setDrawerOpen(false)} />
      <aside ref={drawerRef} id="workspace-navigation" tabIndex={-1} inert={mobile && !mobileDrawerOpen}
        className={`sidebar ${mobileDrawerOpen ? 'open' : ''}`} aria-label="主导航"
        role={mobileDrawerOpen ? 'dialog' : undefined} aria-modal={mobileDrawerOpen || undefined}>
        <div className="brand-lockup"><div className="brand-mark" aria-hidden="true"><span /><span /><span /></div><div><div className="brand-name">WORLD ENGINE</div><div className="brand-caption">世界引擎 · 本地工作台</div></div><button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setDrawerOpen(false)}><Icon name="close" /></button></div>
        <button className="workspace-switch" onClick={() => { setPage('library'); setDrawerOpen(false) }} aria-label="返回世界库"><span className="workspace-glyph"><Icon name="orbit" size={18} /></span><span className="workspace-copy"><strong>个人工作区</strong><small>{session?.principal.display_name ?? '本地身份'}</small></span><Icon name="chevrons" size={16} className="muted-icon" /></button>
        <div className="nav-section-label">工作空间</div>
        <nav className="primary-nav">{navigation.map((item) => <button key={item.page} className={`nav-item ${page === item.page ? 'active' : ''}`} onClick={() => { setPage(item.page); setDrawerOpen(false) }} aria-current={page === item.page ? 'page' : undefined}><Icon name={item.icon} size={18} /><span>{item.label}</span>{item.page === 'library' && worlds.length > 0 && <span className="nav-count">{worlds.length}</span>}</button>)}</nav>
        <div className="nav-section-label nav-section-tools">系统</div><nav className="primary-nav"><button className={`nav-item ${page === 'operations' ? 'active' : ''}`} onClick={() => { setPage('operations'); setDrawerOpen(false) }} aria-current={page === 'operations' ? 'page' : undefined}><Icon name="sliders" size={18} /><span>模型与运行</span></button></nav>
        <div className="sidebar-bottom"><div className={`connection-card ${connected ? 'connected' : ''}`}><span className="connection-dot" /><span><strong>{connected ? '本地引擎已连接' : '等待本地引擎'}</strong><small>{connected ? '连接正常' : loading ? '正在检查 API 状态' : '连接不可用'}</small></span><button className="connection-refresh" aria-label="重新连接" onClick={refreshWorkspace} disabled={loading}><Icon name="refresh" size={15} /></button></div><div className="sidebar-footer"><span>LOCAL FIRST</span><span className="footer-separator" /><span>1.0</span></div></div>
      </aside>

      <main className="main-column" inert={mobileDrawerOpen}>
        <header className="topbar"><div className="topbar-leading"><button className="icon-button mobile-menu" aria-label="打开导航" aria-controls="workspace-navigation" aria-expanded={mobileDrawerOpen} onClick={() => setDrawerOpen(true)}><Icon name="menu" /></button><div className="breadcrumb"><span>世界引擎</span><Icon name="chevron" size={13} /><strong>{selectedPage}</strong></div></div><div className="topbar-actions">{selectedWorld && <button className="context-pill" onClick={() => setPage('play')}><span className="context-led" />{selectedWorld.title}<Icon name="chevron" size={13} /></button>}<span className={`api-status ${connected ? 'online' : 'offline'}`}><i />{connectionLabel}</span><button className="avatar-button" title={session?.principal.display_name ?? '本地身份'}>{initials(session?.principal.display_name ?? 'W E')}</button></div></header>
        <div className="page-frame">
          {(page === 'play' || page === 'history') && selectedBranches?.failed &&
            <div className="inline-error" role="status"><span>未能读取当前世界的分支。</span><button onClick={() => { setBranchList(null); setBranchRefresh((value) => value + 1) }}>重试分支</button></div>}
          <Suspense fallback={<p className="quiet-loading" role="status">正在打开页面…</p>}>
          {page === 'library' && <LibraryPage worlds={worlds} packs={packs} loading={loading} error={error} onSelect={openWorld} onCreate={() => setCreateOpen(true)} onRetry={refreshWorkspace} />}
          {page === 'play' && <PlayPage key={`${selectedWorldId ?? 'none'}:${branchId ?? 'none'}`} world={selectedWorld} view={viewMatchesSelection ? view : null} viewError={selectedViewError?.message ?? null} branches={branches} branchId={branchId} loading={Boolean(selectedWorldId && !viewMatchesSelection && !selectedViewError)} onSelectWorld={() => setPage('library')} onOpenHistory={() => setPage('history')} onBranchChange={setBranch} onNotice={flash} onRefresh={refreshWorkspace} />}
          {page === 'world' && <EditorPage key={`${selectedWorldId ?? 'none'}:${branchId ?? 'none'}`} world={selectedWorld} branchId={branchId} onSelectWorld={() => setPage('library')} onNotice={flash} />}
          {page === 'history' && <HistoryPage key={`${selectedWorldId ?? 'none'}:${branchId ?? 'none'}`} world={selectedWorld} branches={branches} branchId={branchId} onBranchChange={setBranch} onBranchCreated={refreshBranchesAndSelect} />}
          {page === 'operations' && <OperationsPage onNotice={flash} />}
          </Suspense>
        </div>
      </main>
      {createOpen && <CreateWorldDialog packs={packs} packStatus={packStatus} onRetryPacks={() => void loadPacks()} onClose={() => setCreateOpen(false)} onSubmit={createWorld} />}
      {notice && <div className={`toast toast-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}><Icon name={notice.kind === 'error' ? 'alert' : notice.kind === 'success' ? 'check' : 'info'} size={17} /><span>{notice.text}</span><button className="toast-close" aria-label="关闭通知" onClick={() => setNotice(null)}><Icon name="close" size={15} /></button></div>}
      {error && <div className="global-error" role="status"><Icon name="cloud-off" size={15} /><span>{errorMessage(error)}</span><button onClick={refreshWorkspace}>重试</button></div>}
    </div>
  )
}

