import { useId, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, type AuthorRevisionPreview, type AuthorRevisionResult } from '../api/client'
import type { Notice } from '../App'
import { errorMessage } from '../utils'
import { Icon } from './Icon'

type RevisionEntity = { id: string; kind: string; name?: string | null; status?: string | null; components?: Record<string, unknown> }

const componentLabels: Record<string, string> = {
  life: '生命资料', health: '健康', needs: '需求', skills: '技能', personality: '性格与价值',
  attributes: '自定义数值', capabilities: '能力', progression: '成长', known_places: '已知地点',
  daily_routine: '每日作息', routine_mobility: '作息出行',
  place: '地点', market: '市场', production: '生产', governance: '治理', treasury: '资金库',
  inventory: '物品清单', population: '人口', public_description: '公开描述', appearance: '外貌',
  public_role: '公开身份', hidden: '隐藏标记', parent_id: '父实体',
}
const personalityFields = [
  ['openness', '开放性'], ['conscientiousness', '尽责性'], ['extraversion', '外向性'],
  ['agreeableness', '宜人性'], ['neuroticism', '情绪敏感度'],
  ['traits', '性格特征'], ['values', '重视的价值'],
] as const
const textComponents: Record<string, { label: string; maxLength: number }> = {
  public_description: { label: '公开描述', maxLength: 4000 },
  appearance: { label: '外貌描述', maxLength: 4000 },
  public_role: { label: '公开身份与职责', maxLength: 1000 },
}
const autonomousVacancyBallotAuthority = 'authority.autonomous_vacancy_ballot'
const revisionFieldLabels: Record<string, string> = {
  personality: '性格与价值',
  locked_fields: '锁定字段',
  grants: '能力授权',
  combat_death_protection: '战斗保命',
  routes: '路线',
  route_id: '路线标识',
  to_place_id: '目的地点',
  from_place_id: '出发地点',
  access_tags: '通行标签',
  routine_mobility: '作息出行',
  alternatives: '备选地点',
  avoid_conditions: '避开天气',
}

