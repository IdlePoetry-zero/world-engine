import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, type CallRecord, type ModelInfo, type Provider, type ProviderDraft, type Settings } from '../api/client'
import { errorMessage } from '../utils'
import { Icon, type IconName } from '../components/Icon'
import { InlineState } from '../components/States'
import type { Notice } from '../App'

type OpsTab = 'overview' | 'providers' | 'routes' | 'calls'
export function OperationsPage({ onNotice }: { onNotice: (notice: Notice) => void }) {
  const [tab, setTab] = useState<OpsTab>('overview')
  const [settings, setSettings] = useState<Settings | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [calls, setCalls] = useState<CallRecord[]>([])
  const [models, setModels] = useState<Record<string, ModelInfo[]>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [savingSettings, setSavingSettings] = useState(false)
  const [providerModal, setProviderModal] = useState<Provider | 'new' | null>(null)

  const reload = useCallback(async () => {
    setLoading(true); setError(null)
    const results = await Promise.allSettled([api.getSettings(), api.listProviders(), api.listCalls()])
    const [settingsResult, providersResult, callsResult] = results
    if (settingsResult.status === 'fulfilled') setSettings(settingsResult.value)
    if (providersResult.status === 'fulfilled') {
      setProviders(providersResult.value.items)
      const found = await Promise.allSettled(providersResult.value.items.map((provider) => api.listModels(provider.provider_id)))
      const modelMap: Record<string, ModelInfo[]> = {}
      found.forEach((result, index) => { if (result.status === 'fulfilled') modelMap[providersResult.value.items[index].provider_id] = result.value.items })
      setModels(modelMap)
    }
    if (callsResult.status === 'fulfilled') setCalls(callsResult.value.items)
    const rejected = results.find((result) => result.status === 'rejected')
    if (rejected?.status === 'rejected') setError(errorMessage(rejected.reason))
    setLoading(false)
  }, [])
  useEffect(() => { void reload() }, [reload])

  const updateSetting = async (patch: Partial<Settings>) => {
    if (!settings) return
    const next = { ...settings, ...patch }
    setSettings(next); setSavingSettings(true)
    try { const saved = await api.updateSettings(next); setSettings(saved); onNotice({ kind: 'success', text: '运行设置已保存。' }) }
    catch (reason) { setError(errorMessage(reason)); onNotice({ kind: 'error', text: errorMessage(reason) }); void reload() }
    finally { setSavingSettings(false) }
  }

  const saveProvider = async (draft: ProviderDraft, provider?: Provider) => {
    const saved = provider ? await api.updateProvider(provider.provider_id, draft) : await api.createProvider(draft)
    setProviderModal(null); onNotice({ kind: 'success', text: provider ? '供应商配置已保存。' : '供应商已添加。' }); await reload(); return saved
  }
  const probeProvider = async (provider: Provider) => {
    try {
      const result = await api.probeProvider(provider.provider_id)
      onNotice({ kind: 'info', text: result.detail ?? `目录发现状态：${result.status ?? '服务端未提供状态标签'}` })
      await reload()
    } catch (reason) { onNotice({ kind: 'error', text: errorMessage(reason) }) }
  }
  const exportCsv = () => {
    const header = ['request_id', 'started_at', 'role', 'provider', 'model', 'duration_ms', 'total_tokens', 'cost_state', 'cost_amount', 'currency', 'status']
    const rows = calls.map((call) => [call.request_id, call.started_at ?? '', call.role, call.provider ?? '', call.model ?? '', call.duration_ms ?? '', call.usage?.total_tokens ?? 'unknown', call.cost?.state ?? 'unknown', call.cost?.amount ?? 'unknown', call.cost?.currency ?? 'unknown', call.status])
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
      <section className="profile-settings panel-card"><div className="panel-card-heading"><div><span className="panel-overline">EXECUTION PROFILE</span><h2>速度与深度</h2></div><span className={`save-indicator ${savingSettings ? 'saving' : ''}`}><i />{savingSettings ? '保存中…' : settings ? '自动保存' : '等待设置'}</span></div><p className="panel-description">两项设置分别影响调度节奏和模拟精度。无论选择哪一档，世界硬规则始终生效。</p>{settings ? <div className="profile-controls"><ProfileControl title="速度" subtitle="请求调度、并发与呈现策略" value={settings.speed} options={[['fast', '快速', '更快地调度独立请求'], ['balanced', '均衡', '保持交互与后台平衡'], ['patient', '耐心', '减少资源竞争，完整校验后呈现']]} onChange={(value) => void updateSetting({ speed: value as Settings['speed'] })} /><ProfileControl title="深度" subtitle="推演范围、记忆和复核层级" value={settings.depth} options={[['standard', '标准', '精简候选与回顾范围'], ['deep', '深入', '扩展因果与人物考虑'], ['exhaustive', '充分', '更完整的候选与冲突审视']]} onChange={(value) => void updateSetting({ depth: value as Settings['depth'] })} /></div> : <div className="settings-empty">{loading ? '正在读取运行设置…' : '服务端尚未返回运行设置。'}</div>}<div className="profile-separation"><span className="separation-icon"><Icon name="crosshair" size={15} /></span><span><strong>互相独立的两个维度</strong><small>快速 + 充分仍会执行充分推演；耐心 + 标准不会自动增加模拟深度。</small></span></div></section>
      <div className="ops-overview-bottom"><div className="mini-panel"><span className="panel-overline">LOCAL API</span><h3>连接检查</h3><p>设置状态来自服务端的运行配置响应。</p><button className="text-button" onClick={() => void reload()}>重新检测 <Icon name="arrow-right" size={14} /></button></div><div className="mini-panel"><span className="panel-overline">PRIVACY</span><h3>凭据只留在本机</h3><p>浏览器只保存 secret reference，不接收或回显供应商密钥。</p><span className="privacy-badge"><Icon name="shield" size={13} />本地凭据边界</span></div></div>
    </div>}
    {tab === 'providers' && <div className="providers-page"><div className="subpage-header"><div><h2>模型连接</h2><p>目录发现只返回可见模型；生成能力需经过独立验证。</p></div><button className="button button-primary" onClick={() => setProviderModal('new')}><Icon name="plus" size={16} />添加供应商</button></div>{loading ? <div className="provider-skeleton" /> : providers.length ? <div className="provider-list">{providers.map((provider) => <ProviderCard key={provider.provider_id} provider={provider} models={models[provider.provider_id] ?? []} onEdit={() => setProviderModal(provider)} onProbe={() => void probeProvider(provider)} />)}</div> : <InlineState icon="plug" title="还没有模型连接" description="添加一个供应商端点后，可以发现模型目录。密钥由后端凭据库管理。" action={<button className="button button-primary" onClick={() => setProviderModal('new')}><Icon name="plus" size={15} />添加供应商</button>} />}</div>}
    {tab === 'routes' && <RoutesPage settings={settings} providers={providers} models={models} loading={loading} saving={savingSettings} onChange={updateSetting} />}
    {tab === 'calls' && <div className="calls-page"><div className="subpage-header"><div><h2>调用记录</h2><p>默认不展示提示正文。未知用量和费用保持为未知，不显示为零。</p></div><button className="button button-secondary button-small" onClick={exportCsv} disabled={!calls.length}><Icon name="download" size={14} />导出 CSV</button></div>{calls.length ? <div className="calls-table-wrap"><table className="calls-table"><thead><tr><th>时间 / 调用</th><th>职责</th><th>供应商 / 模型</th><th>延迟</th><th>Token</th><th>费用</th><th>结果</th><th>追踪</th></tr></thead><tbody>{calls.map((call) => <tr key={call.request_id}><td><strong>{formatTime(call.started_at)}</strong><small>{call.request_id.slice(0, 12)}</small></td><td>{roleLabel(call.role)}</td><td><strong>{call.provider ?? '未知'}</strong><small>{call.model ?? '模型未知'}</small></td><td>{call.duration_ms == null ? '未知' : `${call.duration_ms} ms`}</td><td>{call.usage?.total_tokens == null ? '未知' : `${call.usage.total_tokens} tokens`}</td><td><CostCell cost={call.cost} /></td><td><span className={`call-status call-${call.status}`}>{call.status}</span></td><td><details className="trace-details"><summary>查看</summary><pre>{JSON.stringify(call.trace ?? { request_id: call.request_id, role: call.role, provider: call.provider, model: call.model, status: call.status }, null, 2)}</pre></details></td></tr>)}</tbody></table></div> : <InlineState icon="activity" title="还没有调用记录" description="请求实际通过模型后台后，服务端会返回可核对的运行信息。" />}</div>}
    {providerModal && <ProviderDialog provider={providerModal === 'new' ? undefined : providerModal} onClose={() => setProviderModal(null)} onSave={saveProvider} />}
  </section>
}

