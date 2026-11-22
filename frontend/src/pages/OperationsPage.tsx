import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, type CallRecord, type ModelInfo, type ModelRoute, type ModelRouteTarget, type Provider, type ProviderCredentialStatus, type ProviderDraft, type Settings } from '../api/client'
import { errorMessage } from '../utils'
import { Icon, type IconName } from '../components/Icon'
import { InlineState } from '../components/States'
import { TransportFailure, transportFailureCsvFields } from '../components/TransportFailure'
import { useModalKeyboard } from '../components/useModalKeyboard'
import type { Notice } from '../App'

type OpsTab = 'overview' | 'providers' | 'routes' | 'calls'
type SettingsMutation = (settings: Settings) => Settings
type SettingsUpdateJob = { mutation: SettingsMutation; resolve: (saved: boolean) => void }

export function OperationsPage({ onNotice }: { onNotice: (notice: Notice) => void }) {
  const [tab, setTab] = useState<OpsTab>('overview')
  const [settings, setSettings] = useState<Settings | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [calls, setCalls] = useState<CallRecord[]>([])
  const [models, setModels] = useState<Record<string, ModelInfo[]>>({})
  const [modelReadFailures, setModelReadFailures] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [savingSettings, setSavingSettings] = useState(false)
  const [providerModal, setProviderModal] = useState<Provider | 'new' | null>(null)
  const confirmedSettingsRef = useRef<Settings | null>(null)
  const visibleSettingsRef = useRef<Settings | null>(null)
  const settingsQueueRef = useRef<SettingsUpdateJob[]>([])
  const settingsWriteActiveRef = useRef(false)
  const pendingSettingsWritesRef = useRef(0)
  const settingsRevisionRef = useRef(0)
  const settingsReadSequenceRef = useRef(0)
  const mountedRef = useRef(true)

  const publishSettings = useCallback((next: Settings | null) => {
    visibleSettingsRef.current = next
    if (mountedRef.current) setSettings(next)
  }, [])

  const publishProjectedSettings = useCallback(() => {
    const confirmed = confirmedSettingsRef.current
    const projected = confirmed && settingsQueueRef.current.reduce((current, job) => job.mutation(current), confirmed)
    publishSettings(projected ?? confirmed)
  }, [publishSettings])

  const drainSettingsQueue = useCallback(async () => {
    if (settingsWriteActiveRef.current) return
    settingsWriteActiveRef.current = true
    while (settingsQueueRef.current.length > 0) {
      const job = settingsQueueRef.current.shift()!
      const confirmed = confirmedSettingsRef.current
      if (!confirmed) {
        job.resolve(false)
        pendingSettingsWritesRef.current -= 1
        continue
      }
      try {
        // Construct each full PUT from the most recently confirmed server state.
        const next = job.mutation(confirmed)
        if (next === confirmed) {
          publishProjectedSettings()
          job.resolve(false)
          pendingSettingsWritesRef.current -= 1
          if (mountedRef.current) setSavingSettings(pendingSettingsWritesRef.current > 0)
          continue
        }
        const saved = await api.updateSettings(next)
        settingsRevisionRef.current += 1
        confirmedSettingsRef.current = saved
        publishProjectedSettings()
        onNotice({ kind: 'success', text: '运行设置已保存。' })
        job.resolve(true)
      } catch (reason) {
        const writeError = errorMessage(reason)
        if (mountedRef.current) {
          setError(`运行设置保存失败：${writeError}`)
          onNotice({ kind: 'error', text: `运行设置保存失败：${writeError}` })
        }

        // A failed full PUT may have reached the server. Refresh the base before
        // applying any queued mutations so later writes never spread stale routes.
        try {
          const authoritative = await api.getSettings()
          settingsRevisionRef.current += 1
          confirmedSettingsRef.current = authoritative
          publishProjectedSettings()
          job.resolve(false)
        } catch (readReason) {
          const readError = errorMessage(readReason)
          if (mountedRef.current) {
            setError(`运行设置保存失败：${writeError}；重新读取权威设置也失败：${readError}。已取消后续排队的设置更改。`)
            onNotice({ kind: 'error', text: `无法重新读取运行设置，已取消后续排队的设置更改。${readError}` })
          }
          job.resolve(false)
          while (settingsQueueRef.current.length > 0) {
            settingsQueueRef.current.shift()!.resolve(false)
            pendingSettingsWritesRef.current -= 1
          }
          publishSettings(confirmedSettingsRef.current)
        }
      }
      pendingSettingsWritesRef.current -= 1
      if (mountedRef.current) setSavingSettings(pendingSettingsWritesRef.current > 0)
    }
    settingsWriteActiveRef.current = false
  }, [onNotice, publishProjectedSettings, publishSettings])

  const loadData = useCallback(async () => {
    const settingsRevision = settingsRevisionRef.current
    const settingsReadSequence = ++settingsReadSequenceRef.current
    const results = await Promise.allSettled([api.getSettings(), api.listProviders(), api.listCalls()])
    const [settingsResult, providersResult, callsResult] = results
    if (settingsResult.status === 'fulfilled'
      && settingsRevision === settingsRevisionRef.current
      && settingsReadSequence === settingsReadSequenceRef.current
      && pendingSettingsWritesRef.current === 0) {
      confirmedSettingsRef.current = settingsResult.value
      publishSettings(settingsResult.value)
    }
    if (providersResult.status === 'fulfilled') {
      setProviders(providersResult.value.items)
      const found = await Promise.allSettled(providersResult.value.items.map((provider) => api.listModels(provider.provider_id)))
      const modelMap: Record<string, ModelInfo[]> = {}
      const modelFailures: string[] = []
      found.forEach((result, index) => { const id = providersResult.value.items[index].provider_id; if (result.status === 'fulfilled') modelMap[id] = result.value.items; else modelFailures.push(id) })
      setModels(modelMap)
      setModelReadFailures(modelFailures)
    }
    if (callsResult.status === 'fulfilled') setCalls(callsResult.value.items)
    const rejected = results.find((result) => result.status === 'rejected')
    if (mountedRef.current && rejected?.status === 'rejected') setError(errorMessage(rejected.reason))
    if (mountedRef.current) setLoading(false)
  }, [publishSettings])
  const reload = useCallback(async () => { setLoading(true); setError(null); await loadData() }, [loadData])
  useEffect(() => {
    mountedRef.current = true
    const timer = window.setTimeout(() => void loadData(), 0)
    return () => { mountedRef.current = false; window.clearTimeout(timer) }
  }, [loadData])

  const updateSetting = useCallback((mutation: SettingsMutation): Promise<boolean> => {
    const visible = visibleSettingsRef.current
    if (!visible) return Promise.resolve(false)
    settingsRevisionRef.current += 1
    pendingSettingsWritesRef.current += 1
    publishSettings(mutation(visible))
    setSavingSettings(true)
    const saved = new Promise<boolean>((resolve) => settingsQueueRef.current.push({ mutation, resolve }))
    void drainSettingsQueue()
    return saved
  }, [drainSettingsQueue, publishSettings])

  const saveProvider = async (draft: ProviderDraft, provider?: Provider) => {
    const saved = provider ? await api.updateProvider(provider.provider_id, draft) : await api.createProvider(draft)
    onNotice({ kind: 'success', text: provider ? '供应商配置已保存。' : '供应商已添加。' }); await reload(); return saved
  }
  const probeProvider = async (provider: Provider) => {
    try {
      const result = await api.probeProvider(provider.provider_id)
      onNotice({ kind: 'info', text: result.detail ?? `目录发现状态：${result.status ?? '服务端未提供状态标签'}` })
      await reload()
    } catch (reason) { onNotice({ kind: 'error', text: errorMessage(reason) }) }
  }
  const exportCsv = () => {
    const header = ['request_id', 'started_at', 'role', 'provider', 'model', 'duration_ms', 'queue_seconds', 'first_token_seconds', 'validation_seconds', 'usage_source', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'max_output_tokens', 'reasoning_tokens', 'total_tokens', 'cost_state', 'cost_amount', 'currency', 'cost_components', 'transport_failure_attempt_number', 'transport_failure_attempt_context', 'transport_failure_phase', 'transport_failure_retryable', 'transport_failure_possibly_billed', 'transport_attempt_summary', 'error_code', 'status']
    const rows = calls.map((call) => {
      const [failureAttemptNumber, failureAttemptContext, failurePhase, retryable, possiblyBilled, attemptSummary] = transportFailureCsvFields(call)
      return [call.request_id, call.started_at ?? '', call.role, call.provider ?? '', call.model ?? '', call.duration_ms ?? 'unknown', traceNumber(call.trace, 'queue_seconds') ?? 'unknown', traceNumber(call.trace, 'first_token_seconds') ?? 'unknown', traceNumber(call.trace, 'validation_seconds') ?? 'unknown', call.usage?.source ?? 'unknown', call.usage?.input_tokens ?? 'unknown', call.usage?.cached_input_tokens ?? 'unknown', call.usage?.output_tokens ?? 'unknown', traceNumber(call.trace, 'max_output_tokens') ?? 'unknown', call.usage?.reasoning_tokens ?? 'unknown', call.usage?.total_tokens ?? 'unknown', call.cost?.state ?? 'unknown', call.cost?.amount ?? 'unknown', call.cost?.currency ?? 'unknown', call.cost?.components ? JSON.stringify(call.cost.components) : 'unknown', failureAttemptNumber, failureAttemptContext, failurePhase, retryable, possiblyBilled, attemptSummary, call.trace?.error_code ?? '', call.status ?? 'unknown']
    })
    const csv = [header, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n')
    const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob(['\ufeff', csv], { type: 'text/csv;charset=utf-8' })); link.download = 'world-engine-model-calls.csv'; link.click(); URL.revokeObjectURL(link.href)
    onNotice({ kind: 'success', text: `已导出 ${calls.length} 条当前可见记录。` })
  }

  const tabs: { value: OpsTab; label: string; icon: IconName }[] = [{ value: 'overview', label: '运行概览', icon: 'pulse' }, { value: 'providers', label: '模型连接', icon: 'plug' }, { value: 'routes', label: '职责路由', icon: 'route' }, { value: 'calls', label: '调用记录', icon: 'list' }]

  return <section className="operations-page page-enter">
    <div className="operations-heading"><div><div className="eyebrow"><span className="eyebrow-line" />系统运行 · 可观察与可恢复</div><h1>模型与运行</h1><p>模型连接、职责路由、调用记录与运行精度，保持可核查。</p></div><button className="button button-secondary" onClick={() => void reload()} disabled={loading}><Icon name="refresh" size={15} />刷新状态</button></div>
    <div className="ops-tabs" role="tablist" aria-label="运行后台页面">{tabs.map((item) => <button key={item.value} role="tab" aria-selected={tab === item.value} className={tab === item.value ? 'active' : ''} onClick={() => setTab(item.value)}><Icon name={item.icon} size={16} />{item.label}</button>)}</div>
    {error && <div className="inline-error"><Icon name="alert" size={16} /><span>{error}</span><button onClick={() => void reload()}>重试</button></div>}
    {tab === 'overview' && <div className="ops-overview">
      <div className="ops-summary-grid"><SummaryCard label="已配置模型连接" icon="plug" value={loading ? '—' : providers.length} note={`${providers.filter((provider) => provider.enabled).length} 个启用`} action="查看连接" onClick={() => setTab('providers')} /><SummaryCard label="可用模型" icon="cpu" value={loading ? '—' : Object.values(models).reduce((sum, list) => sum + list.length, 0)} note="以服务发现结果为准" action="查看模型" onClick={() => setTab('providers')} /><SummaryCard label="调用记录" icon="activity" value={loading ? '—' : calls.length} note="服务端返回的最近记录" action="查看记录" onClick={() => setTab('calls')} /></div>
      <section className="profile-settings panel-card"><div className="panel-card-heading"><div><span className="panel-overline">EXECUTION PROFILE</span><h2>速度与深度</h2></div><span className={`save-indicator ${savingSettings ? 'saving' : ''}`}><i />{savingSettings ? '保存中…' : settings ? '自动保存' : '等待设置'}</span></div><p className="panel-description">两项设置分别影响调度节奏和模拟精度。无论选择哪一档，世界硬规则始终生效。</p>{settings ? <div className="profile-controls"><ProfileControl title="速度" subtitle="请求调度、并发与呈现策略" value={settings.speed} options={[['fast', '快速', '更快地调度独立请求'], ['balanced', '均衡', '保持交互与后台平衡'], ['patient', '耐心', '减少资源竞争，完整校验后呈现']]} onChange={(value) => void updateSetting((current) => ({ ...current, speed: value as Settings['speed'] }))} /><ProfileControl title="深度" subtitle="推演范围、记忆和复核层级" value={settings.depth} options={[['standard', '标准', '精简候选与回顾范围'], ['deep', '深入', '扩展因果与人物考虑'], ['exhaustive', '充分', '更完整的候选与冲突审视']]} onChange={(value) => void updateSetting((current) => ({ ...current, depth: value as Settings['depth'] }))} /></div> : <div className="settings-empty">{loading ? '正在读取运行设置…' : '服务端尚未返回运行设置。'}</div>}<div className="profile-separation"><span className="separation-icon"><Icon name="crosshair" size={15} /></span><span><strong>互相独立的两个维度</strong><small>快速 + 充分仍会执行充分推演；耐心 + 标准不会自动增加模拟深度。</small></span></div></section>
      <div className="ops-overview-bottom"><div className="mini-panel"><span className="panel-overline">LOCAL API</span><h3>连接检查</h3><p>设置状态来自服务端的运行配置响应。</p><button className="text-button" onClick={() => void reload()}>重新检测 <Icon name="arrow-right" size={14} /></button></div><div className="mini-panel"><span className="panel-overline">PRIVACY</span><h3>凭据只留在本机</h3><p>密钥只写入 Windows 凭据管理器；界面只接收状态和引用，不回读密钥。</p><span className="privacy-badge"><Icon name="shield" size={13} />本地凭据边界</span></div></div>
    </div>}
    {tab === 'providers' && <div className="providers-page"><div className="subpage-header"><div><h2>模型连接</h2><p>目录发现只返回可见模型；生成能力需经过独立验证。</p></div><button className="button button-primary" onClick={() => setProviderModal('new')}><Icon name="plus" size={16} />添加供应商</button></div>{loading && providers.length === 0 ? <div className="provider-skeleton" /> : providers.length ? <div className="provider-list">{providers.map((provider) => <ProviderCard key={provider.provider_id} provider={provider} models={models[provider.provider_id] ?? []} modelsReadFailed={modelReadFailures.includes(provider.provider_id)} onEdit={() => setProviderModal(provider)} onProbe={() => void probeProvider(provider)} />)}</div> : <InlineState icon="plug" title="还没有模型连接" description="添加一个供应商端点后，可以发现模型目录。密钥由后端凭据库管理。" action={<button className="button button-primary" onClick={() => setProviderModal('new')}><Icon name="plus" size={15} />添加供应商</button>} />}</div>}
    {tab === 'routes' && <RoutesPage settings={settings} providers={providers} models={models} loading={loading} saving={savingSettings} onChange={updateSetting} />}
    {tab === 'calls' && <div className="calls-page"><div className="subpage-header"><div><h2>调用记录</h2><p>按任务角色核对总耗时、供应商报告的输入 / 输出 Token 与费用。未知值不会显示为零；完整安全追踪可展开查看。</p></div><button className="button button-secondary button-small" onClick={exportCsv} disabled={!calls.length}><Icon name="download" size={14} />导出 CSV</button></div>{calls.length ? <div className="calls-table-wrap"><table className="calls-table"><thead><tr><th>时间 / 调用</th><th>职责</th><th>供应商 / 模型</th><th>总耗时 / 阶段</th><th>Token 用量</th><th>费用</th><th>结果</th><th>追踪</th></tr></thead><tbody>{calls.map((call) => <tr key={call.request_id}><td><strong>{formatTime(call.started_at)}</strong><small>{call.request_id.slice(0, 12)}</small></td><td><strong>{roleLabel(call.role)}</strong><small>{call.role}</small></td><td><strong>{call.provider ?? '未知'}</strong><small>{call.model ?? '模型未知'}</small></td><td className="call-metrics-cell"><strong>{formatMilliseconds(call.duration_ms)}</strong><small>队列 {formatSeconds(traceNumber(call.trace, 'queue_seconds'))}</small><small>首 Token {formatSeconds(traceNumber(call.trace, 'first_token_seconds'))}</small></td><td className="call-metrics-cell"><strong>输入 {formatTokenCount(call.usage?.input_tokens)} · 输出 {formatTokenCount(call.usage?.output_tokens)}</strong><small>合计 {formatTokenCount(call.usage?.total_tokens)} tokens · {usageSourceLabel(call.usage?.source)}</small>{traceNumber(call.trace, 'max_output_tokens') != null && <small>本次输出上限 {formatTokenCount(traceNumber(call.trace, 'max_output_tokens'))} tokens</small>}{call.usage?.cached_input_tokens != null && <small>缓存输入 {formatTokenCount(call.usage.cached_input_tokens)} tokens</small>}{call.usage?.reasoning_tokens != null && <small>推理 {formatTokenCount(call.usage.reasoning_tokens)} tokens</small>}</td><td><CostCell cost={call.cost} /></td><td><span className={`call-status call-status-${callStatusTone(call.status)}`}>{callStatusLabel(call.status, call.trace?.error_code)}</span>{call.trace?.error_code === 'context_limit' && <small style={{ whiteSpace: 'normal', overflow: 'visible' }}>请在「模型与运行 → 模型连接」提高 Ollama 上下文容量；较大值会占用更多内存。</small>}<TransportFailure call={call} compact /></td><td><details className="trace-details"><summary>查看</summary><TransportFailure call={call} /><pre>{JSON.stringify(call.trace ?? { request_id: call.request_id, role: call.role, provider: call.provider, model: call.model, status: call.status }, null, 2)}</pre></details></td></tr>)}</tbody></table></div> : <InlineState icon="activity" title="还没有调用记录" description="请求实际通过模型后台后，服务端会返回可核对的运行信息。" />}</div>}
    {providerModal && <ProviderDialog key={providerModal === 'new' ? 'new' : providerModal.provider_id} provider={providerModal === 'new' ? undefined : providerModal} onClose={() => setProviderModal(null)} onSave={saveProvider} />}
  </section>
}