export function AuthorRevisionEditor({
  worldId, branchId, stateVersion, entities, referenceEntities = entities, playerId, daySeconds = 86_400, onNotice, onCommitted,
}: {
  worldId: string
  branchId: string
  stateVersion: string
  entities: RevisionEntity[]
  referenceEntities?: RevisionEntity[]
  playerId?: string
  daySeconds?: number
  onNotice: (notice: Notice) => void
  onCommitted: (result?: AuthorRevisionResult) => void
}) {
  const [selectedEntityId, setSelectedEntityId] = useState(entities[0]?.id ?? '')
  const entity = entities.find((item) => item.id === selectedEntityId) ?? entities[0]
  const isNpc = Boolean(entity && isCharacterKind(entity.kind) && entity.id !== playerId)
  const availableComponents = getAvailableComponents(entity, isNpc)
  const [selectedComponent, setSelectedComponent] = useState(availableComponents[0] ?? '')
  const componentName = availableComponents.includes(selectedComponent) ? selectedComponent : availableComponents[0] ?? ''
  const componentKey = `${entity?.id ?? ''}:${componentName}`
  const originalValue = entity?.components?.[componentName]
  const [draftState, setDraftState] = useState<{ key: string; value: unknown } | null>(null)
  const defaultDraft = componentName === 'daily_routine' ? { windows: [] }
    : componentName === 'routine_mobility' ? { enabled: false, alternatives: {}, avoid_conditions: [] }
    : componentName === 'capabilities' && isNpc ? {} : undefined
  const draft = draftState?.key === componentKey ? draftState.value : cloneJsonValue(originalValue === undefined ? defaultDraft : originalValue)
  const [advancedState, setAdvancedState] = useState<{ key: string; enabled: boolean } | null>(null)
  const advanced = advancedState?.key === componentKey && advancedState.enabled
  const [advancedTextState, setAdvancedTextState] = useState<{ key: string; text: string } | null>(null)
  const advancedText = advancedTextState?.key === componentKey ? advancedTextState.text : JSON.stringify(draft, null, 2) ?? 'null'
  const [reason, setReason] = useState('')
  const [reasonKey, setReasonKey] = useState(componentKey)
  const currentReason = reasonKey === componentKey ? reason : ''
  const [previewState, setPreviewState] = useState<{ key: string; value: AuthorRevisionPreview } | null>(null)
  const preview = previewState?.key === componentKey ? previewState.value : null
  const [failure, setFailure] = useState<{ key: string; reason: unknown } | null>(null)
  const currentFailure = failure?.key === componentKey ? failure : null
  const error = currentFailure ? errorMessage(currentFailure.reason) : ''
  const fieldErrors = revisionFieldErrors(currentFailure?.reason)
  const errorId = useId()
  const [refreshRequired, setRefreshRequired] = useState(false)
  const [busy, setBusy] = useState(false)
  const showRequestError = (reason: unknown) => {
    setFailure({ key: componentKey, reason })
    if (reason instanceof ApiError && reason.status === 409 && reason.code !== 'AUTHOR_RULE_CONFLICT') {
      setRefreshRequired(true)
    }
  }
  const setDraft = (value: unknown) => {
    setDraftState({ key: componentKey, value })
    setPreviewState(null)
    setFailure(null)
  }
  const setCurrentReason = (value: string) => {
    setReasonKey(componentKey)
    setReason(value)
    setPreviewState(null)
    setFailure(null)
  }
  const changeComponent = (name: string) => {
    setSelectedComponent(name)
    setPreviewState(null)
    setFailure(null)
    setReasonKey(`${entity?.id ?? ''}:${name}`)
    setReason('')
  }
  const changeEntity = (id: string) => {
    setSelectedEntityId(id)
    setPreviewState(null)
    setFailure(null)
    setReason('')
  }
  const refreshProjection = () => {
    setRefreshRequired(false)
    setPreviewState(null)
    setFailure(null)
    onCommitted()
  }
  const toggleAdvanced = () => {
    if (!advanced) {
      setAdvancedTextState({ key: componentKey, text: JSON.stringify(draft, null, 2) ?? 'null' })
      setAdvancedState({ key: componentKey, enabled: true })
      setFailure(null)
      return
    }
    try {
      setDraft(JSON.parse(advancedText) as unknown)
      setAdvancedState({ key: componentKey, enabled: false })
    } catch {
      setFailure({ key: componentKey, reason: new Error('JSON 格式无效。修正后再切回表单，或直接使用此 JSON 请求预览。') })
    }
  }
  const previewRevision = async (event: FormEvent) => {
    event.preventDefault()
    if (!entity || !componentName || !branchId || !currentReason.trim()) return
    setBusy(true); setFailure(null); setPreviewState(null); setRefreshRequired(false)
    try {
      const replacement = editsJson ? JSON.parse(advancedText) as unknown : draft
      const result = await api.previewAuthorRevision(worldId, {
        branch_id: branchId,
        patch_proposal: { entity_id: entity.id, component_name: componentName, replacement, reason: currentReason.trim() },
      })
      setPreviewState({ key: componentKey, value: result })
    } catch (reasonValue) {
      showRequestError(reasonValue)
    } finally { setBusy(false) }
  }
  const commitRevision = async () => {
    if (!preview) return
    setBusy(true); setFailure(null)
    try {
      const result = await api.commitAuthorRevision(worldId, { preview_id: preview.preview_id, expected_version: preview.expected_version })
      setPreviewState(null)
      onNotice({ kind: 'success', text: `修订已提交 · 状态版本 ${result.state_version} · 世界时间未推进` })
      onCommitted(result)
    } catch (reasonValue) {
      showRequestError(reasonValue)
    } finally { setBusy(false) }
  }

  const isPublicText = Boolean(textComponents[componentName])
  const isPersonality = componentName === 'personality'
  const isAttributes = componentName === 'attributes'
  const isHealth = componentName === 'health'
  const isNpcCapabilities = componentName === 'capabilities' && isNpc
  const isProgression = componentName === 'progression'
  const isHidden = componentName === 'hidden'
  const isRoutine = componentName === 'daily_routine'
  const isRoutineMobility = componentName === 'routine_mobility'
  const rawPopulationSeatPolicy = asRecord(draft).seat_policy
  const hasPopulationSeatPolicy = componentName === 'population'
    && rawPopulationSeatPolicy !== null
    && typeof rawPopulationSeatPolicy === 'object'
    && !Array.isArray(rawPopulationSeatPolicy)
  const simpleEditor = isPublicText || isPersonality || isAttributes || isHealth || isNpcCapabilities || isProgression || isHidden || isRoutine || isRoutineMobility || componentName === 'place' || componentName === 'governance' || hasPopulationSeatPolicy
  const editsJson = advanced || !simpleEditor
  const governanceError = componentName === 'governance' && !advanced ? governanceMembershipError(draft) : ''
  const routineError = isRoutine && !advanced ? routineValidationError(draft, daySeconds) : ''
  const routineMobilityError = isRoutineMobility && !advanced ? routineWeatherValidationError(draft) : ''
  const waypointOpportunityError = componentName === 'place' && !advanced ? waypointOpportunityValidationError(draft) : ''
  const populationSeatPolicyError = hasPopulationSeatPolicy && !advanced ? populationSeatPolicyValidationError(draft) : ''
  const canPreview = Boolean(entity && componentName && currentReason.trim() && !busy && !refreshRequired && !governanceError && !routineError && !routineMobilityError && !waypointOpportunityError && !populationSeatPolicyError && branchId && (advanced || simpleEditor || draft !== undefined))

  if (!entities.length || !entity) return <section className="author-revision-panel"><div className="author-revision-heading"><span className="panel-overline">AUTHOR REVISION</span><h3>组件修订</h3></div><p className="author-revision-muted">当前作者投影没有可编辑的实体。</p></section>

  return <section className="author-revision-panel" aria-labelledby="author-revision-title">
    <div className="author-revision-heading"><div><span className="panel-overline">AUTHOR REVISION · TICK 不变</span><h3 id="author-revision-title">组件修订</h3><p>先由服务端验证完整档案和引用，再查看前后差异并提交。</p></div><Icon name="edit" size={17} /></div>
    <div className="author-revision-targets">
      {entities.length > 1 && <label><span>修订对象</span><select value={entity.id} onChange={(event) => changeEntity(event.target.value)} aria-label="选择修订对象">{entities.map((item) => <option key={item.id} value={item.id}>{item.name || item.id} · {item.kind}</option>)}</select></label>}
      <label><span>组件</span><select value={componentName} onChange={(event) => changeComponent(event.target.value)} aria-label="选择组件" disabled={!availableComponents.length}>{availableComponents.length ? availableComponents.map((name) => <option key={name} value={name}>{componentLabels[name] ?? name}</option>) : <option value="">暂无已注册组件</option>}</select></label>
    </div>
    {!availableComponents.length || !componentName ? <p className="author-revision-muted">此实体没有当前可编辑的已注册组件。</p> : <form onSubmit={(event) => void previewRevision(event)}>
      <div className="author-field-heading"><label htmlFor="author-revision-draft">修订内容</label>{simpleEditor && <button type="button" className="text-button author-advanced-toggle" onClick={toggleAdvanced}>{advanced ? '返回表单' : '高级 JSON'} <Icon name="layers" size={13} /></button>}</div>
      {editsJson ? <><textarea id="author-revision-draft" className="author-json-input" value={advancedText} onChange={(event) => { setAdvancedTextState({ key: componentKey, text: event.target.value }); setPreviewState(null); setFailure(null) }} spellCheck={false} aria-label={`${componentLabels[componentName]} JSON`} aria-invalid={fieldErrors.length > 0 || undefined} aria-describedby={fieldErrors.length > 0 ? errorId : undefined} /><p className="author-revision-help">提交整个组件的 JSON 值。修订号由服务端管理；精确小数请用字符串表达。</p></> : isPersonality
        ? <PersonalityComponentFields key={componentKey} draft={draft} originalValue={originalValue} onChange={setDraft} />
        : isRoutineMobility
          ? <RoutineMobilityComponentFields key={componentKey} record={asRecord(draft)} routine={asRecord(entity.components?.daily_routine)} entities={referenceEntities} onChange={setDraft} />
          : <SimpleComponentFields key={componentKey} name={componentName} draft={draft} onChange={setDraft} referenceEntities={referenceEntities} daySeconds={daySeconds} />}
      {governanceError && <p className="author-revision-inline-warning" role="status">{governanceError}</p>}
      {routineError && <p className="author-revision-inline-warning" role="status">{routineError}</p>}
      {routineMobilityError && <p className="author-revision-inline-warning" role="status">{routineMobilityError}</p>}
      {waypointOpportunityError && <p className="author-revision-inline-warning" role="status">{waypointOpportunityError}</p>}
      {populationSeatPolicyError && <p className="author-revision-inline-warning" role="status">{populationSeatPolicyError}</p>}
      <label className="author-reason-field" htmlFor="author-revision-reason"><span>修订原因</span><textarea id="author-revision-reason" value={currentReason} onChange={(event) => setCurrentReason(event.target.value)} maxLength={1000} rows={2} placeholder="说明这项设定变更的原因" required /></label>
      {error && <div id={errorId} className="form-error" role="alert">
        <div>{error}</div>
        {fieldErrors.length > 0 && <ul>{fieldErrors.map((field) => <li key={field}>{field}</li>)}</ul>}
      </div>}
      <button className="button button-primary author-preview-button" type="submit" disabled={!canPreview}>{busy ? <><Icon name="spinner" size={15} />处理中…</> : <>验证并预览 <Icon name="arrow-right" size={15} /></>}</button>
    </form>}
    {refreshRequired && !preview && <div className="author-revision-stale" role="alert"><div><strong>作者档案需要刷新</strong><span>服务端发现状态版本已变化。刷新后再检查草稿并重新预览；未提交的表单内容可能被清除。</span></div><button type="button" className="button button-secondary button-small" disabled={busy} onClick={refreshProjection}>刷新作者投影</button></div>}
    {preview && <div className="author-revision-preview"><div className="author-preview-heading"><div><span className="panel-overline">SERVER VALIDATED · {preview.expected_version}</span><h4>提交前预览</h4></div><span className="no-tick-effect">世界时间不变</span></div><div className="author-diff-grid"><div><span>当前值</span><pre>{prettyJson(preview.before)}</pre></div><div><span>预览值</span><pre>{prettyJson(preview.after)}</pre></div></div><p className="author-revision-help">预览将在 {new Date(preview.expires_at).toLocaleTimeString('zh-HK', { hour: '2-digit', minute: '2-digit', hour12: false })} 到期。提交使用预览时的状态版本 {preview.expected_version}。</p>{refreshRequired || preview.expected_version !== stateVersion ? <button type="button" className="button button-secondary author-commit-button" disabled={busy} onClick={refreshProjection}>刷新作者投影</button> : <button type="button" className="button button-primary author-commit-button" disabled={busy} onClick={() => void commitRevision()}>{busy ? '提交中…' : '确认提交修订'}</button>}</div>}
  </section>
}