function SummaryCard({ label, icon, value, note, action, onClick }: { label: string; icon: IconName; value: number | string; note: string; action: string; onClick: () => void }) { return <div className="ops-summary-card"><div className="summary-card-label">{label}<Icon name={icon} size={16} /></div><strong>{value}</strong><span>{note}</span><div className="summary-card-bottom"><button className="text-button" onClick={onClick}>{action} <Icon name="arrow-right" size={14} /></button></div></div> }

function ProfileControl({ title, subtitle, value, options, onChange }: { title: string; subtitle: string; value: string; options: [string, string, string][]; onChange: (value: string) => void }) { return <div className="profile-control"><div className="profile-control-title"><div><strong>{title}</strong><small>{subtitle}</small></div><span>{options.find((option) => option[0] === value)?.[1] ?? value}</span></div><div className="segmented-options" role="group" aria-label={title}>{options.map(([option, label, description]) => <button key={option} className={value === option ? 'selected' : ''} onClick={() => onChange(option)} aria-pressed={value === option} title={description}><span>{label}</span><i /></button>)}</div><div className="profile-control-foot">{options.find((option) => option[0] === value)?.[2]}</div></div> }

function RoutesPage({ settings, providers, models, loading, saving, onChange }: { settings: Settings | null; providers: Provider[]; models: Record<string, ModelInfo[]>; loading: boolean; saving: boolean; onChange: (patch: Partial<Settings>) => Promise<void> }) {
  if (!settings) return <div className="routes-page"><div className="subpage-header"><div><h2>职责路由</h2><p>每类任务独立选择首选模型；本地限定策略不会自动回退到云端。</p></div></div><div className="settings-empty">{loading ? '正在读取职责路由…' : '服务端尚未返回路由设置。'}</div></div>
  return <div className="routes-page"><div className="subpage-header"><div><h2>职责路由</h2><p>每类任务独立选择首选模型；本地限定策略不会自动回退到云端。</p></div></div><div className="local-only-row"><span className="local-only-copy"><span className="route-local-icon"><Icon name="shield" size={16} /></span><span><strong>仅使用本地服务</strong><small>阻止选择的内容发送到云端供应商</small></span></span><button type="button" className={`toggle-switch ${settings.local_only ? 'on' : ''}`} aria-pressed={settings.local_only} onClick={() => void onChange({ local_only: !settings.local_only })}><span /></button></div><div className="routes-list">{Object.entries(settings.routes).map(([role, route]) => <div className="route-row" key={role}><div className="route-role"><span className="route-role-icon"><Icon name={roleIcon(role)} size={15} /></span><span><strong>{roleLabel(role)}</strong><small>{role}</small></span></div><div className="route-selects"><select aria-label={`${roleLabel(role)}供应商`} value={route.provider_id ?? ''} onChange={(event) => { const routes = { ...settings.routes, [role]: { ...route, provider_id: event.target.value || undefined } }; void onChange({ routes }) }}><option value="">使用系统默认</option>{providers.filter((provider) => provider.enabled).map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.name}</option>)}</select><select aria-label={`${roleLabel(role)}模型`} value={route.model ?? ''} onChange={(event) => { const routes = { ...settings.routes, [role]: { ...route, model: event.target.value || undefined } }; void onChange({ routes }) }}><option value="">自动发现</option>{(models[route.provider_id ?? ''] ?? []).map((model) => <option key={model.id} value={model.id}>{model.name ?? model.id}</option>)}</select></div><span className="route-save">{saving ? '保存中' : '已保存'}</span></div>)}</div>{Object.keys(settings.routes).length === 0 && <InlineState icon="route" title="暂无职责路由" description="当前设置没有返回可配置的职责。" />}</div>
}