function SummaryCard({ label, icon, value, note, action, onClick }: { label: string; icon: IconName; value: number | string; note: string; action: string; onClick: () => void }) { return <div className="ops-summary-card"><div className="summary-card-label">{label}<Icon name={icon} size={16} /></div><strong>{value}</strong><span>{note}</span><div className="summary-card-bottom"><button className="text-button" onClick={onClick}>{action} <Icon name="arrow-right" size={14} /></button></div></div> }

function ProfileControl({ title, subtitle, value, options, onChange }: { title: string; subtitle: string; value: string; options: [string, string, string][]; onChange: (value: string) => void }) { return <div className="profile-control"><div className="profile-control-title"><div><strong>{title}</strong><small>{subtitle}</small></div><span>{options.find((option) => option[0] === value)?.[1] ?? value}</span></div><div className="segmented-options" role="group" aria-label={title}>{options.map(([option, label, description]) => <button key={option} className={value === option ? 'selected' : ''} onClick={() => onChange(option)} aria-pressed={value === option} title={description}><span>{label}</span><i /></button>)}</div><div className="profile-control-foot">{options.find((option) => option[0] === value)?.[2]}</div></div> }

function RoutesPage({ settings, providers, models, loading, saving, onChange }: { settings: Settings | null; providers: Provider[]; models: Record<string, ModelInfo[]>; loading: boolean; saving: boolean; onChange: (mutation: SettingsMutation) => Promise<boolean> }) {
  const [manualModels, setManualModels] = useState<Record<string, string>>({})
  const [outputTokenDrafts, setOutputTokenDrafts] = useState<Record<string, string>>({})
  const [routeErrors, setRouteErrors] = useState<Record<string, string>>({})
  const manualDraftRevisionRef = useRef<Record<string, number>>({})
  const manualDraftProviderRef = useRef<Record<string, { providerId?: string }>>({})
  const outputDraftRevisionRef = useRef<Record<string, number>>({})
  if (!settings) return <div className="routes-page"><div className="subpage-header"><div><h2>职责路由</h2><p>每类任务独立选择首选模型；本地限定策略不会自动回退到云端。</p></div></div><div className="settings-empty">{loading ? '正在读取职责路由…' : '服务端尚未返回路由设置。'}</div></div>
  const routeEntries: Array<[string, ModelRoute]> = Object.entries(settings.routes)
  if (!Object.prototype.hasOwnProperty.call(settings.routes, 'npc_plan_review')) {
    routeEntries.push(['npc_plan_review', {}])
  }
  return <div className="routes-page"><div className="subpage-header"><div><h2>职责路由</h2><p>每类任务独立选择首选模型；本地限定策略不会自动回退到云端。</p></div></div><div className="local-only-row"><span className="local-only-copy"><span className="route-local-icon"><Icon name="shield" size={16} /></span><span><strong>仅使用本地服务</strong><small>阻止选择的内容发送到云端供应商</small></span></span><button type="button" className={`toggle-switch ${settings.local_only ? 'on' : ''}`} aria-pressed={settings.local_only} onClick={() => { const localOnly = !settings.local_only; void onChange((current) => ({ ...current, local_only: localOnly })) }}><span /></button></div><div className="routes-list">{routeEntries.map(([role, route]) => {
    const isInheritedReviewer = role === 'npc_plan_review' && !Object.prototype.hasOwnProperty.call(settings.routes, role)
    const supportsOutputTokenLimit = role !== 'embedding' && (role !== 'npc_plan_review' || Boolean(route.provider_id))
    const routeModels = models[route.provider_id ?? ''] ?? []
    const routeProvider = providers.find((provider) => provider.provider_id === route.provider_id)
    const allowsManualModel = routeProvider?.kind === 'openai_compatible'
    const modelValue = manualModels[role] ?? route.model ?? ''
    const outputTokenValue = outputTokenDrafts[role] ?? (route.max_output_tokens != null ? String(route.max_output_tokens) : '')
    const outputTokenError = maxOutputTokensError(outputTokenValue)
    const hasOutputTokenDraft = Object.prototype.hasOwnProperty.call(outputTokenDrafts, role)
    const outputTokenDirty = hasOutputTokenDraft && outputTokenValue !== String(route.max_output_tokens ?? '')
    const savePrimaryPatch = async (patch: Partial<ModelRoute>, expectedProviderId = route.provider_id) => {
      const candidate = { ...route, ...patch }
      const issue = primaryRouteIssue(candidate, providers)
      if (issue) {
        setRouteErrors((current) => ({ ...current, [role]: issue }))
        return false
      }
      setRouteErrors((current) => { const next = { ...current }; delete next[role]; return next })
      return onChange((current) => {
        const currentRoute = current.routes[role] ?? (role === 'npc_plan_review' ? {} : undefined)
        if (!currentRoute || currentRoute.provider_id !== expectedProviderId) return current
        const updated = { ...currentRoute, ...patch }
        if (primaryRouteIssue(updated, providers)) return current
        if (role === 'npc_plan_review' && !updated.provider_id) {
          const routes = { ...current.routes }
          delete routes[role]
          return { ...current, routes }
        }
        return { ...current, routes: { ...current.routes, [role]: updated } }
      })
    }
    const saveManualModel = async () => {
      const model = modelValue.trim()
      const draftRevision = manualDraftRevisionRef.current[role] ?? 0
      const submittedValue = modelValue
      const draftProvider = manualDraftProviderRef.current[role]
      const intendedProviderId = draftProvider ? draftProvider.providerId : route.provider_id
      if (model === (route.model ?? '')) {
        setManualModels((current) => {
          if ((manualDraftRevisionRef.current[role] ?? 0) !== draftRevision || current[role] !== submittedValue) return current
          const next = { ...current }; delete next[role]; return next
        })
        delete manualDraftProviderRef.current[role]
        return
      }
      const saved = await savePrimaryPatch({ model: model || undefined }, intendedProviderId)
      if (saved && (manualDraftRevisionRef.current[role] ?? 0) === draftRevision) {
        setManualModels((current) => {
          if (current[role] !== submittedValue) return current
          const next = { ...current }; delete next[role]; return next
        })
        delete manualDraftProviderRef.current[role]
      }
    }
    const saveOutputTokenLimit = async () => {
      if (!supportsOutputTokenLimit || outputTokenError) return
      const submittedValue = outputTokenValue
      const draftRevision = outputDraftRevisionRef.current[role] ?? 0
      const maxOutputTokens = outputTokenValue === '' ? undefined : Number(outputTokenValue)
      if (maxOutputTokens === route.max_output_tokens) {
        setOutputTokenDrafts((current) => {
          if ((outputDraftRevisionRef.current[role] ?? 0) !== draftRevision) return current
          const next = { ...current }; delete next[role]; return next
        })
        return
      }
      const saved = await onChange((current) => {
        const currentRoute = current.routes[role]
        if (!currentRoute) return current
        return { ...current, routes: { ...current.routes, [role]: { ...currentRoute, max_output_tokens: maxOutputTokens } } }
      })
      if (saved) setOutputTokenDrafts((current) => {
        if ((outputDraftRevisionRef.current[role] ?? 0) !== draftRevision || current[role] !== submittedValue) return current
        const next = { ...current }; delete next[role]; return next
      })
    }
    const outputTokenHelpId = `route-output-token-help-${role.replace(/[^a-zA-Z0-9_-]/g, '-')}`
    const canClearPrimary = role === 'npc_plan_review'
      ? (route.fallbacks?.length ?? 0) === 0
      : !primaryRouteIssue({ ...route, provider_id: undefined, model: undefined }, providers)
    const roleId = role.replace(/[^a-zA-Z0-9_-]/g, '-')
    return <div className="route-row" key={role}>
      <div className="route-main-row">
        <div className="route-role"><span className="route-role-icon"><Icon name={roleIcon(role)} size={15} /></span><span><strong>{roleLabel(role)}</strong><small>{role}</small>{isInheritedReviewer && <small>{settings.routes.npc_decision?.provider_id ? '当前暂用人物决策路由；配置后可独立选择复审模型。' : '尚未配置人物决策模型；选择供应商以启用独立复审。'}</small>}{role === 'npc_plan_review' && !isInheritedReviewer && !route.provider_id && <small>此复审路由尚未配置供应商，不能用于运行。</small>}</span></div>
        <div className="route-selects">
          <select aria-label={`${roleLabel(role)}首选供应商`} value={route.provider_id ?? ''} onChange={(event) => {
            const providerId = event.target.value || undefined
            manualDraftRevisionRef.current[role] = (manualDraftRevisionRef.current[role] ?? 0) + 1
            delete manualDraftProviderRef.current[role]
            setManualModels((current) => { const next = { ...current }; delete next[role]; return next })
            if (role === 'npc_plan_review' && !providerId) {
              setOutputTokenDrafts((current) => { const next = { ...current }; delete next[role]; return next })
            }
            void savePrimaryPatch({ provider_id: providerId, model: undefined }, route.provider_id)
          }}>
            <option value="" disabled={!canClearPrimary}>使用系统默认</option>
            {providers.filter((provider) => provider.enabled).map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.name}</option>)}
          </select>
          <select aria-label={`${roleLabel(role)}首选模型`} value={route.model ?? ''} onChange={(event) => {
            const model = event.target.value || undefined
            manualDraftRevisionRef.current[role] = (manualDraftRevisionRef.current[role] ?? 0) + 1
            delete manualDraftProviderRef.current[role]
            setManualModels((current) => { const next = { ...current }; delete next[role]; return next })
            void savePrimaryPatch({ model }, route.provider_id)
          }}>
            <option value="">自动发现</option>
            {route.model && !routeModels.some((model) => model.id === route.model) && <option value={route.model}>已配置 · {route.model}</option>}
            {routeModels.map((model) => <option key={model.id} value={model.id}>{model.name ?? model.id}</option>)}
          </select>
          {allowsManualModel && <input className="route-model-manual" type="text" aria-label={`${roleLabel(role)}首选模型手动 ID`} title="兼容协议可使用服务提供商指定的模型 ID" placeholder="手动模型 ID" value={modelValue} onChange={(event) => {
            manualDraftRevisionRef.current[role] = (manualDraftRevisionRef.current[role] ?? 0) + 1
            manualDraftProviderRef.current[role] = { providerId: route.provider_id }
            setManualModels((current) => ({ ...current, [role]: event.target.value }))
          }} onBlur={() => void saveManualModel()} onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || event.keyCode === 229) return
            if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur() }
          }} />}
        </div>
        <span className="route-save">{saving ? '保存中' : '已保存'}</span>
      </div>
      {routeErrors[role] && <small className="route-config-error" role="alert">{routeErrors[role]}</small>}
      {supportsOutputTokenLimit && <div className="route-output-token-control">
        <label htmlFor={`route-output-token-${roleId}`}>最大输出 Token<small>当前已保存：{route.max_output_tokens != null ? `${formatTokenCount(route.max_output_tokens)} tokens` : '引擎默认'}；留空使用引擎默认</small></label>
        <input id={`route-output-token-${roleId}`} type="number" min={1} max={2147483647} step={1} inputMode="numeric" aria-label={`${roleLabel(role)}最大输出 Token`} aria-invalid={Boolean(outputTokenError)} aria-describedby={outputTokenHelpId} value={outputTokenValue} placeholder="引擎默认" onChange={(event) => { outputDraftRevisionRef.current[role] = (outputDraftRevisionRef.current[role] ?? 0) + 1; setOutputTokenDrafts((current) => ({ ...current, [role]: event.target.value })) }} onKeyDown={(event) => { if (event.nativeEvent.isComposing || event.keyCode === 229) return; if (event.key === 'Enter') { event.preventDefault(); void saveOutputTokenLimit() } }} />
        <span className={`route-output-token-status ${outputTokenDirty ? 'is-dirty' : ''}`} aria-live="polite">{saving ? '保存中…' : outputTokenDirty ? '待保存' : route.max_output_tokens != null ? '已保存' : '引擎默认'}</span>
        <button className="button button-secondary button-small route-output-token-save" type="button" aria-label={`${roleLabel(role)}输出上限保存`} disabled={saving || !outputTokenDirty || Boolean(outputTokenError)} onClick={() => void saveOutputTokenLimit()}>{saving ? '保存中…' : '保存'}</button>
        <small className={`route-output-token-help ${outputTokenError ? 'has-error' : ''}`} id={outputTokenHelpId} role={outputTokenError ? 'alert' : undefined}>{outputTokenError ?? '请输入 1 到 2,147,483,647 的整数；清空后保存即可恢复引擎默认。'}</small>
      </div>}
      <FallbacksEditor role={role} route={route} providers={providers} models={models} saving={saving} onChange={onChange} />
    </div>
  })}</div>{routeEntries.length === 0 && <InlineState icon="route" title="暂无职责路由" description="当前设置没有返回可配置的职责。" />}</div>
}