function revisionFieldErrors(reason: unknown): string[] {
  if (!(reason instanceof ApiError) || reason.code !== 'AUTHOR_RULE_CONFLICT' || !Array.isArray(reason.field_errors)) return []
  const errors = reason.field_errors.map(({ path, message }) => {
    const fieldPath = path.replace(/^patch_proposal\.replacement(?:\.|$)/, '')
    const label = fieldPath.split('.').filter(Boolean).map((part) => {
      if (/^\d+$/.test(part)) return `第 ${Number(part) + 1} 项`
      return Object.hasOwn(revisionFieldLabels, part) ? `${revisionFieldLabels[part]}（${part}）` : part
    }).join(' · ')
    return `${label || '修订内容'}：${message || '违反世界规则。'}`
  })
  return [...new Set(errors)]
}

function PersonalityComponentFields({ draft, originalValue, onChange }: {
  draft: unknown
  originalValue: unknown
  onChange: (value: unknown) => void
}) {
  const id = useId()
  const record = asRecord(draft)
  const originalLocks = asRecord(originalValue).locked_fields
  const committedLocks = Array.isArray(originalLocks) ? originalLocks : []
  const draftLocks = Array.isArray(record.locked_fields) ? record.locked_fields : []

  return <div className="author-personality-form">
    <p id={`${id}-help`} className="author-revision-help">要修改已锁定的字段，请先取消勾选并提交解锁，再发起修订。</p>
    <div className="author-trait-grid">
      {personalityFields.map(([key, label]) => {
        const inputId = `${id}-${key}`
        const isList = key === 'traits' || key === 'values'
        const committed = committedLocks.includes(key)
        const checked = draftLocks.includes(key)

        return <div className={isList ? 'author-personality-field author-wide-field' : 'author-personality-field'} key={key}>
          <div className="author-personality-field-heading">
            <label htmlFor={inputId}>{label}</label>
            <label className="author-personality-lock">
              <input type="checkbox" aria-label={`锁定${label}`} checked={checked} onChange={(event) => {
                const lockedFields = event.target.checked ? [...draftLocks, key] : draftLocks.filter((field) => field !== key)
                onChange({ ...record, locked_fields: lockedFields })
              }} />
              <span>{committed && !checked ? '解锁待提交' : '锁定'}</span>
            </label>
          </div>
          {isList
            ? <input id={inputId} defaultValue={stringList(record[key])} disabled={committed} aria-describedby={committed ? `${id}-help` : undefined}
              onChange={(event) => onChange({ ...record, [key]: parseList(event.target.value.replaceAll('，', ',')) })}
              placeholder={key === 'traits' ? '谨慎，重诺，直率' : '自由，家族，求知'} />
            : <input id={inputId} type="number" min="0" max="1" step="0.01" value={String(record[key] ?? '0.5')} disabled={committed} aria-describedby={committed ? `${id}-help` : undefined}
              onChange={(event) => onChange({ ...record, [key]: event.target.value })} />}
          <small>{isList ? '用逗号分隔' : '0–1'}</small>
        </div>
      })}
    </div>
  </div>
}

function SimpleComponentFields({ name, draft, onChange, referenceEntities, daySeconds }: { name: string; draft: unknown; onChange: (value: unknown) => void; referenceEntities: RevisionEntity[]; daySeconds: number }) {
  const record = asRecord(draft)
  if (textComponents[name]) {
    const field = textComponents[name]
    return <label className="author-simple-field" htmlFor="author-component-text"><span>{field.label}</span><textarea id="author-component-text" value={typeof draft === 'string' ? draft : ''} onChange={(event) => onChange(event.target.value)} maxLength={field.maxLength} rows={4} /></label>
  }
  if (name === 'attributes') {
    const values = asRecord(record.values)
    const updateValues = (next: Record<string, unknown>) => onChange({ ...record, values: next })
    return <div className="author-attributes-form"><p className="author-revision-help">自定义键值会按文本保存；精确数值可写成如 12.75 的十进制字符串。</p>{Object.entries(values).map(([key, value]) => <div className="author-attribute-row" key={key}><label><span>{key}</span><input value={String(value ?? '')} aria-label={`${key} 的值`} onChange={(event) => updateValues({ ...values, [key]: event.target.value })} /></label><button className="icon-button" type="button" aria-label={`删除 ${key}`} onClick={() => { const next = { ...values }; delete next[key]; updateValues(next) }}><Icon name="close" size={14} /></button></div>)}<AttributeAdder onAdd={(key) => updateValues({ ...values, [key]: '' })} /></div>
  }
  if (name === 'health') return <div className="author-trait-grid"><label><span>当前生命值</span><input type="number" min="0" value={String(record.current ?? '')} onChange={(event) => onChange({ ...record, current: event.target.value })} /></label><label><span>生命上限</span><input type="number" min="1" value={String(record.maximum ?? '')} onChange={(event) => onChange({ ...record, maximum: event.target.value })} /></label><label className="author-wide-field"><span>状况标签 <small>用逗号分隔</small></span><input value={stringList(record.conditions)} onChange={(event) => onChange({ ...record, conditions: parseList(event.target.value) })} placeholder="轻伤，疲惫" /></label><p className="author-revision-help author-wide-field">已有伤势记录保留不变；复杂医疗记录可用高级 JSON 编辑。</p></div>
  if (name === 'capabilities') return <>
    <label className="author-toggle-field"><span>战斗保命</span><input id="author-revision-draft" type="checkbox" checked={record.combat_death_protection === true} aria-describedby="author-combat-protection-help" onChange={(event) => onChange({ ...record, combat_death_protection: event.target.checked })} /></label>
    <p id="author-combat-protection-help" className="author-revision-help">仅在战斗中生效。开启后，人物受到致命攻击时会重伤倒下，治疗导致倒下的那处伤势后才能恢复行动。</p>
  </>
  if (name === 'progression') return <div className="author-trait-grid"><label><span>等级</span><input type="number" min="1" value={String(record.level ?? '')} onChange={(event) => onChange({ ...record, level: event.target.value })} /></label><label><span>经验</span><input type="number" min="0" value={String(record.experience ?? '')} onChange={(event) => onChange({ ...record, experience: event.target.value })} /></label><label className="author-wide-field"><span>成长里程碑 <small>用逗号分隔</small></span><input value={stringList(record.milestones)} onChange={(event) => onChange({ ...record, milestones: parseList(event.target.value) })} /></label></div>
  if (name === 'hidden') return <label className="author-toggle-field"><span>作者隐藏标记</span><input type="checkbox" checked={draft === true} onChange={(event) => onChange(event.target.checked)} /></label>
  if (name === 'daily_routine') return <RoutineComponentFields record={record} entities={referenceEntities} daySeconds={daySeconds} onChange={onChange} />
  if (name === 'place') return <PlaceComponentFields record={record} entities={referenceEntities} onChange={(value) => onChange(value)} />
  if (name === 'governance') return <GovernanceComponentFields record={record} entities={referenceEntities} onChange={(value) => onChange(value)} />
  if (name === 'population') return <PopulationSeatPolicyFields record={record} onChange={onChange} />
  return null
}