function ProviderCard({ provider, models, onEdit, onProbe }: { provider: Provider; models: ModelInfo[]; onEdit: () => void; onProbe: () => void }) { return <article className="provider-card"><div className="provider-card-head"><div className={`provider-avatar provider-${provider.kind}`}><Icon name={providerIcon(provider.kind)} size={18} /></div><div className="provider-name-block"><h3>{provider.name}</h3><span>{provider.kind} · {provider.base_url}</span></div><span className={`provider-enabled ${provider.enabled ? 'is-enabled' : ''}`}><i />{provider.enabled ? '已启用' : '已停用'}</span><button className="icon-button" aria-label={`编辑 ${provider.name}`} onClick={onEdit}><Icon name="edit" size={16} /></button></div><div className="provider-card-bottom"><div className="provider-model-stat"><span className="provider-meta-label">目录模型</span><strong>{models.length}</strong>{models.length > 0 && <span className="model-chip">{models.slice(0, 2).map((model) => model.name ?? model.id).join(' · ')}{models.length > 2 ? ` +${models.length - 2}` : ''}</span>}</div><div className="provider-probe-controls"><span className="discovery-note">发现目录不等同于生成验证</span><button className="button button-secondary button-small" onClick={onProbe} disabled={!provider.enabled}><Icon name="refresh" size={14} />发现模型目录</button></div></div></article> }