function maxOutputTokensError(value: string): string | null {
  if (value === '') return null
  const parsed = Number(value)
  if (!Number.isInteger(parsed)) return '请输入整数，不接受小数。'
  if (parsed < 1 || parsed > 2147483647) return '取值范围为 1 到 2,147,483,647。'
  return null
}

function FallbacksEditor({ role, route, providers, models, saving, onChange }: {
  role: string
  route: ModelRoute
  providers: Provider[]
  models: Record<string, ModelInfo[]>
  saving: boolean
  onChange: (mutation: SettingsMutation) => Promise<boolean>
}) {
  const [manualModels, setManualModels] = useState<Record<number, string>>({})
  const [outputTokenDrafts, setOutputTokenDrafts] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  const manualRevisionRef = useRef<Record<number, number>>({})
  const outputRevisionRef = useRef<Record<number, number>>({})
  const fallbacks = route.fallbacks ?? []
  const primaryConfigured = hasConfiguredPrimary(route) && (role !== 'npc_plan_review' || Boolean(route.provider_id))
  const addCandidate = createFallbackCandidate(route, providers, models)

  const updateFallback = async (index: number, patch: Partial<ModelRouteTarget>) => {
    const existing = route.fallbacks?.[index]
    if (!existing || !primaryConfigured) {
      setError('备用模型需要先配置首选模型。')
      return false
    }
    const candidate = { ...existing, ...patch }
    if (hasFallbackTargetConflict(candidate, route, providers, index)) {
      setError('该供应商和模型已在首选或备用列表中。')
      return false
    }
    setError(null)
    return onChange((current) => {
      const currentRoute = current.routes[role]
      const currentFallbacks = currentRoute?.fallbacks ?? []
      if (!currentRoute || !hasConfiguredPrimary(currentRoute) || !currentFallbacks[index]) return current
      const nextCandidate = { ...currentFallbacks[index], ...patch }
      if (hasFallbackTargetConflict(nextCandidate, currentRoute, providers, index)) return current
      const nextFallbacks = [...currentFallbacks]
      nextFallbacks[index] = nextCandidate
      return withFallbacks(current, role, currentRoute, nextFallbacks)
    })
  }

  const addFallback = () => {
    if (!primaryConfigured || fallbacks.length >= 3 || !addCandidate) return
    setError(null)
    void onChange((current) => {
      const currentRoute = current.routes[role]
      const currentFallbacks = currentRoute?.fallbacks ?? []
      if (!currentRoute || !hasConfiguredPrimary(currentRoute) || currentFallbacks.length >= 3) return current
      const target = createFallbackCandidate(currentRoute, providers, models)
      if (!target) return current
      return withFallbacks(current, role, currentRoute, [...currentFallbacks, target])
    })
  }

  const removeFallback = (index: number) => {
    setError(null)
    setManualModels((current) => { const next = { ...current }; delete next[index]; return next })
    setOutputTokenDrafts((current) => { const next = { ...current }; delete next[index]; return next })
    void onChange((current) => {
      const currentRoute = current.routes[role]
      const currentFallbacks = currentRoute?.fallbacks ?? []
      if (!currentRoute || !currentFallbacks[index]) return current
      return withFallbacks(current, role, currentRoute, currentFallbacks.filter((_, currentIndex) => currentIndex !== index))
    })
  }

  const moveFallback = (index: number, direction: -1 | 1) => {
    if (!primaryConfigured) return
    setError(null)
    setManualModels({})
    setOutputTokenDrafts({})
    void onChange((current) => {
      const currentRoute = current.routes[role]
      const currentFallbacks = [...(currentRoute?.fallbacks ?? [])]
      const destination = index + direction
      if (!currentRoute || !hasConfiguredPrimary(currentRoute) || destination < 0 || destination >= currentFallbacks.length) return current
      const [moved] = currentFallbacks.splice(index, 1)
      currentFallbacks.splice(destination, 0, moved)
      return withFallbacks(current, role, currentRoute, currentFallbacks)
    })
  }

  const saveManualModel = async (index: number, target: ModelRouteTarget, value: string) => {
    const submitted = value
    const revision = manualRevisionRef.current[index] ?? 0
    const model = value.trim() || undefined
    if (model === target.model) {
      setManualModels((current) => current[index] === submitted
        ? Object.fromEntries(Object.entries(current).filter(([key]) => Number(key) !== index))
        : current)
      return true
    }
    const saved = await updateFallback(index, { model })
    if (saved && (manualRevisionRef.current[index] ?? 0) === revision) {
      setManualModels((current) => current[index] === submitted
        ? Object.fromEntries(Object.entries(current).filter(([key]) => Number(key) !== index))
        : current)
    }
    return saved
  }

  const saveOutputTokenLimit = async (index: number, target: ModelRouteTarget, value: string) => {
    const validation = maxOutputTokensError(value)
    if (role === 'embedding' || validation) return false
    const submitted = value
    const revision = outputRevisionRef.current[index] ?? 0
    const maxOutputTokens = value === '' ? undefined : Number(value)
    if (maxOutputTokens === target.max_output_tokens) {
      setOutputTokenDrafts((current) => { const next = { ...current }; delete next[index]; return next })
      return true
    }
    const saved = await updateFallback(index, { max_output_tokens: maxOutputTokens })
    if (saved && (outputRevisionRef.current[index] ?? 0) === revision) {
      setOutputTokenDrafts((current) => current[index] === submitted
        ? Object.fromEntries(Object.entries(current).filter(([key]) => Number(key) !== index))
        : current)
    }
    return saved
  }

  return <section className="route-fallbacks" aria-labelledby={`route-fallbacks-title-${role.replace(/[^a-zA-Z0-9_-]/g, '-')}`}>
    <div className="route-fallbacks-heading">
      <div><strong id={`route-fallbacks-title-${role.replace(/[^a-zA-Z0-9_-]/g, '-')}`}>备用模型</strong><small>首选不可用时按顺序尝试，最多 3 个。</small></div>
      <button type="button" className="button button-secondary button-small" onClick={addFallback} disabled={!primaryConfigured || fallbacks.length >= 3 || !addCandidate} title={!primaryConfigured ? '请先配置首选模型' : !addCandidate ? '没有可添加的不同供应商或模型' : undefined}>添加备用模型</button>
    </div>
    {fallbacks.length === 0
      ? <p className="route-fallback-empty">{primaryConfigured ? '尚未设置备用模型。' : '先配置首选模型，再添加备用项。'}</p>
      : <div className="route-fallback-list">{fallbacks.map((target, index) => {
        const provider = providers.find((item) => item.provider_id === target.provider_id)
        const providerModels = models[target.provider_id] ?? []
        const allowsManualModel = provider?.kind === 'openai_compatible'
        const modelValue = manualModels[index] ?? target.model ?? ''
        const tokenValue = outputTokenDrafts[index] ?? (target.max_output_tokens != null ? String(target.max_output_tokens) : '')
        const tokenError = maxOutputTokensError(tokenValue)
        const tokenDirty = Object.prototype.hasOwnProperty.call(outputTokenDrafts, index)
          && tokenValue !== String(target.max_output_tokens ?? '')
        const tokenHelpId = `route-fallback-token-help-${role.replace(/[^a-zA-Z0-9_-]/g, '-')}-${index}`
        const controlDisabled = saving || !primaryConfigured
        return <fieldset className="route-fallback-item" key={`${role}:${index}:${target.provider_id}:${target.model ?? ''}`}>
          <legend>备用模型 {index + 1}</legend>
          <div className="route-fallback-controls">
            <select aria-label={`${roleLabel(role)}备用模型 ${index + 1} 供应商`} value={target.provider_id} disabled={controlDisabled} onChange={(event) => {
              const providerId = event.target.value
              manualRevisionRef.current[index] = (manualRevisionRef.current[index] ?? 0) + 1
              setManualModels((current) => { const next = { ...current }; delete next[index]; return next })
              void updateFallback(index, { provider_id: providerId, model: undefined })
            }}>
              {provider && !provider.enabled && <option value={provider.provider_id} disabled>{provider.name} · 未启用</option>}
              {!provider && <option value={target.provider_id} disabled>{target.provider_id} · 不可用</option>}
              {providers.filter((item) => item.enabled).map((item) => <option key={item.provider_id} value={item.provider_id}>{item.name}</option>)}
            </select>
            <select aria-label={`${roleLabel(role)}备用模型 ${index + 1} 模型`} value={target.model ?? ''} disabled={controlDisabled} onChange={(event) => {
              manualRevisionRef.current[index] = (manualRevisionRef.current[index] ?? 0) + 1
              setManualModels((current) => { const next = { ...current }; delete next[index]; return next })
              void updateFallback(index, { model: event.target.value || undefined })
            }}>
              <option value="">自动发现</option>
              {target.model && !providerModels.some((item) => item.id === target.model) && <option value={target.model}>已配置 · {target.model}</option>}
              {providerModels.map((item) => <option key={item.id} value={item.id}>{item.name ?? item.id}</option>)}
            </select>
            {allowsManualModel && <input className="route-fallback-manual" type="text" aria-label={`${roleLabel(role)}备用模型 ${index + 1} 手动模型 ID`} title="兼容协议可使用服务提供商指定的模型 ID" placeholder="手动模型 ID" value={modelValue} disabled={controlDisabled} onChange={(event) => {
              manualRevisionRef.current[index] = (manualRevisionRef.current[index] ?? 0) + 1
              setManualModels((current) => ({ ...current, [index]: event.target.value }))
            }} onBlur={() => void saveManualModel(index, target, modelValue)} onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || event.keyCode === 229) return
              if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur() }
            }} />}
          </div>
          {role !== 'embedding' && <div className="route-fallback-token">
            <label htmlFor={`route-fallback-token-${role.replace(/[^a-zA-Z0-9_-]/g, '-')}-${index}`}>最大输出 Token</label>
            <input id={`route-fallback-token-${role.replace(/[^a-zA-Z0-9_-]/g, '-')}-${index}`} type="number" min={1} max={2147483647} step={1} inputMode="numeric" aria-label={`${roleLabel(role)}备用模型 ${index + 1} 最大输出 Token`} aria-invalid={Boolean(tokenError)} aria-describedby={tokenHelpId} value={tokenValue} placeholder="引擎默认" disabled={controlDisabled} onChange={(event) => {
              outputRevisionRef.current[index] = (outputRevisionRef.current[index] ?? 0) + 1
              setOutputTokenDrafts((current) => ({ ...current, [index]: event.target.value }))
            }} onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || event.keyCode === 229) return
              if (event.key === 'Enter') { event.preventDefault(); void saveOutputTokenLimit(index, target, tokenValue) }
            }} />
            <span className={`route-output-token-status ${tokenDirty ? 'is-dirty' : ''}`} aria-live="polite">{saving ? '保存中…' : tokenDirty ? '待保存' : target.max_output_tokens != null ? '已保存' : '引擎默认'}</span>
            <button className="button button-secondary button-small route-output-token-save" type="button" aria-label={`${roleLabel(role)}备用模型 ${index + 1} 输出上限保存`} disabled={controlDisabled || !tokenDirty || Boolean(tokenError)} onClick={() => void saveOutputTokenLimit(index, target, tokenValue)}>{saving ? '保存中…' : '保存'}</button>
            <small className={`route-output-token-help ${tokenError ? 'has-error' : ''}`} id={tokenHelpId} role={tokenError ? 'alert' : undefined}>{tokenError ?? '留空使用引擎默认。'}</small>
          </div>}
          <div className="route-fallback-actions">
            <button type="button" className="button button-secondary button-small" aria-label={`将第 ${index + 1} 个备用模型上移`} disabled={controlDisabled || index === 0} onClick={() => moveFallback(index, -1)}>上移</button>
            <button type="button" className="button button-secondary button-small" aria-label={`将第 ${index + 1} 个备用模型下移`} disabled={controlDisabled || index === fallbacks.length - 1} onClick={() => moveFallback(index, 1)}>下移</button>
            <button type="button" className="button button-secondary button-small" aria-label={`移除第 ${index + 1} 个备用模型`} onClick={() => removeFallback(index)}>移除</button>
          </div>
        </fieldset>
      })}</div>}
    {error && <small className="route-config-error" role="alert">{error}</small>}
  </section>
}