type RoutineWindowDraft = { window_id?: string; start_second?: number | string; duration_seconds?: number | string; activity?: string; place_id?: string }
const routineActivities: Record<string, string> = { rest: '休息', work: '工作', study: '学习', socialize: '社交' }

function RoutineComponentFields({ record, entities, daySeconds, onChange }: { record: Record<string, unknown>; entities: RevisionEntity[]; daySeconds: number; onChange: (value: unknown) => void }) {
  const windows: RoutineWindowDraft[] = Array.isArray(record.windows) ? record.windows.map((item) => asRecord(item) as RoutineWindowDraft) : []
  const places = entities.filter((entity) => isPlaceKind(entity.kind))
  const updateWindow = (index: number, patch: Partial<RoutineWindowDraft>) => {
    const next = [...windows]
    next[index] = { ...next[index], ...patch }
    onChange({ ...record, windows: next })
  }
  const removeWindow = (index: number) => onChange({ ...record, windows: windows.filter((_, itemIndex) => itemIndex !== index) })
  const addWindow = () => {
    const windowId = globalThis.crypto?.randomUUID?.()
    if (!windowId) return
    onChange({ ...record, windows: [...windows, { window_id: windowId, start_second: 0, duration_seconds: Math.min(3600, daySeconds), activity: 'rest', place_id: places[0]?.id ?? '' }] })
  }
  return <div className="author-routine-form">
    <p className="author-revision-help">按世界历法的秒数设置每日时段。时段必须位于同一天且不可重叠。可在「作息出行」中设置 NPC 前往时段地点的条件。</p>
    {windows.length ? <div className="author-routine-list">{windows.map((window, index) => {
      const prefix = `作息时段 ${index + 1}`
      return <fieldset className="author-routine-window" key={window.window_id ?? `window-${index}`}>
        <legend>{prefix}</legend>
        <div className="author-routine-fields">
          <label><span>活动</span><select value={window.activity ?? 'rest'} onChange={(event) => updateWindow(index, { activity: event.target.value })} aria-label={`${prefix} 活动`}>{Object.entries(routineActivities).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label><span>开始秒 <small>0–{Math.max(0, daySeconds - 1)}</small></span><input type="number" min="0" max={Math.max(0, daySeconds - 1)} step="1" value={String(window.start_second ?? '')} onChange={(event) => updateWindow(index, { start_second: event.target.value === '' ? '' : Number(event.target.value) })} aria-label={`${prefix} 开始秒`} /></label>
          <label><span>持续秒</span><input type="number" min="1" max={daySeconds} step="1" value={String(window.duration_seconds ?? '')} onChange={(event) => updateWindow(index, { duration_seconds: event.target.value === '' ? '' : Number(event.target.value) })} aria-label={`${prefix} 持续秒`} /></label>
          <label className="author-routine-place"><span>地点</span><select required value={window.place_id ?? ''} onChange={(event) => updateWindow(index, { place_id: event.target.value })} aria-label={`${prefix} 地点`}><option value="" disabled>选择作者投影中的地点</option>{places.map((place) => <option key={place.id} value={place.id}>{place.name || '未命名地点'} · {place.kind}</option>)}{window.place_id && !places.some((place) => place.id === window.place_id) && <option value={window.place_id}>{window.place_id} · 当前引用不在投影中</option>}</select></label>
          <button className="icon-button author-routine-remove" type="button" aria-label={`删除${prefix}`} onClick={() => removeWindow(index)}><Icon name="close" size={14} /></button>
        </div>
      </fieldset>
    })}</div> : <p className="author-routine-empty">尚未设置每日时段。</p>}
    <button type="button" className="button button-secondary button-small" onClick={addWindow} disabled={!places.length || windows.length >= 64}><Icon name="plus" size={13} />添加每日时段</button>
    {!places.length && <p className="author-revision-help">作者投影中没有可选地点，请先创建或载入地点。</p>}
  </div>
}

function RoutineMobilityComponentFields({ record, routine, entities, onChange }: {
  record: Record<string, unknown>
  routine: Record<string, unknown>
  entities: RevisionEntity[]
  onChange: (value: unknown) => void
}) {
  const windows = Array.isArray(routine.windows) ? routine.windows.map(asRecord) : []
  const alternatives = asRecord(record.alternatives)
  const places = entities.filter((entity) => isPlaceKind(entity.kind))
  const setAlternatives = (windowId: string, placeIds: string[]) => {
    const next = { ...alternatives }
    if (placeIds.length) next[windowId] = placeIds
    else delete next[windowId]
    onChange({ ...record, alternatives: next })
  }

  return <div className="author-routine-form">
    <label className="author-toggle-field"><span>启用作息出行</span><input id="author-revision-draft" type="checkbox" checked={record.enabled === true} aria-describedby="author-routine-mobility-help" onChange={(event) => onChange({ ...record, enabled: event.target.checked })} /></label>
    <p id="author-routine-mobility-help" className="author-revision-help">推进世界时，系统会尝试让 NPC 沿可通行的直达路线准时抵达。玩家角色由玩家自行行动。</p>
    <label className="author-simple-field">
      <span>避开的天气标签 <small>用逗号分隔</small></span>
      <input defaultValue={stringList(record.avoid_conditions)} placeholder="暴雨，暴雪" onChange={(event) => onChange({ ...record, avoid_conditions: parseList(event.target.value.replaceAll('，', ',')) })} />
      <small>匹配目的地的天气标签。目的地未设置时，使用所属区域的标签。</small>
    </label>
    <p className="author-revision-help">首选地点无法按时抵达时，按以下顺序尝试备选地点。删除作息时段前，请先清除该时段的备选地点并提交。</p>
    {windows.length === 0 && <p className="author-routine-empty">请先在「每日作息」中设置并提交时段。</p>}
    <div className="author-routine-list">{windows.map((window) => {
      const windowId = nullableId(window.window_id)
      if (!windowId) return null
      const activity = typeof window.activity === 'string' ? routineActivities[window.activity] : undefined
      const title = `${activity ?? '作息'} · 第 ${String(window.start_second ?? '—')} 秒`
      const primary = places.find((place) => place.id === window.place_id)
      const selected = idList(alternatives[windowId])
      const available = places.filter((place) => place.status !== 'dead' && place.status !== 'retired' && place.id !== window.place_id && !selected.includes(place.id))
      const move = (index: number, offset: number) => {
        const next = [...selected]
        const [placeId] = next.splice(index, 1)
        next.splice(index + offset, 0, placeId)
        setAlternatives(windowId, next)
      }
      return <fieldset className="author-routine-window" key={windowId}>
        <legend>{title}</legend>
        <p className="author-revision-help">首选地点 · {primary?.name || primary?.id || String(window.place_id ?? '—')}</p>
        {selected.map((placeId, index) => {
          const placeName = places.find((place) => place.id === placeId)?.name || placeId
          return <div className="author-attribute-row" key={placeId}>
            <div className="author-simple-field"><span>备选地点 {index + 1} · {placeName}</span></div>
            <button type="button" className="button button-secondary button-small" disabled={index === 0} aria-label={`${title} · 上移 ${placeName}`} onClick={() => move(index, -1)}>上移</button>
            <button type="button" className="button button-secondary button-small" disabled={index === selected.length - 1} aria-label={`${title} · 下移 ${placeName}`} onClick={() => move(index, 1)}>下移</button>
            <button type="button" className="icon-button" aria-label={`${title} · 移除 ${placeName}`} onClick={() => setAlternatives(windowId, selected.filter((_, position) => position !== index))}><Icon name="close" size={14} /></button>
          </div>
        })}
        {selected.length < 8 && available.length > 0 && <label className="author-simple-field">
          <span>添加备选地点</span>
          <select value="" aria-label={`${title} · 添加备选地点`} onChange={(event) => {
            if (event.target.value) setAlternatives(windowId, [...selected, event.target.value])
          }}>
            <option value="">选择地点</option>
            {available.map((place) => <option key={place.id} value={place.id}>{place.name || place.id}</option>)}
          </select>
        </label>}
      </fieldset>
    })}</div>
  </div>
}

function routineWeatherValidationError(draft: unknown): string {
  const tags = idList(asRecord(draft).avoid_conditions)
  if (tags.length > 16) return '避开天气最多可设置 16 项。'
  if (tags.some((tag) => Array.from(tag).length > 160)) return '每项天气标签最多 160 字。'
  return ''
}

function routineValidationError(value: unknown, daySeconds: number): string {
  const record = asRecord(value)
  const windows = Array.isArray(record.windows) ? record.windows.map(asRecord) : []
  if (windows.length > 64) return '每日时段最多 64 项。'
  const ids = windows.map((window) => window.window_id).filter((id): id is string => typeof id === 'string')
  if (ids.length !== new Set(ids).size) return '每日时段 ID 必须唯一。'
  const sorted = windows.map((window, index) => ({
    index,
    start: Number(window.start_second),
    duration: Number(window.duration_seconds),
    emptyTime: window.start_second === '' || window.duration_seconds === '',
    activity: window.activity,
    place: window.place_id,
  })).sort((left, right) => left.start - right.start)
  for (const window of sorted) {
    if (window.emptyTime) return `时段 ${window.index + 1} 的开始秒和持续时间不能为空。`
    if (!Number.isSafeInteger(window.start) || window.start < 0 || window.start >= daySeconds) return `时段 ${window.index + 1} 的开始秒必须位于当日范围内。`
    if (!Number.isSafeInteger(window.duration) || window.duration < 1 || window.start + window.duration > daySeconds) return `时段 ${window.index + 1} 的持续时间必须大于 0，且不可跨日。`
    if (typeof window.activity !== 'string' || !Object.prototype.hasOwnProperty.call(routineActivities, window.activity)) return `时段 ${window.index + 1} 的活动无效。`
    if (typeof window.place !== 'string' || !window.place) return `时段 ${window.index + 1} 需要选择一个地点。`
  }
  for (let index = 1; index < sorted.length; index += 1) {
    if (sorted[index - 1].start + sorted[index - 1].duration > sorted[index].start) return '每日时段不可重叠。'
  }
  return ''
}

const tickMinimum = -(1n << 63n)
const tickMaximum = (1n << 63n) - 1n
const waypointUuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function parseOpportunityTick(value: unknown): bigint | null {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) return null
    return BigInt(value)
  }
  if (typeof value !== 'string' || !/^-?\d+$/.test(value.trim())) return null
  try {
    const tick = BigInt(value.trim())
    return tick >= tickMinimum && tick <= tickMaximum ? tick : null
  } catch {
    return null
  }
}

