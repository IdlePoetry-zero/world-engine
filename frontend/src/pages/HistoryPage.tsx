import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { HistoryEvent, World, Branch, RestorePreview, MediaManifest, JobStatusRecord } from '../api/client'
import { api, ApiError, confirmRestore, previewRestore } from '../api/client'
import { errorMessage } from '../utils'
import { Icon } from '../components/Icon'
import { InlineState, PageEmpty } from '../components/States'
import { HistoryMediaControl } from '../components/HistoryMediaControl'
import { TemplateUpdatePanel } from '../components/TemplateUpdatePanel'

export function BackupPreview() {
  const [summary, setSummary] = useState<RestorePreview | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState<'verify' | 'restore' | null>(null)
  const [archive, setArchive] = useState<File | null>(null)
  const [targetVersion, setTargetVersion] = useState('')
  const [destination, setDestination] = useState('')
  const [restoredVersion, setRestoredVersion] = useState('')
  const [copyStatus, setCopyStatus] = useState('')
  const successRef = useRef<HTMLDivElement>(null)
  const commandRef = useRef<HTMLTextAreaElement>(null)
  const busy = pending !== null
  const upgrades = summary?.ruleset_upgrades ?? []
  const selectedUpgrade = upgrades.find((upgrade) => upgrade.target_version === targetVersion)

  useEffect(() => {
    if (destination) successRef.current?.focus()
  }, [destination])

  const launchCommand = destination
    ? `& ./.runtime/venv313/Scripts/python.exe -m world_engine.runtime --data-directory ${quotePowerShell(destination)} --port 8001`
    : ''

  async function verify(file: File) {
    setSummary(null)
    setArchive(null)
    setTargetVersion('')
    setDestination('')
    setRestoredVersion('')
    setCopyStatus('')
    setError('')
    setPending('verify')
    try {
      setSummary(await previewRestore(file))
      setArchive(file)
    }
    catch (reason) { setError(errorMessage(reason)) }
    finally { setPending(null) }
  }
  async function restore() {
    if (!summary || !archive || busy || destination) return
    setError('')
    setPending('restore')
    try {
      const result = await confirmRestore(archive, selectedUpgrade?.preview_id ?? summary.preview_id, selectedUpgrade?.target_version)
      setDestination(result.data_directory)
      if (selectedUpgrade) setRestoredVersion(result.ruleset_version ?? selectedUpgrade.target_version)
    } catch (reason) { setError(errorMessage(reason)) }
    finally { setPending(null) }
  }

  async function copyLaunchCommand() {
    if (!launchCommand) return
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable')
      await navigator.clipboard.writeText(launchCommand)
      setCopyStatus('启动命令已复制。请在仓库根目录的 PowerShell 中粘贴并运行。')
    } catch {
      commandRef.current?.focus()
      commandRef.current?.select()
      setCopyStatus('无法访问剪贴板，命令已选中；请按 Ctrl+C 复制。')
    }
  }
  return <div>
    <label htmlFor="backup-preview-file">验证备份归档</label>
    <input id="backup-preview-file" type="file" accept=".webackup,application/zip" disabled={busy}
      onChange={(event) => {
        const file = event.currentTarget.files?.[0]
        event.currentTarget.value = ''
        if (file) void verify(file)
      }} />
    <div role="status">{pending === 'verify' ? '正在校验归档及回放…' : pending === 'restore' ? '正在恢复独立副本…' : summary ?
      `已验证「${summary.title}」，${summary.branch_count} 个分支，${summary.event_count} 条事件。` : ''}</div>
    {summary?.ruleset_version && summary.pack_version && <p className="restore-port-note">
      {upgrades.length > 0
        ? `归档使用规则 ${summary.ruleset_version} 和内容 ${summary.pack_version}。`
        : `副本沿用规则 ${summary.ruleset_version} 和内容 ${summary.pack_version}。`}
      {summary.has_source_world_pack ? '归档包含作者源世界包。' : ''}
    </p>}
    {summary && upgrades.length > 0 && !destination && <div>
      <label htmlFor="backup-restore-ruleset">副本规则版本</label>
      <select id="backup-restore-ruleset" className="branch-select" value={targetVersion} disabled={busy}
        aria-describedby={selectedUpgrade ? 'backup-restore-ruleset-summary' : undefined}
        onChange={(event) => {
          setTargetVersion(event.currentTarget.value)
          setError('')
        }}>
        <option value="">沿用原规则{summary.ruleset_version ? ` ${summary.ruleset_version}` : ''}</option>
        {upgrades.map((upgrade) => <option key={upgrade.target_version} value={upgrade.target_version}>升级至 {upgrade.target_version}</option>)}
      </select>
      {selectedUpgrade && <p id="backup-restore-ruleset-summary" role="status">{selectedUpgrade.summary}</p>}
    </div>}
    {summary && archive && !destination && <button type="button" className="button button-secondary" disabled={busy}
      onClick={() => void restore()}>{selectedUpgrade ? '升级规则并恢复独立副本' : '恢复为独立副本'}</button>}
    {destination && <div className="restore-success" role="region" aria-labelledby="restore-success-title" tabIndex={-1} ref={successRef}>
      <div className="restore-success-heading"><Icon name="check" size={17} /><div><h3 id="restore-success-title">独立副本已恢复</h3><p>源世界保持不变。恢复服务尚未启动；请按以下步骤手动运行副本。</p></div></div>
      {restoredVersion && <div className="restore-directory"><span>副本规则版本</span><code>{restoredVersion}</code></div>}
      <div className="restore-directory"><span>数据目录</span><code>{destination}</code></div>
      <ol className="restore-launch-steps">
        <li>在仓库根目录打开 PowerShell。</li>
        <li>复制并运行下方命令。它会在端口 <code>8001</code> 启动此独立副本。</li>
        <li>在浏览器打开 <a href="http://127.0.0.1:8001" target="_blank" rel="noreferrer">http://127.0.0.1:8001</a>。</li>
      </ol>
      <label className="restore-command-label" htmlFor="restore-launch-command">PowerShell 启动命令</label>
      <textarea id="restore-launch-command" ref={commandRef} className="restore-command" value={launchCommand} readOnly rows={3} onFocus={(event) => event.currentTarget.select()} />
      <button type="button" className="button button-secondary button-small restore-copy-button" onClick={() => void copyLaunchCommand()}><Icon name="copy" size={14} />复制启动命令</button>
      <p className="restore-copy-status" role="status" aria-live="polite">{copyStatus}</p>
      <p className="restore-port-note">该副本使用独立数据目录和 8001 端口，不会自动打开进程。</p>
    </div>}
    {error && <p role="alert">{error}</p>}
  </div>
}