function hasConfiguredPrimary(route: ModelRoute): boolean {
  return Boolean(route.provider_id || route.model?.trim() || route.max_output_tokens != null)
}

function primaryRouteIssue(route: ModelRoute, providers: Provider[]): string | null {
  const fallbacks = route.fallbacks ?? []
  if (fallbacks.length > 0 && !hasConfiguredPrimary(route)) return '请先配置首选模型，或移除备用模型。'
  if (route.provider_id) {
    const primary = { provider_id: route.provider_id, model: route.model }
    if (fallbacks.some((fallback) => sameRouteTarget(primary, fallback, providers))) return '首选模型不能与备用模型重复。'
  }
  return null
}

function hasFallbackTargetConflict(target: ModelRouteTarget, route: ModelRoute, providers: Provider[], ignoredIndex: number): boolean {
  if (route.provider_id && sameRouteTarget(target, { provider_id: route.provider_id, model: route.model }, providers)) return true
  return (route.fallbacks ?? []).some((fallback, index) => index !== ignoredIndex && sameRouteTarget(target, fallback, providers))
}

function sameRouteTarget(left: ModelRouteTarget, right: ModelRouteTarget, providers: Provider[]): boolean {
  if (left.provider_id !== right.provider_id) return false
  const defaultModel = providers.find((provider) => provider.provider_id === left.provider_id)?.default_model?.trim() ?? ''
  return (left.model?.trim() || defaultModel) === (right.model?.trim() || defaultModel)
}