function optionalTickText(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function waypointOpportunityValidationError(value: unknown): string {
  const record = asRecord(value)
  if (record.waypoint_opportunities === undefined) return ''
  if (!Array.isArray(record.waypoint_opportunities)) return '行程停留机会必须是列表。'
  const opportunities = record.waypoint_opportunities.map(asRecord)
  if (opportunities.length > 64) return '行程停留机会最多 64 项。'
  const ids = opportunities.map((opportunity) => opportunity.opportunity_id)
  if (ids.some((id) => typeof id !== 'string' || !waypointUuidPattern.test(id))) return '每项行程停留机会都需要有效的 UUID。'
  if (new Set(ids).size !== ids.length) return '行程停留机会 UUID 必须唯一。'

  for (const [index, opportunity] of opportunities.entries()) {
    const title = opportunity.title
    if (typeof title !== 'string' || !title.trim()) return `停留机会 ${index + 1} 需要填写标题。`
    if (title.length > 160) return `停留机会 ${index + 1} 的标题最多 160 字。`
    const summary = opportunity.summary
    if (typeof summary !== 'string' || !summary.trim()) return `停留机会 ${index + 1} 需要填写说明。`
    if (summary.length > 1000) return `停留机会 ${index + 1} 的说明最多 1000 字。`

    const minimumElapsed = parseOpportunityTick(opportunity.minimum_itinerary_elapsed_ticks === undefined ? 0 : opportunity.minimum_itinerary_elapsed_ticks)
    if (minimumElapsed === null || minimumElapsed < 0n) return `停留机会 ${index + 1} 的最低已行程 tick 必须是 0 到 2⁶³−1 之间的整数。`
    const opensText = optionalTickText(opportunity.opens_tick).trim()
    const closesText = optionalTickText(opportunity.closes_tick).trim()
    const hasOpensTick = opportunity.opens_tick !== undefined && opportunity.opens_tick !== null && !(typeof opportunity.opens_tick === 'string' && opensText === '')
    const hasClosesTick = opportunity.closes_tick !== undefined && opportunity.closes_tick !== null && !(typeof opportunity.closes_tick === 'string' && closesText === '')
    const opensTick = hasOpensTick ? parseOpportunityTick(opportunity.opens_tick) : null
    const closesTick = hasClosesTick ? parseOpportunityTick(opportunity.closes_tick) : null
    if (hasOpensTick && opensTick === null) return `停留机会 ${index + 1} 的开放 tick 必须是有符号 64 位整数。`
    if (hasClosesTick && closesTick === null) return `停留机会 ${index + 1} 的关闭 tick 必须是有符号 64 位整数。`
    if (opensTick !== null && closesTick !== null && opensTick >= closesTick) return `停留机会 ${index + 1} 的开放 tick 必须早于关闭 tick。`
  }
  return ''
}

function getAvailableComponents(entity: RevisionEntity | undefined, isNpc: boolean): string[] {
  const names = Object.keys(entity?.components ?? {}).filter((name) => componentLabels[name] && (name !== 'routine_mobility' || isNpc))
  if (entity && isCharacterKind(entity.kind) && !names.includes('daily_routine')) names.push('daily_routine')
  if (isNpc && !names.includes('capabilities')) names.push('capabilities')
  if (isNpc && !names.includes('routine_mobility')) names.push('routine_mobility')
  return names
}

function PlaceComponentFields({ record, entities, onChange }: { record: Record<string, unknown>; entities: RevisionEntity[]; onChange: (value: Record<string, unknown>) => void }) {
  const placeEntities = entities.filter((entity) => isPlaceKind(entity.kind))
  const regions = placeEntities.filter((entity) => asRecord(entity.components?.place).place_type === 'region')
  const regionValue = nullableId(record.region_id)
  const parentValue = nullableId(record.parent_id)
  const update = (key: string, value: unknown) => onChange({ ...record, [key]: value })
  const weather = asRecord(record.environment).weather
  const updateWeather = (value: string) => {
    const environment = { ...asRecord(record.environment) }
    if (value.trim()) environment.weather = value.trim()
    else delete environment.weather
    update('environment', environment)
  }
  const opportunities = Array.isArray(record.waypoint_opportunities) ? record.waypoint_opportunities.map(asRecord) : []
  const canGenerateOpportunityId = typeof globalThis.crypto?.randomUUID === 'function'
  const updateOpportunity = (index: number, key: string, value: unknown) => {
    const next = [...opportunities]
    const opportunity = { ...next[index] }
    if ((key === 'opens_tick' || key === 'closes_tick') && value === '') delete opportunity[key]
    else opportunity[key] = value
    next[index] = opportunity
    update('waypoint_opportunities', next)
  }
  const removeOpportunity = (index: number) => update('waypoint_opportunities', opportunities.filter((_, itemIndex) => itemIndex !== index))
  const addOpportunity = () => {
    const opportunityId = globalThis.crypto?.randomUUID?.()
    if (!opportunityId) return
    update('waypoint_opportunities', [...opportunities, {
      opportunity_id: opportunityId,
      title: '',
      summary: '',
      minimum_itinerary_elapsed_ticks: '0',
    }])
  }
  return <div className="author-schema-form">
    <div className="author-trait-grid">
      <label><span>地点类型</span><select value={String(record.place_type ?? 'site')} onChange={(event) => update('place_type', event.target.value)}><option value="region">区域</option><option value="settlement">聚落</option><option value="building">建筑</option><option value="wilderness">荒野</option><option value="site">地点</option><option value="other">其他</option></select></label>
      <label><span>通行策略</span><select value={String(record.access_policy ?? 'open')} onChange={(event) => update('access_policy', event.target.value)}><option value="open">开放</option><option value="restricted">受限</option><option value="private">私有</option></select></label>
    </div>
    <EntityReferenceSelect label="所属区域" value={regionValue} entities={regions} preservedEntities={placeEntities} onChange={(value) => update('region_id', value)} emptyLabel="不指定区域" />
    <EntityReferenceSelect label="上级地点" value={parentValue} entities={placeEntities} onChange={(value) => update('parent_id', value)} emptyLabel="不指定上级地点" />
    <label className="author-simple-field"><span>天气标签 <small>可留空</small></span><input defaultValue={typeof weather === 'string' ? weather : ''} maxLength={160} onChange={(event) => updateWeather(event.target.value)} placeholder="暴雨" /><small>用于作息出行。留空时使用所属区域的天气。</small></label>
    <div className="author-routine-form">
      <div className="author-field-heading"><strong>行程停留机会</strong><small>{opportunities.length} / 64</small></div>
      <p className="author-revision-help">机会只提供可选互动；角色实际到达地点并满足时间条件时才会询问。开放 tick 包含边界，关闭 tick 不包含边界；两者均可留空。</p>
      {opportunities.length ? <div className="author-routine-list">{opportunities.map((opportunity, index) => {
        const prefix = `停留机会 ${index + 1}`
        return <fieldset className="author-routine-window" key={`${String(opportunity.opportunity_id ?? 'missing')}-${index}`}>
          <legend>{prefix}</legend>
          <label className="author-simple-field"><span>标题</span><input type="text" maxLength={160} required value={typeof opportunity.title === 'string' ? opportunity.title : ''} onChange={(event) => updateOpportunity(index, 'title', event.target.value)} aria-label={`${prefix} 标题`} /></label>
          <label className="author-simple-field"><span>说明</span><textarea rows={2} maxLength={1000} required value={typeof opportunity.summary === 'string' ? opportunity.summary : ''} onChange={(event) => updateOpportunity(index, 'summary', event.target.value)} aria-label={`${prefix} 说明`} /></label>
          <div className="author-trait-grid">
            <label><span>开放 tick（含，可留空）</span><input type="text" inputMode="text" value={optionalTickText(opportunity.opens_tick)} onChange={(event) => updateOpportunity(index, 'opens_tick', event.target.value)} placeholder="不限制开始时间" aria-label={`${prefix} 开放 tick`} /></label>
            <label><span>关闭 tick（不含，可留空）</span><input type="text" inputMode="text" value={optionalTickText(opportunity.closes_tick)} onChange={(event) => updateOpportunity(index, 'closes_tick', event.target.value)} placeholder="不限制结束时间" aria-label={`${prefix} 关闭 tick`} /></label>
            <label><span>最低已行程时长（tick）</span><input type="text" inputMode="numeric" value={optionalTickText(opportunity.minimum_itinerary_elapsed_ticks ?? 0)} onChange={(event) => updateOpportunity(index, 'minimum_itinerary_elapsed_ticks', event.target.value)} aria-label={`${prefix} 最低已行程时长 tick`} /></label>
          </div>
          <button type="button" className="button button-secondary button-small author-routine-remove" onClick={() => removeOpportunity(index)} aria-label={`删除${prefix}`}><Icon name="close" size={13} />删除机会</button>
        </fieldset>
      })}</div> : <p className="author-routine-empty">尚未登记行程停留机会。</p>}
      <button type="button" className="button button-secondary button-small" onClick={addOpportunity} disabled={!canGenerateOpportunityId || opportunities.length >= 64}><Icon name="plus" size={13} />添加停留机会</button>
      {!canGenerateOpportunityId && <p className="author-revision-help">当前浏览器无法生成机会 UUID，暂时不能添加。</p>}
    </div>
    <p className="author-revision-help">地点引用仅从当前作者投影中选择；路线、环境、容量与地貌等未改字段会原样保留。高级 JSON 可编辑完整组件。</p>
  </div>
}

function PopulationSeatPolicyFields({ record, onChange }: { record: Record<string, unknown>; onChange: (value: unknown) => void }) {
  const seatPolicy = asRecord(record.seat_policy)
  const roles = Array.isArray(seatPolicy.extra_roles) ? seatPolicy.extra_roles.filter((role): role is string => typeof role === 'string') : []
  const profile = asRecord(seatPolicy.occupation_profile)
  const roleWeights = asRecord(profile.role_weights)
  const roleSkillLevels = asRecord(profile.role_skill_levels)
  const maintainCivicOccupations = seatPolicy.maintain_civic_occupations === true
  const registeredWeightRoles = Object.keys(roleWeights).filter((role) => roles.includes(role) && isSupportedPopulationRoleWeight(roleWeights[role]))
  const [newRole, setNewRole] = useState('')

  const updateProfile = (field: string, value: unknown) => onChange({
    ...record,
    seat_policy: {
      ...seatPolicy,
      occupation_profile: { ...profile, [field]: value },
    },
  })
  const setRoleWeight = (role: string, weight: number) => updateProfile('role_weights', { ...roleWeights, [role]: weight })
  const addRole = () => {
    const role = newRole.trim()
    if (!isBoundedPopulationRole(role) || roles.includes(role) || roles.length >= 10_000) return
    onChange({
      ...record,
      seat_policy: {
        ...seatPolicy,
        extra_roles: [...roles, role],
        occupation_profile: { ...profile, role_weights: { ...roleWeights, [role]: 1 } },
      },
    })
    setNewRole('')
  }
  const removeRole = (role: string) => {
    const nextWeights = { ...roleWeights }
    delete nextWeights[role]
    onChange({
      ...record,
      seat_policy: {
        ...seatPolicy,
        extra_roles: roles.filter((item) => item !== role),
        occupation_profile: { ...profile, role_weights: nextWeights },
      },
    })
  }
  const addRoleIsValid = isBoundedPopulationRole(newRole.trim())
    && !roles.includes(newRole.trim())
    && roles.length < 10_000

  return <div className="author-schema-form">
    <div className="author-routine-form">
      <div className="author-field-heading"><strong>未来人口晋升职业</strong><small>{roles.length} 项</small></div>
      <p className="author-revision-help">仅编辑未来晋升可采用的额外职业与权重。权重为 1–1000 的正整数，未单独登记的职业按 1 计算。人口数、年龄、容量及既有技能等级都会保留。</p>
      {roles.length ? <div className="author-routine-list">{roles.map((role, index) => {
        const hasRegisteredWeight = Object.prototype.hasOwnProperty.call(roleWeights, role)
        const hasSkillLevels = Object.prototype.hasOwnProperty.call(roleSkillLevels, role)
        const weightValue = isSupportedPopulationRoleWeight(roleWeights[role]) ? String(roleWeights[role]) : '1'
        const wouldRemoveFinalWeight = maintainCivicOccupations && hasRegisteredWeight && registeredWeightRoles.length <= 1
        const canRemove = roles.length > 1 && !hasSkillLevels && !wouldRemoveFinalWeight
        return <fieldset className="author-routine-window" key={`${role}-${index}`}>
          <legend>职业 {index + 1}</legend>
          <div className="author-routine-fields">
            <label><span>职业名称</span><input type="text" readOnly value={role} aria-label={`职业 ${role}`} /></label>
            <label><span>晋升权重 <small>{hasRegisteredWeight ? '已登记' : '默认 1'}</small></span><input type="number" min="1" max="1000" step="1" inputMode="numeric" value={weightValue} onChange={(event) => {
              const weight = parseSupportedPopulationRoleWeight(event.target.value)
              if (weight !== null) setRoleWeight(role, weight)
            }} aria-label={`${role} 晋升权重`} /></label>
            {!hasRegisteredWeight && <button type="button" className="button button-secondary button-small" onClick={() => setRoleWeight(role, 1)}>登记权重 1</button>}
            <button type="button" className="button button-secondary button-small author-routine-remove" onClick={() => removeRole(role)} disabled={!canRemove} aria-label={`删除职业 ${role}`}><Icon name="close" size={13} />删除</button>
          </div>
          {hasSkillLevels && <p className="author-revision-help">此职业已有技能等级设定；本表单会保留该设定，并暂不允许删除职业名。</p>}
          {roles.length <= 1 && <p className="author-revision-help">人口席位政策至少需要保留一个额外职业。</p>}
          {wouldRemoveFinalWeight && <p className="author-revision-help">启用维持公民职业时至少要保留一项已登记权重；请先为其他职业登记权重。</p>}
        </fieldset>
      })}</div> : <p className="author-routine-empty">当前没有额外职业，请添加至少一个。</p>}
      <div className="author-attribute-add">
        <input value={newRole} maxLength={320} onChange={(event) => setNewRole(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addRole() } }} placeholder="新增职业名称" aria-label="新增职业名称" />
        <button type="button" className="button button-secondary button-small" onClick={addRole} disabled={!addRoleIsValid}><Icon name="plus" size={13} />添加职业</button>
      </div>
      <p className="author-revision-help">新增职业会先登记默认权重 1。权重越高，未来采用该职业的相对机会越大。</p>
    </div>

    <label className="author-toggle-field"><span>优先补充本地机构所需职业 <small>未来晋升时参考已有招募规则</small></span><input type="checkbox" checked={maintainCivicOccupations} disabled={!maintainCivicOccupations && registeredWeightRoles.length === 0} onChange={(event) => {
      if (event.target.checked && registeredWeightRoles.length === 0) return
      onChange({ ...record, seat_policy: { ...seatPolicy, maintain_civic_occupations: event.target.checked } })
    }} /></label>
    {!maintainCivicOccupations && registeredWeightRoles.length === 0 && <p className="author-revision-help">先登记至少一项职业权重，才能启用职业优先。</p>}
    {maintainCivicOccupations && registeredWeightRoles.length === 0 && <p className="author-revision-inline-warning" role="status">职业优先至少需要一项有效权重；请登记权重或关闭此选项。</p>}
    <p className="author-revision-help">只有已登记权重、且符合本地章程的职业才参与优先补充。设置影响未来人口晋升。入会仍须通过正式招募，公职另按章程处理。</p>
  </div>
}