function quotePowerShell(value: string) {
  return `'${value.replaceAll("'", "''")}'`
}

type ForkPoint = { branchId: string; sequence: string; source: string }
type ForkRequestBody = { branch_id: string; sequence: string; label: string }
type ForkAttempt = { body: ForkRequestBody; key: string }

export function HistoryPage({ world, branches, branchId, onBranchChange, onBranchCreated }: { world: World | null; branches: Branch[]; branchId: string | null; onBranchChange: (id: string) => void; onBranchCreated: (worldId: string, branchId: string, label: string, sourceBranchId: string) => Promise<void> }) {
  const [historyLoad, setHistoryLoad] = useState<{ key: string; attempt: number; items: HistoryEvent[]; cursor: string | null; mode?: 'author' | 'player'; hasSourcePack?: boolean; loading: boolean; error?: string } | null>(null)
  const [reloadAttempt, setReloadAttempt] = useState(0)
  const [loadMoreScope, setLoadMoreScope] = useState<string | null>(null)
  const [loadMoreError, setLoadMoreError] = useState<{ key: string; message: string } | null>(null)
  const historyGeneration = useRef(0)
  const [forkPoint, setForkPoint] = useState<ForkPoint | null>(null)
  const [forkLabel, setForkLabel] = useState('')
  const [forkError, setForkError] = useState<string | null>(null)
  const [forkBusy, setForkBusy] = useState(false)
  const [forkAttempt, setForkAttempt] = useState<ForkAttempt | null>(null)
  const [acceptedFork, setAcceptedFork] = useState<Branch | null>(null)
  const [mediaManifests, setMediaManifests] = useState<MediaManifest[] | null>(null)
  const [mediaManifestError, setMediaManifestError] = useState('')
  const [branchJobLoad, setBranchJobLoad] = useState<{ key: string; items: JobStatusRecord[]; error?: string } | null>(null)
  const scopeEpoch = useRef(0)

  const worldId = world?.world_id
  const scopeKey = worldId && branchId ? `${worldId}:${branchId}` : ''
  const visibleHistory = historyLoad?.key === scopeKey && historyLoad.attempt === reloadAttempt ? historyLoad : null
  const canManageHistory = visibleHistory?.mode === 'author'
  const events = visibleHistory?.items ?? []
  const visibleJobLoad = canManageHistory && branchJobLoad?.key === scopeKey ? branchJobLoad : null
  const visibleJobs = visibleJobLoad?.items ?? []
  const cursor = visibleHistory?.cursor ?? null
  const loading = Boolean(scopeKey && (!visibleHistory || visibleHistory.loading))
  const error = visibleHistory?.error ?? null
  const loadingMore = loadMoreScope === scopeKey
  const pageError = loadMoreError?.key === scopeKey ? loadMoreError.message : null
  const currentBranch = branches.find((branch) => branch.branch_id === branchId) ?? null
  const hasMatchingMediaSubscription = Boolean(canManageHistory && mediaManifests && branchId && events.some((event) =>
    event.branch_id === branchId && mediaManifests.some((manifest) =>
      manifest.subscriptions.some((subscription) => subscription.event_type === event.type))))

  useEffect(() => {
    const epoch = ++scopeEpoch.current
    return () => { if (scopeEpoch.current === epoch) scopeEpoch.current += 1 }
  }, [scopeKey])

  useEffect(() => {
    if (!canManageHistory) return
    let active = true
    void api.extensions().then((result) => {
      if (active) {
        setMediaManifests(result.items)
        setMediaManifestError('')
      }
    }).catch((reason) => {
      if (active) {
        setMediaManifests(null)
        setMediaManifestError(errorMessage(reason))
      }
    })
    return () => { active = false }
  }, [canManageHistory])

  const beginFork = (sourceBranchId: string, sequence: string, source: string) => {
    setForkPoint({ branchId: sourceBranchId, sequence, source })
    setForkLabel(`分支 ${sequence}`)
    setForkError(null)
    setForkAttempt(null)
    setAcceptedFork(null)
  }

  const clearFork = () => {
    if (forkBusy || acceptedFork) return
    setForkPoint(null)
    setForkError(null)
    setForkAttempt(null)
  }

  const syncAcceptedFork = async () => {
    if (!worldId || !acceptedFork || forkBusy) return
    const epoch = scopeEpoch.current
    const sourceBranchId = forkPoint?.branchId
    if (!sourceBranchId) return
    setForkBusy(true)
    setForkError(null)
    try {
      await onBranchCreated(worldId, acceptedFork.branch_id, acceptedFork.label, sourceBranchId)
      if (scopeEpoch.current !== epoch) return
      setForkPoint(null)
      setForkAttempt(null)
      setAcceptedFork(null)
    } catch (reason) {
      if (scopeEpoch.current === epoch) setForkError(`分支已创建，但无法刷新分支列表：${errorMessage(reason)}`)
    } finally { if (scopeEpoch.current === epoch) setForkBusy(false) }
  }

  const submitFork = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!worldId || !forkPoint || forkBusy || acceptedFork) return
    const label = forkLabel.trim()
    if (!label || label.length > 160) return
    const body: ForkRequestBody = { branch_id: forkPoint.branchId, sequence: forkPoint.sequence, label }
    const sameRequest = forkAttempt && forkAttempt.body.branch_id === body.branch_id
      && forkAttempt.body.sequence === body.sequence && forkAttempt.body.label === body.label
    const attempt = sameRequest ? forkAttempt : { body, key: crypto.randomUUID() }
    const epoch = scopeEpoch.current
    setForkAttempt(attempt)
    setForkBusy(true)
    setForkError(null)
    try {
      const created = await api.createBranch(worldId, attempt.body, attempt.key)
      if (scopeEpoch.current !== epoch) {
        await onBranchCreated(worldId, created.branch_id, created.label, forkPoint.branchId)
        return
      }
      setAcceptedFork(created)
      try {
        await onBranchCreated(worldId, created.branch_id, created.label, forkPoint.branchId)
        if (scopeEpoch.current !== epoch) return
        setForkPoint(null)
        setForkAttempt(null)
        setAcceptedFork(null)
      } catch (reason) {
        if (scopeEpoch.current === epoch) setForkError(`分支「${created.label}」已创建，但无法刷新分支列表：${errorMessage(reason)}`)
      }
    } catch (reason) {
      if (scopeEpoch.current !== epoch) return
      setForkError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status < 500 && !reason.retryable) setForkAttempt(null)
    } finally { if (scopeEpoch.current === epoch) setForkBusy(false) }
  }
  useEffect(() => {
    if (!worldId || !branchId) return
    let active = true
    const generation = ++historyGeneration.current
    void api.history(worldId, branchId).then((result) => {
      if (active && generation === historyGeneration.current) {
        setHistoryLoad({ key: scopeKey, attempt: reloadAttempt, items: result.items, cursor: result.next_cursor,
          mode: result.mode, hasSourcePack: result.has_source_world_pack, loading: false })
        setLoadMoreError(null)
      }
    }).catch((reason) => {
      if (active && generation === historyGeneration.current) setHistoryLoad({ key: scopeKey, attempt: reloadAttempt, items: [], cursor: null, loading: false, error: errorMessage(reason) })
    }).finally(() => {
      if (active && generation === historyGeneration.current) setLoadMoreScope((current) => current === scopeKey ? null : current)
    })
    return () => { active = false; if (historyGeneration.current === generation) historyGeneration.current += 1 }
  }, [worldId, branchId, scopeKey, reloadAttempt])

  useEffect(() => {
    if (!canManageHistory || !worldId || !branchId) return
    const jobScope = `${worldId}:${branchId}`
    let active = true
    void api.listJobs(worldId, branchId).then((result) => {
      if (active) setBranchJobLoad({ key: jobScope, items: result.items })
    }).catch((reason) => {
      if (active) setBranchJobLoad({ key: jobScope, items: [], error: errorMessage(reason) })
    })
    return () => { active = false }
  }, [worldId, branchId, canManageHistory])

  const loadMore = async () => {
    if (!worldId || !branchId || !cursor || loadingMore || !visibleHistory) return
    const requestScope = scopeKey
    const attempt = reloadAttempt
    const generation = historyGeneration.current
    setLoadMoreScope(requestScope)
    setLoadMoreError(null)
    try {
      const result = await api.history(worldId, branchId, cursor)
      if (generation !== historyGeneration.current) return
      setHistoryLoad((current) => current?.key === requestScope && current.attempt === attempt
        ? { ...current, items: [...current.items, ...result.items], cursor: result.next_cursor, mode: result.mode }
        : current)
    } catch (reason) {
      if (generation === historyGeneration.current) setLoadMoreError({ key: requestScope, message: errorMessage(reason) })
    } finally {
      if (generation === historyGeneration.current) setLoadMoreScope((current) => current === requestScope ? null : current)
    }
  }

  const retry = () => {
    if (!worldId || !branchId) return
    setReloadAttempt((attempt) => attempt + 1)
  }

  if (!world) return <PageEmpty eyebrow="历史档案" title="选择一个世界以查看历史。" body="事件与分支只会在后端返回后显示。" />
  const canForkHead = Boolean(currentBranch && /^(0|[1-9]\d*)$/.test(currentBranch.head_seq))
  return <section className="history-page page-enter">
    {canManageHistory && <>
      <a className="button button-secondary button-small" href={`/api/v1/worlds/${encodeURIComponent(world.world_id)}/backup?format_version=2.0`} download>
        <Icon name="download" size={15} />下载世界备份
      </a>
      {visibleHistory?.hasSourcePack && <a className="button button-secondary button-small"
        href={`/api/v1/worlds/${encodeURIComponent(world.world_id)}/source-world-pack`} download>
        <Icon name="download" size={15} />下载源世界包（含作者资料）
      </a>}
      {visibleHistory?.hasSourcePack && branchId && <TemplateUpdatePanel key={scopeKey} worldId={world.world_id}
        branchId={branchId} branchLabel={currentBranch?.label || branchId} onBranchCreated={onBranchCreated} />}
      <BackupPreview />
    </>}
    <div className="history-heading">
      <div><div className="eyebrow"><span className="eyebrow-line" />时间留下的痕迹</div><h1>历史与分支</h1><p>每条记录来自当前世界分支的事件日志。</p></div>
      <div className="history-actions">
        <select className="branch-select" aria-label="选择历史分支" value={branchId ?? ''} onChange={(event) => onBranchChange(event.target.value)}>{branches.length ? branches.map((branch) => <option key={branch.branch_id} value={branch.branch_id}>{branch.label || branch.branch_id.slice(0, 8)}</option>) : <option value="">暂无分支</option>}</select>
        {currentBranch && visibleHistory?.mode && <a className="button button-secondary button-small"
          href={`/api/v1/worlds/${encodeURIComponent(world.world_id)}/player-report?branch_id=${encodeURIComponent(currentBranch.branch_id)}`} download>
          <Icon name="download" size={15} />下载玩家报告
        </a>}
      </div>
    </div>
    <div className="history-layout">
      <div className="timeline-panel">
        <div className="timeline-panel-head"><div><span className="panel-overline">EVENT LEDGER</span><h2>事件记录</h2></div><span className="history-count" role="status" aria-live="polite">{loading ? '同步中' : `${events.length} 条记录`}</span></div>
        {canManageHistory && mediaManifestError && <div className="history-media-capability-note" role="status">媒体插件状态暂时无法读取；未提交任何媒体任务。</div>}
        {canManageHistory && visibleJobLoad?.error && <div className="history-media-capability-note" role="status">旧媒体任务状态无法读取：{visibleJobLoad.error}</div>}
        {canManageHistory && !mediaManifestError && mediaManifests && events.length > 0 && !hasMatchingMediaSubscription && <div className="history-media-capability-note" role="status">当前显示的事件没有匹配的已安装媒体插件。配置并启用对应插件后，可在支持的事件上制作音频或图片。</div>}
        {error ? <InlineState icon="alert" title="无法读取历史" description={error} action={<button className="button button-secondary button-small" onClick={retry}>重试</button>} /> : loading ? <div className="event-loading" role="status" aria-live="polite"><Icon name="spinner" />正在读取事件日志…</div> : events.length ? <div className="timeline-list">{events.map((event) => {
            const matchingMedia = mediaManifests?.some((manifest) =>
              manifest.subscriptions.some((subscription) => subscription.event_type === event.type)) ?? false
            const restoredMediaJob = visibleJobs.find((item) => item.job_type === 'media.generate'
              && item.media_request?.source_event_id === event.event_id)
            return <article className="timeline-event" key={`${branchId ?? 'none'}:${event.event_id}`}>
            <div className="timeline-rail"><span className="timeline-dot" /></div>
            <div className="timeline-event-body">
              <div className="event-meta"><span className="event-seq">#{event.sequence}</span><span className="event-time">{event.tick} tick</span><span className="event-kind">{event.type}</span>
                {canManageHistory && event.can_fork && branchId && <button type="button" className="event-fork-button" onClick={() => beginFork(branchId, event.sequence, `第 ${event.sequence} 条提交 · ${event.tick} tick`)} disabled={forkBusy || Boolean(acceptedFork)}><Icon name="branch" size={12} />从此处分支</button>}
              </div>
              <h3>{eventTitle(event.type)}</h3><p>{eventSummary(event.type, event.payload)}</p>
              {canManageHistory && event.branch_id === branchId && visibleJobLoad && (matchingMedia || restoredMediaJob) && worldId && branchId && <HistoryMediaControl
                key={`${event.event_id}:${restoredMediaJob?.job_id ?? 'new'}`}
                worldId={worldId} branchId={branchId} event={event} manifests={mediaManifests ?? []} initialJob={restoredMediaJob} />}
              <details><summary>查看事件载荷</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>
            </div>
          </article>})}</div>
        : !error && !loading && <div className="history-empty"><div className="empty-orbit small-orbit"><span className="empty-orbit-core" /><i /><i /></div><strong>{cursor ? '本页没有可显示的事件。' : '这条时间线上还没有事件。'}</strong><span>{cursor ? '仍可读取后续记录。' : '世界提交事件后会出现在这里。查询不会创建世界事件。'}</span></div>}
        {!error && !loading && cursor && <button className="button button-secondary load-more" onClick={() => void loadMore()} disabled={loadingMore} aria-label={loadingMore ? '正在读取更多历史事件' : '加载更多历史事件'}>{loadingMore ? <Icon name="spinner" size={15} /> : <Icon name="chevrons" size={15} />}{loadingMore ? '正在读取…' : '读取后续记录'}</button>}
        {!error && !loading && pageError && <div className="history-page-error" role="alert"><span>读取后续记录失败：{pageError}</span><button className="text-button" onClick={() => void loadMore()} disabled={loadingMore}>重试</button></div>}
      </div>
      <aside className="branch-panel">
        <div className="branch-panel-head"><div><span className="panel-overline">TIMELINE FORK</span><h2>分支</h2></div><span className="branch-total">{branches.length}</span></div>
        {branches.length ? <div className="branch-list">{branches.map((branch) => <button className={`branch-card ${branch.branch_id === branchId ? 'current' : ''}`} key={branch.branch_id} onClick={() => onBranchChange(branch.branch_id)}><span className="branch-card-icon"><Icon name="branch" size={16} /></span><span><strong>{branch.label || '未命名分支'}</strong><small>{branch.branch_id.slice(0, 8)} · {branch.current_tick} tick</small></span>{branch.branch_id === branchId && <span className="current-branch-tag">当前</span>}</button>)}</div> : <div className="branch-none">当前世界尚未返回分支记录。</div>}
        {canManageHistory && canForkHead && currentBranch && <button type="button" className="button button-secondary head-fork-button" onClick={() => beginFork(currentBranch.branch_id, currentBranch.head_seq, `当前分支末端 · ${currentBranch.current_tick} tick`)} disabled={forkBusy || Boolean(acceptedFork)}><Icon name="branch" size={14} />从当前末端创建分支</button>}
        {canManageHistory && forkPoint && <form className="new-branch-form fork-form" onSubmit={(event) => void submitFork(event)}>
          <div className="fork-form-title"><span>创建分支</span><button type="button" className="icon-button" aria-label="关闭分支表单" onClick={clearFork} disabled={forkBusy || Boolean(acceptedFork)}><Icon name="close" size={14} /></button></div>
          <p className="fork-source">基于 {forkPoint.source}，只接受完整提交点。</p>
          <label htmlFor="fork-label">分支名称</label>
          <input id="fork-label" value={forkLabel} maxLength={160} onChange={(event) => { setForkLabel(event.target.value); setForkError(null) }} disabled={forkBusy || Boolean(forkAttempt) || Boolean(acceptedFork)} aria-describedby="fork-label-help" />
          <small id="fork-label-help">名称最多 160 个字符。此操作会保留原分支和历史。</small>
          {forkError && <p className="fork-error" role="alert">{forkError}</p>}
          {acceptedFork ? <button type="button" className="button button-secondary button-small fork-submit" onClick={() => void syncAcceptedFork()} disabled={forkBusy}>{forkBusy ? <Icon name="spinner" size={14} /> : <Icon name="refresh" size={14} />}重新读取分支并打开</button> : <button type="submit" className="button button-primary button-small fork-submit" disabled={forkBusy || !forkLabel.trim()}>{forkBusy ? <Icon name="spinner" size={14} /> : <Icon name="branch" size={14} />}{forkBusy ? '正在创建…' : forkAttempt ? '使用相同请求安全重试' : '创建独立分支'}</button>}
        </form>}
        {canManageHistory && <div className="branch-note"><Icon name="info" size={14} /><span>可从完整命令提交点或当前分支末端建立新分支。原分支与事件历史会保留。</span></div>}
      </aside>
    </div>
  </section>
}