function CostCell({ cost }: { cost: CallRecord['cost'] }) {
  if (!cost || cost.state === 'unknown' || cost.amount == null) return <span className="cost-unknown" title="服务端没有报告确定费用">未知</span>
  const amount = `${cost.currency ?? ''} ${cost.amount}`.trim()
  return <span className={`cost-${cost.state}`} title={cost.source_url ?? (cost.state === 'estimated' ? '费用为估算值' : '服务端报告费用')}>{cost.state === 'estimated' ? `约 ${amount}` : amount}</span>
}

function ProviderDialog({ provider, onClose, onSave }: { provider?: Provider; onClose: () => void; onSave: (draft: ProviderDraft, provider?: Provider) => Promise<Provider> }) {
  const [name, setName] = useState(provider?.name ?? '')
  const [kind, setKind] = useState<ProviderDraft['kind']>((provider?.kind as ProviderDraft['kind']) ?? 'ollama')
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? 'http://127.0.0.1:11434')
  const [secretRef, setSecretRef] = useState(provider?.secret_ref ?? '')
  const [enabled, setEnabled] = useState(provider?.enabled ?? true)
  const [defaultModel, setDefaultModel] = useState(provider?.default_model ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => { event.preventDefault(); setSaving(true); setError(''); try { await onSave({ name, kind, base_url: baseUrl, secret_ref: secretRef || undefined, enabled, default_model: defaultModel || undefined }, provider) } catch (reason) { setError(errorMessage(reason)) } finally { setSaving(false) } }
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-title"><div className="dialog-topline"><span className="eyebrow"><span className="eyebrow-line" />模型连接</span><button className="icon-button" onClick={onClose} aria-label="关闭"><Icon name="close" /></button></div><h2 id="provider-title">{provider ? '编辑供应商' : '添加供应商'}</h2><p className="dialog-subtext">配置连接信息与凭据引用。不要在此粘贴 API 密钥。</p><form onSubmit={(event) => void submit(event)}><label className="field-label" htmlFor="provider-name">显示名称</label><input id="provider-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：本地 Ollama" required /><label className="field-label" htmlFor="provider-kind">协议类型</label><select id="provider-kind" value={kind} onChange={(event) => { setKind(event.target.value as ProviderDraft['kind']); if (event.target.value === 'ollama') setBaseUrl('http://127.0.0.1:11434') }}><option value="ollama">Ollama</option><option value="deepseek">DeepSeek</option><option value="openai_compatible">OpenAI 兼容</option><option value="anthropic">Anthropic Messages</option></select><label className="field-label" htmlFor="provider-url">服务地址</label><input id="provider-url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com" required /><label className="field-label" htmlFor="provider-secret">凭据引用 <span className="optional-label">可选</span></label><input id="provider-secret" value={secretRef} onChange={(event) => setSecretRef(event.target.value)} placeholder="secret://world-engine/provider-name" autoComplete="off" /><div className="provider-secret-note"><Icon name="shield" size={13} />输入 secret_ref 名称，不要输入密钥值。</div><label className="field-label" htmlFor="provider-model">默认模型 <span className="optional-label">可选</span></label><input id="provider-model" value={defaultModel} onChange={(event) => setDefaultModel(event.target.value)} placeholder="留空以发现模型" /><label className="provider-enabled-control"><span><strong>启用连接</strong><small>允许职责路由使用此服务</small></span><button type="button" className={`toggle-switch ${enabled ? 'on' : ''}`} aria-pressed={enabled} onClick={() => setEnabled(!enabled)}><span /></button></label>{error && <p className="form-error" role="alert">{error}</p>}<button className="button button-primary dialog-submit" disabled={saving}>{saving ? <><Icon name="spinner" size={16} />保存中…</> : <>保存连接 <Icon name="arrow-right" size={16} /></>}</button></form></section></div>
}

function roleLabel(role: string) { return ({ intent: '意图解释', intent_classification: '意图解释', gm_answer: 'GM 回答', npc_decision: '人物决策', social_adjudication: '社会裁定', narration: '叙述', memory_summary: '记忆摘要', author_assistance: '作者辅助', embedding: '嵌入' } as Record<string, string>)[role] ?? role }
function roleIcon(role: string): IconName { return role.includes('narr') ? 'message' : role.includes('npc') ? 'user' : role.includes('author') ? 'edit' : role.includes('social') ? 'users' : 'spark' }
function providerIcon(kind: string): IconName { return kind === 'ollama' ? 'server' : kind === 'anthropic' ? 'brain' : 'cloud' }
function formatTime(value: string | undefined) { return value ? new Date(value).toLocaleString('zh-HK', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '时间未知' }
function csvCell(value: unknown) { return `"${String(value ?? '').replaceAll('"', '""')}"` }