function GovernanceComponentFields({ record, entities, onChange }: { record: Record<string, unknown>; entities: RevisionEntity[]; onChange: (value: Record<string, unknown>) => void }) {
  const members = idList(record.members)
  const offices = Array.isArray(record.offices) ? record.offices.map(asRecord) : []
  const people = entities.filter((entity) => isCharacterKind(entity.kind))
  const update = (key: string, value: unknown) => onChange({ ...record, [key]: value })
  const authorityTags = Array.isArray(record.authority_tags) ? record.authority_tags : []
  const supportsAutonomousVacancyBallot = record.succession_policy === 'election' || record.succession_policy === 'council'
  const autonomousVacancyBallotEnabled = authorityTags.includes(autonomousVacancyBallotAuthority)
  const updateAutonomousVacancyBallot = (enabled: boolean) => {
    const otherTags = authorityTags.filter((tag) => tag !== autonomousVacancyBallotAuthority)
    update('authority_tags', enabled ? [...otherTags, autonomousVacancyBallotAuthority] : otherTags)
  }
  const updateOffice = (index: number, value: Record<string, unknown>) => {
    const next = [...offices]
    next[index] = value
    update('offices', next)
  }
  return <div className="author-schema-form">
    <label className="author-simple-field"><span>章程</span><textarea value={String(record.charter ?? '')} maxLength={8000} rows={4} onChange={(event) => update('charter', event.target.value)} /></label>
    <EntityMultiSelect label="成员" values={members} entities={entities} onChange={(value) => update('members', value)} />
    <EntityReferenceSelect label="领导者" value={nullableId(record.leader_id)} entities={people} onChange={(value) => update('leader_id', value)} emptyLabel="未指定领导者" />
    <div className="author-trait-grid">
      <label><span>继任方式</span><select value={String(record.succession_policy ?? 'none')} onChange={(event) => update('succession_policy', event.target.value)}><option value="election">选举</option><option value="appointment">任命</option><option value="inheritance">继承</option><option value="council">议会</option><option value="none">未设定</option></select></label>
      <label><span>决策门槛</span><input type="number" min="0" max="1" step="0.01" value={String(record.decision_threshold ?? '0.5')} onChange={(event) => update('decision_threshold', event.target.value)} /></label>
    </div>
    {supportsAutonomousVacancyBallot && <div className="author-vacancy-ballot-setting">
      <label className="author-toggle-field"><span>空缺时由 NPC 按年复查并表决</span><input type="checkbox" checked={autonomousVacancyBallotEnabled} onChange={(event) => updateAutonomousVacancyBallot(event.target.checked)} /></label>
      <p className="author-revision-help">仅适用于选举或议会继任方式。没有已登记选民，或选民中包含玩家控制角色时，不会自动发起这项 NPC 复查与表决。设定仍须通过常规预览并提交。</p>
    </div>}
    {offices.map((office, index) => <div className="author-office-reference" key={String(office.office_id ?? index)}><div><strong>{String(office.title ?? office.office_id ?? `职务 ${index + 1}`)}</strong><small>职务任期与权限保持不变</small></div><EntityReferenceSelect label="任职者" value={nullableId(office.holder_id)} entities={people} onChange={(value) => updateOffice(index, { ...office, holder_id: value })} emptyLabel="空缺" /></div>)}
    <p className="author-revision-help">领导者与任职者必须属于成员。更改引用时，其余治理字段会保留；服务端预览会校验类型与引用关系。</p>
  </div>
}