const knownEventTitles: Record<string, string> = {
  author_component_revised: '作者修订组件', world_created: '创建世界', branch_created: '创建分支',
  entity_created: '创建实体', character_created: '创建人物', resource_produced: '增加资源',
  resource_consumed: '消耗资源', goal_created: '创建目标', relationship_established: '建立关系',
  relation_established: '建立关系', relation_dimension_changed: '调整关系数值',
  knowledge_acquired: '获得知识',
  combat_exchange_resolved: '结算交战', combat_retreat_resolved: '撤离交战',
  combat_truce_changed: '遭遇停战',
  cultivation_breakthrough_resolved: '修炼突破',
  waypoint_opportunity_decision_recorded: '中途停留选择',
  governance_leadership_handed_over: '移交领导职务', organization_member_resigned: '退出势力',
  meeting_reflection_recorded: '记录会面后的私人感受',
  relationship_partnership_changed: '处理伴侣关系',
  relationship_disclosure_recorded: '记录关系告知',
  investigation_public_accusation_corrected: '更正公开指认',
  accusation_repair_offer_recorded: '交出解释',
  accusation_repair_resolved: '回应解释',
  shipment_relief_distributed: '发放到货粮食',
}

function eventTitle(type: string) {
  const name = type.split('.').filter((segment) => !/^v\d+$/i.test(segment)).at(-1) ?? type
  return knownEventTitles[name] ?? name.replaceAll('_', ' ')
}