function createFallbackCandidate(route: ModelRoute, providers: Provider[], models: Record<string, ModelInfo[]>): ModelRouteTarget | null {
  const enabledProviders = providers.filter((provider) => provider.enabled)
  for (const provider of enabledProviders) {
    const candidates: ModelRouteTarget[] = [
      { provider_id: provider.provider_id },
      ...(models[provider.provider_id] ?? []).map((model) => ({ provider_id: provider.provider_id, model: model.id })),
    ]
    const available = candidates.find((candidate) => !hasFallbackTargetConflict(candidate, route, providers, -1))
    if (available) return available
  }
  return null
}

function withFallbacks(settings: Settings, role: string, route: ModelRoute, fallbacks: ModelRouteTarget[]): Settings {
  return {
    ...settings,
    routes: {
      ...settings.routes,
      [role]: { ...route, fallbacks: fallbacks.length ? fallbacks : undefined },
    },
  }
}

function ProviderCard({ provider, models, modelsReadFailed, onEdit, onProbe }: { provider: Provider; models: ModelInfo[]; modelsReadFailed: boolean; onEdit: () => void; onProbe: () => void }) { return <article className="provider-card"><div className="provider-card-head"><div className={`provider-avatar provider-${provider.kind}`}><Icon name={providerIcon(provider.kind)} size={18} /></div><div className="provider-name-block"><h3>{provider.name}</h3><span>{provider.kind} · {provider.base_url}</span></div><span className={`provider-enabled ${provider.enabled ? 'is-enabled' : ''}`}><i />{provider.enabled ? '已启用' : '已停用'}</span><button className="icon-button" aria-label={`编辑 ${provider.name}`} onClick={onEdit}><Icon name="edit" size={16} /></button></div><div className="provider-card-bottom"><div className="provider-model-stat"><span className="provider-meta-label">目录模型</span>{modelsReadFailed ? <strong title="模型目录请求失败">未知</strong> : <strong>{models.length}</strong>}{!modelsReadFailed && models.length > 0 && <span className="model-chip">{models.slice(0, 2).map((model) => model.name ?? model.id).join(' · ')}{models.length > 2 ? ` +${models.length - 2}` : ''}</span>}</div><div className="provider-probe-controls"><span className="discovery-note">发现目录不等同于生成验证</span><button className="button button-secondary button-small" onClick={onProbe} disabled={!provider.enabled}><Icon name="refresh" size={14} />发现模型目录</button></div></div></article> }