function EntityReferenceSelect({ label, value, entities, preservedEntities = entities, onChange, emptyLabel }: { label: string; value: string | null; entities: RevisionEntity[]; preservedEntities?: RevisionEntity[]; onChange: (value: string | null) => void; emptyLabel: string }) {
  const choices = entities.some((entity) => entity.id === value) ? entities : value ? [...entities, ...preservedEntities.filter((entity) => entity.id === value)] : entities
  return <label className="author-simple-field"><span>{label}</span><select value={value ?? ''} onChange={(event) => onChange(event.target.value || null)}><option value="">{emptyLabel}</option>{choices.map((entity) => <option key={entity.id} value={entity.id}>{entity.name || '未命名实体'} · {entity.kind} · {entity.id}</option>)}{value && !choices.some((entity) => entity.id === value) && <option value={value}>{value} · 当前引用不在投影中</option>}</select></label>
}

function EntityMultiSelect({ label, values, entities, onChange }: { label: string; values: string[]; entities: RevisionEntity[]; onChange: (value: string[]) => void }) {
  const choices = entities
  const known = new Set(choices.map((entity) => entity.id))
  const unknown = values.filter((id) => !known.has(id))
  return <label className="author-simple-field"><span>{label} <small>可多选；未显示的现有 ID 会保留</small></span><select className="author-multi-select" multiple size={Math.min(7, Math.max(3, choices.length + unknown.length))} value={values} onChange={(event) => onChange(Array.from(event.currentTarget.selectedOptions, (option) => option.value))} aria-label={label}>{choices.map((entity) => <option key={entity.id} value={entity.id}>{entity.name || '未命名实体'} · {entity.kind} · {entity.id}</option>)}{unknown.map((id) => <option key={id} value={id}>{id} · 当前引用不在投影中</option>)}</select><small className="author-revision-help">Windows 使用 Ctrl 多选，触屏可直接点选。</small></label>
}