function eventSummary(type: string, payload: unknown) {
  if (!payload || typeof payload !== 'object') return '事件载荷暂无摘要。'
  const fields = payload as Record<string, unknown>
  if (type === 'core.waypoint_opportunity_decision_recorded.v1') {
    const record = typeof fields.record === 'object' && fields.record !== null ? fields.record as Record<string, unknown> : fields
    if (record.choice === 'stop') return '玩家选择在已抵达的地点停留。'
    if (record.choice === 'continue') return '玩家选择继续原行程。'
  }
  if (type === 'core.combat_truce_changed.v1') {
    if (fields.operation === 'offer') return '一方提出停战，等待对方回应。'
    if (fields.operation === 'accept') return '对方接受，双方结束本次交战。'
    if (fields.operation === 'decline') return '对方拒绝，本次交战仍在继续。'
  }
  if (type === 'core.cultivation_breakthrough_resolved.v1') {
    const result = 'plan' in fields && typeof fields.plan === 'object' && fields.plan !== null
      ? fields.plan as Record<string, unknown> : fields
    if (result.outcome === 'succeeded') {
      const realm = result.target_realm === 'qi_gathering' ? '聚气' : result.target_realm === 'foundation' ? '筑基' : null
      return realm ? '突破成功，已进入' + realm + '境。' : '突破成功，境界已晋升。'
    }
    if (result.outcome === 'failed') return '突破失败，已结算灵气消耗和伤势。'
  }
  const entries = Object.entries(fields).slice(0, 3)
  return entries.map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ') || '事件载荷暂无摘要。'
}