function CostCell({ cost }: { cost: CallRecord['cost'] }) {
  if (!cost || cost.state === 'unknown' || cost.amount == null) return <span className="cost-unknown" title="服务端没有报告确定费用">未知</span>
  const amount = `${cost.currency ?? ''} ${cost.amount}`.trim()
  return <span className={`cost-${cost.state}`} title={cost.source_url ?? (cost.state === 'estimated' ? '费用为估算值' : '服务端报告费用')}>{cost.state === 'estimated' ? `约 ${amount}` : amount}</span>
}

function traceNumber(trace: CallRecord['trace'], key: string): number | null {
  const value = trace?.[key]
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function formatMilliseconds(value: number | null | undefined): string {
  if (value == null) return '未知'
  if (!Number.isFinite(value) || value < 0) return '不可用'
  return `${value.toLocaleString('zh-HK', { maximumFractionDigits: 1 })} ms`
}

function formatSeconds(value: number | null): string {
  return value === null ? '未知' : formatMilliseconds(value * 1000)
}

function formatTokenCount(value: number | null | undefined): string {
  if (value == null) return '未知'
  if (!Number.isSafeInteger(value) || value < 0) return '精度未知'
  return value.toLocaleString('zh-HK')
}

function usageSourceLabel(source: string | undefined): string {
  if (source === 'reported') return '供应商报告'
  if (!source || source === 'unknown') return '来源未知'
  return source
}

function callStatusLabel(status: string | null, errorCode?: string | null): string {
  if (errorCode === 'context_limit') return '上下文容量不足'
  const labels: Record<string, string> = {
    completed: '成功完成', preflight_rejected: '前置检查拒绝', provider_error: '供应商调用失败',
    output_rejected: '输出校验拒绝', cancelled: '已取消', stream_failed: '流式响应失败',
  }
  if (!status) return '结果未知'
  return labels[status] ?? `其他结果 · ${status}`
}

function callStatusTone(status: string | null): 'success' | 'error' | 'quiet' | 'unknown' {
  if (status === 'completed') return 'success'
  if (status === 'cancelled') return 'quiet'
  if (!status) return 'unknown'
  return 'error'
}

type ProviderProtocolDraft = { baseUrl: string; defaultModel: string; ollamaContextTokens?: string }

const providerProtocolDefaults: Record<ProviderDraft['kind'], ProviderProtocolDraft> = {
  ollama: { baseUrl: 'http://127.0.0.1:11434', defaultModel: '', ollamaContextTokens: '' },
  deepseek: { baseUrl: 'https://api.deepseek.com', defaultModel: '' },
  openai_compatible: { baseUrl: '', defaultModel: '' },
  anthropic: { baseUrl: 'https://api.anthropic.com', defaultModel: '' },
}

function initialProviderProtocolDrafts(provider?: Provider): Record<ProviderDraft['kind'], ProviderProtocolDraft> {
  return Object.fromEntries(Object.entries(providerProtocolDefaults).map(([protocol, draft]) => {
    const currentProvider = provider?.kind === protocol ? provider : null
    return [protocol, currentProvider
      ? {
        baseUrl: currentProvider.base_url,
        defaultModel: currentProvider.default_model ?? '',
        ...(protocol === 'ollama' ? { ollamaContextTokens: String(currentProvider.ollama_context_tokens ?? '') } : {}),
      }
      : { ...draft }]
  })) as Record<ProviderDraft['kind'], ProviderProtocolDraft>
}

function ProviderDialog({ provider, onClose, onSave }: { provider?: Provider; onClose: () => void; onSave: (draft: ProviderDraft, provider?: Provider) => Promise<Provider> }) {
  const dialogRef = useRef<HTMLElement>(null)
  const busyRef = useRef(false)
  const [currentProvider, setCurrentProvider] = useState(provider)
  const [name, setName] = useState(provider?.name ?? '')
  const [kind, setKind] = useState<ProviderDraft['kind']>((provider?.kind as ProviderDraft['kind']) ?? 'ollama')
  const [protocolDrafts, setProtocolDrafts] = useState(() => initialProviderProtocolDrafts(provider))
  const [enabled, setEnabled] = useState(provider?.enabled ?? true)
  const [busyAction, setBusyAction] = useState<'provider-save' | 'credential-save' | 'credential-delete' | null>(null)
  const [error, setError] = useState('')
  const [credentialRead, setCredentialRead] = useState<{ providerId: string; status?: ProviderCredentialStatus; error?: string } | null>(null)
  const [credentialActionError, setCredentialActionError] = useState('')
  const [credentialNotice, setCredentialNotice] = useState('')
  const [secret, setSecret] = useState('')
  const busy = busyAction !== null
  const activeProtocolDraft = protocolDrafts[kind]
  const currentProviderId = currentProvider?.provider_id
  const credentialReadForProvider = credentialRead?.providerId === currentProviderId ? credentialRead : null
  const credentialStatus = credentialReadForProvider?.status ?? null
  const credentialStatusLoading = Boolean(currentProviderId && !credentialReadForProvider)
  const credentialStatusError = credentialReadForProvider?.error ?? ''
  const credentialConfigMatches = Boolean(currentProvider
    && kind === currentProvider.kind
    && activeProtocolDraft.baseUrl.trim() === currentProvider.base_url.trim())

  const closeDialog = () => { if (!busyRef.current) onClose() }
  useModalKeyboard(dialogRef, { onClose: closeDialog, busy })

  const beginBusy = (action: NonNullable<typeof busyAction>) => {
    if (busyRef.current) return false
    busyRef.current = true
    setBusyAction(action)
    return true
  }
  const endBusy = () => {
    busyRef.current = false
    setBusyAction(null)
  }
  const updateProtocolDraft = (field: keyof ProviderProtocolDraft, value: string) => {
    setProtocolDrafts((current) => ({ ...current, [kind]: { ...current[kind], [field]: value } }))
  }

  useEffect(() => {
    let current = true
    if (!currentProviderId) return () => { current = false }
    void api.getProviderCredential(currentProviderId)
      .then((status) => { if (current) setCredentialRead({ providerId: currentProviderId, status }) })
      .catch((reason: unknown) => { if (current) setCredentialRead({ providerId: currentProviderId, error: errorMessage(reason) }) })
    return () => { current = false }
  }, [currentProviderId])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(''); setCredentialNotice('')
    const submittedKind = kind
    const submittedProtocolDraft = activeProtocolDraft
    let ollamaContextTokens: number | null | undefined
    if (submittedKind === 'ollama') {
      const rawContextTokens = submittedProtocolDraft.ollamaContextTokens ?? ''
      if (rawContextTokens === '') {
        ollamaContextTokens = null
      } else {
        const parsedContextTokens = Number(rawContextTokens)
        if (!/^[0-9]+$/.test(rawContextTokens) || !Number.isSafeInteger(parsedContextTokens) || parsedContextTokens < 1 || parsedContextTokens > 2_147_483_647) {
          setError('上下文容量必须是 1 到 2,147,483,647 之间的整数。')
          return
        }
        ollamaContextTokens = parsedContextTokens
      }
    }
    if (!beginBusy('provider-save')) return
    try {
      const saved = await onSave({ name, kind: submittedKind, base_url: submittedProtocolDraft.baseUrl, secret_ref: currentProvider?.secret_ref, enabled, default_model: submittedProtocolDraft.defaultModel || undefined, ...(submittedKind === 'ollama' ? { ollama_context_tokens: ollamaContextTokens } : {}) }, currentProvider)
      setCurrentProvider(saved)
      setProtocolDrafts((current) => {
        const currentDraft = current[submittedKind]
        if (currentDraft.baseUrl !== submittedProtocolDraft.baseUrl || currentDraft.defaultModel !== submittedProtocolDraft.defaultModel || currentDraft.ollamaContextTokens !== submittedProtocolDraft.ollamaContextTokens) return current
        return { ...current, [submittedKind]: { ...currentDraft, baseUrl: saved.base_url } }
      })
    } catch (reason) {
      setError(reason instanceof ApiError && reason.code === 'PROVIDER_CREDENTIAL_DESTINATION_CONFLICT'
        ? '已保存的密钥绑定到原连接。请为新端点新建供应商，或先删除此供应商的 Windows 凭据后再切换。'
        : errorMessage(reason))
    } finally { endBusy() }
  }
  const saveCredential = async (event: FormEvent) => {
    event.preventDefault()
    if (!currentProvider || !credentialConfigMatches || !secret || credentialStatus?.backend !== 'windows_credential_manager' || !beginBusy('credential-save')) return
    setCredentialActionError(''); setCredentialNotice('')
    try {
      const result = await api.saveProviderCredential(currentProvider.provider_id, secret)
      setSecret('')
      setCurrentProvider({ ...currentProvider, secret_ref: result.secret_ref })
      setCredentialRead({ providerId: currentProvider.provider_id, status: { configured: true, backend: 'windows_credential_manager' } })
      setCredentialNotice('API 密钥已保存到 Windows 凭据管理器。密钥内容不会再次显示。')
    } catch (reason) { setCredentialActionError(errorMessage(reason)) }
    finally { endBusy() }
  }
  const deleteCredential = async () => {
    if (!currentProvider || !credentialStatus?.configured || !beginBusy('credential-delete')) return
    setCredentialActionError(''); setCredentialNotice('')
    try {
      await api.deleteProviderCredential(currentProvider.provider_id)
      setCurrentProvider({ ...currentProvider, secret_ref: undefined })
      setCredentialRead({ providerId: currentProvider.provider_id, status: { configured: false, backend: 'windows_credential_manager' } })
      setCredentialNotice('已从 Windows 凭据管理器删除此密钥。')
    } catch (reason) { setCredentialActionError(errorMessage(reason)) }
    finally { endBusy() }
  }
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDialog() }}><section ref={dialogRef} tabIndex={-1} className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-title"><div className="dialog-topline"><span className="eyebrow"><span className="eyebrow-line" />模型连接</span><button type="button" className="icon-button" onClick={closeDialog} aria-label="关闭" disabled={busy}><Icon name="close" /></button></div><h2 id="provider-title">{provider ? '编辑供应商' : '添加供应商'}</h2><p className="dialog-subtext">先保存连接配置，再通过本机凭据库安全保存密钥。</p><form onSubmit={(event) => void submit(event)}><label className="field-label" htmlFor="provider-name">显示名称</label><input id="provider-name" data-dialog-initial-focus value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：本地 Ollama" required disabled={busy} /><label className="field-label" htmlFor="provider-kind">协议类型</label><select id="provider-kind" value={kind} onChange={(event) => setKind(event.target.value as ProviderDraft['kind'])} disabled={busy}><option value="ollama">Ollama</option><option value="deepseek">DeepSeek</option><option value="openai_compatible">OpenAI 兼容</option><option value="anthropic">Anthropic Messages</option></select><label className="field-label" htmlFor="provider-url">服务地址</label><input id="provider-url" value={activeProtocolDraft.baseUrl} onChange={(event) => updateProtocolDraft('baseUrl', event.target.value)} placeholder="https://api.example.com" required disabled={busy} /><label className="field-label" htmlFor="provider-model">默认模型 <span className="optional-label">可选</span></label><input id="provider-model" value={activeProtocolDraft.defaultModel} onChange={(event) => updateProtocolDraft('defaultModel', event.target.value)} placeholder="留空以发现模型" disabled={busy} />{kind === 'ollama' && <><label className="field-label" htmlFor="provider-ollama-context">上下文容量（Token）</label><input id="provider-ollama-context" type="text" inputMode="numeric" aria-label="上下文容量（Token）" aria-describedby="provider-ollama-context-help" value={activeProtocolDraft.ollamaContextTokens ?? ''} placeholder="本机服务默认值" onChange={(event) => updateProtocolDraft('ollamaContextTokens', event.target.value)} disabled={busy} /><span className="optional-label" id="provider-ollama-context-help">留空使用本机服务默认值；较大的容量会占用更多内存。</span></>}<label className="provider-enabled-control"><span><strong>启用连接</strong><small>允许职责路由使用此服务</small></span><button type="button" className={`toggle-switch ${enabled ? 'on' : ''}`} aria-pressed={enabled} onClick={() => setEnabled(!enabled)} disabled={busy}><span /></button></label>{error && <p className="form-error" role="alert">{error}</p>}<button className="button button-primary dialog-submit" disabled={busy}>{busyAction === 'provider-save' ? <><Icon name="spinner" size={16} />保存中…</> : <>保存连接 <Icon name="arrow-right" size={16} /></>}</button></form>
      <section className="provider-credential-panel" aria-labelledby="provider-credential-title"><div className="credential-panel-heading"><div><span className="panel-overline">本机凭据</span><h3 id="provider-credential-title">API 密钥</h3></div><Icon name="shield" size={16} /></div>{!currentProvider ? <p className="credential-status-copy">保存连接配置后，可在这里设置 API 密钥。</p> : credentialStatusLoading ? <p className="credential-status-copy">正在读取凭据状态…</p> : credentialStatusError && !credentialStatus ? <p className="credential-status-copy">无法确认凭据库状态，请稍后重试。{credentialStatusError}</p> : credentialStatus?.backend === 'unavailable' ? <p className="credential-status-copy">Windows 凭据管理器不可用。密钥输入已停用，不会改存到浏览器或普通文件。</p> : credentialStatus?.configured ? <p className="credential-status-copy is-configured">密钥已设置。出于安全原因，不能查看已保存的内容。</p> : currentProvider ? <p className="credential-status-copy">尚未设置密钥。留空用于不需要密钥的本地服务。</p> : null}{currentProvider && !credentialConfigMatches && <p className="credential-status-copy">协议或服务地址已更改。请先保存连接，再输入或保存密钥；已保存的密钥仍可删除。</p>}
        {currentProvider && credentialStatus?.backend === 'windows_credential_manager' && <form className="credential-form" onSubmit={(event) => void saveCredential(event)}><label className="field-label" htmlFor="provider-api-key">{credentialStatus.configured ? '替换 API 密钥' : '输入 API 密钥'}</label><input id="provider-api-key" type="password" value={secret} onChange={(event) => setSecret(event.target.value)} autoComplete="new-password" spellCheck={false} disabled={busy || !credentialConfigMatches} aria-describedby="credential-storage-note" placeholder={credentialStatus.configured ? '输入新密钥以替换' : '密钥仅发送到本机服务'} /><p className="provider-secret-note" id="credential-storage-note"><Icon name="shield" size={13} />只写入 Windows 凭据管理器；保存后立即清空，不会回读。</p><div className="credential-actions"><button className="button button-primary button-small" disabled={!secret || busy || !credentialConfigMatches}>{busyAction === 'credential-save' ? '保存中…' : '保存密钥'}</button>{credentialStatus.configured && <button className="button button-secondary button-small" type="button" disabled={busy} onClick={() => void deleteCredential()}>{busyAction === 'credential-delete' ? '处理中…' : '删除密钥'}</button>}</div></form>}
        {(credentialNotice || credentialStatusError || credentialActionError) && <p className={credentialStatusError || credentialActionError ? 'form-error' : 'credential-success'} role={credentialStatusError || credentialActionError ? 'alert' : 'status'}>{credentialActionError || credentialStatusError || credentialNotice}</p>}
      </section><div className="dialog-footer-actions"><button className="button button-secondary" type="button" onClick={closeDialog} disabled={busy}>完成</button></div></section></div>
}

function roleLabel(role: string) { return ({ intent: '意图解释', intent_classification: '意图解释', gm_answer: 'GM 回答', npc_decision: '人物决策', npc_plan_review: '人物计划复审', social_adjudication: '社会裁定', narration: '叙述', memory_summary: '记忆摘要', author_assistance: '作者辅助', author_assist: '作者辅助', embedding: '嵌入' } as Record<string, string>)[role] ?? role }
function roleIcon(role: string): IconName { return role.includes('narr') ? 'message' : role.includes('npc') ? 'user' : role.includes('author') ? 'edit' : role.includes('social') ? 'users' : 'spark' }
function providerIcon(kind: string): IconName { return kind === 'ollama' ? 'server' : kind === 'anthropic' ? 'brain' : 'cloud' }
function formatTime(value: string | undefined) { return value ? new Date(value).toLocaleString('zh-HK', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '时间未知' }
function csvCell(value: unknown) {
  const text = String(value ?? '')
  // Quoting alone does not stop spreadsheet apps from evaluating a formula.
  const safeText = /^[\s\uFEFF]*[=+\-@]/.test(text) ? `'${text}` : text
  return `"${safeText.replaceAll('"', '""')}"`
}