function governanceMembershipError(value: unknown): string {
  const record = asRecord(value)
  const members = new Set(idList(record.members))
  if (nullableId(record.leader_id) && !members.has(nullableId(record.leader_id) as string)) return '请先将领导者加入成员，或清空领导者。'
  const offices = Array.isArray(record.offices) ? record.offices.map(asRecord) : []
  if (offices.some((office) => nullableId(office.holder_id) && !members.has(nullableId(office.holder_id) as string))) return '每位职务任职者都必须属于成员；请先更新成员列表。'
  return ''
}

function isRecordObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isBoundedPopulationRole(value: string): boolean {
  const trimmed = value.trim()
  return trimmed.length > 0 && Array.from(trimmed).length <= 160
}

function parseSupportedPopulationRoleWeight(value: string): number | null {
  if (!/^\d+$/.test(value)) return null
  const weight = Number(value)
  return isSupportedPopulationRoleWeight(weight) ? weight : null
}

function isSupportedPopulationRoleWeight(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1 && value <= 1000
}

function populationSeatPolicyValidationError(value: unknown): string {
  const record = asRecord(value)
  if (record.seat_policy === undefined || record.seat_policy === null) return ''
  if (!isRecordObject(record.seat_policy)) return '人口席位政策格式无效。'
  const seatPolicy = record.seat_policy
  if (!Array.isArray(seatPolicy.extra_roles) || seatPolicy.extra_roles.length < 1 || seatPolicy.extra_roles.length > 10_000) return '人口席位政策至少需要一个额外职业，且最多 10000 项。'
  if (seatPolicy.extra_roles.some((role) => typeof role !== 'string' || !isBoundedPopulationRole(role))) return '额外职业名称不能为空，且最多 160 字。'
  const roles = seatPolicy.extra_roles as string[]
  const normalizedRoles = roles.map((role) => role.trim())
  if (new Set(normalizedRoles).size !== normalizedRoles.length) return '额外职业名称不能重复。'

  const rawProfile = seatPolicy.occupation_profile
  if (rawProfile !== undefined && rawProfile !== null && !isRecordObject(rawProfile)) return '职业配置格式无效。'
  const profile = isRecordObject(rawProfile) ? rawProfile : {}
  const rawWeights = profile.role_weights === undefined ? {} : profile.role_weights
  if (!isRecordObject(rawWeights)) return '职业权重必须是对象。'
  const weightEntries = Object.entries(rawWeights)
  if (weightEntries.length > 10_000) return '职业权重最多 10000 项。'
  for (const [role, weight] of weightEntries) {
    if (!roles.includes(role)) return `职业权重“${role}”必须对应一个额外职业。`
    if (!isSupportedPopulationRoleWeight(weight)) return `职业“${role}”的权重必须是 1 到 1000 的整数。`
  }

  const rawRoleSkills = profile.role_skill_levels === undefined ? {} : profile.role_skill_levels
  if (!isRecordObject(rawRoleSkills)) return '职业技能等级必须是对象。'
  const skillEntries = Object.entries(rawRoleSkills)
  if (skillEntries.length > 64) return '职业技能等级最多配置 64 个职业。'
  for (const [role, skills] of skillEntries) {
    if (!roles.includes(role)) return `职业“${role}”已有技能等级设置；请保留该职业名，或使用高级 JSON 同时调整两项。`
    if (!isRecordObject(skills) || Object.keys(skills).length > 32) return `职业“${role}”的技能等级配置格式无效。`
  }

  if (seatPolicy.maintain_civic_occupations !== undefined && typeof seatPolicy.maintain_civic_occupations !== 'boolean') return '维持地区公民职业必须使用开关值。'
  if (seatPolicy.maintain_civic_occupations === true && weightEntries.length === 0) return '维持地区公民职业至少需要一项有效职业权重。'
  return ''
}

function nullableId(value: unknown): string | null { return typeof value === 'string' && value ? value : null }
function idList(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [] }
function isPlaceKind(kind: string) { return kind === 'place' || kind === 'location' }
function isCharacterKind(kind: string) { return kind === 'character' || kind === 'person' }

function AttributeAdder({ onAdd }: { onAdd: (key: string) => void }) {
  const [key, setKey] = useState('')
  const add = () => {
    const value = key.trim()
    if (!value) return
    onAdd(value)
    setKey('')
  }
  return <div className="author-attribute-add"><input value={key} onChange={(event) => setKey(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); add() } }} placeholder="新数值名称" aria-label="新数值名称" /><button type="button" className="button button-secondary button-small" onClick={add} disabled={!key.trim()}><Icon name="plus" size={13} />添加数值</button></div>
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function cloneJsonValue(value: unknown): unknown {
  if (value === undefined) return undefined
  return JSON.parse(JSON.stringify(value)) as unknown
}

function stringList(value: unknown): string {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').join(', ') : ''
}

function parseList(value: string): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}

function prettyJson(value: unknown): string {
  const result = JSON.stringify(value, null, 2)
  return result === undefined ? 'null' : result
}
