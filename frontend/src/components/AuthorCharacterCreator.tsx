import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, type AuthorCharacterDraft, type AuthorCharacterPreview, type AuthorCharacterResult } from '../api/client'
import type { Notice } from '../App'
import { errorMessage } from '../utils'
import { Icon } from './Icon'
import { useModalKeyboard } from './useModalKeyboard'

type CharacterFormDraft = Omit<AuthorCharacterDraft, 'branch_id' | 'expected_state_version'>
type LocationChoice = { id: string; name?: string | null }
const MAX_CHARACTER_CARD_JSON_BYTES = 256 * 1024
const MAX_CHARACTER_CARD_PNG_BYTES = 8 * 1024 * 1024

export function AuthorCharacterCreator({
  worldId, branchId, stateVersion, locations, onClose, onRefreshProjection, onCreated, onNotice,
}: {
  worldId: string
  branchId: string
  stateVersion: string
  locations: LocationChoice[]
  onClose: () => void
  onRefreshProjection: () => void
  onCreated: (result: AuthorCharacterResult) => void
  onNotice: (notice: Notice) => void
}) {
  const [draft, setDraft] = useState<CharacterFormDraft>(() => ({
    name: '', location_id: locations[0]?.id ?? '', public_role: '', description: '',
    values: [], traits: [], secret: '', goal: '', review_count: 0, review_interval_days: 7,
  }))
  const [preview, setPreview] = useState<AuthorCharacterPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [refreshRequired, setRefreshRequired] = useState(false)
  const [focusNow, setFocusNow] = useState(0)
  const dialogRef = useRef<HTMLElement>(null)
  useModalKeyboard(dialogRef, {
    onClose,
    busy,
    focusKey: preview ? 'preview' : locations.length > 0 ? 'edit' : 'empty',
  })

  const previewExpired = Boolean(preview && Date.parse(preview.expires_at) <= focusNow)
  const reviewCount = draft.review_count ?? 0
  const reviewInterval = draft.review_interval_days ?? 7
  const validReview = reviewCount === 0 || Boolean(
    draft.goal.trim() && Number.isInteger(reviewCount) && reviewCount >= 1 && reviewCount <= 1_000_000
    && (reviewCount === 1 || (Number.isSafeInteger(reviewInterval) && reviewInterval >= 1)),
  )
  const validListSizes = draft.values.length <= 16 && draft.traits.length <= 16
    && [...draft.values, ...draft.traits].every((item) => item.length <= 120)
  const canPreview = Boolean(
    draft.name.trim() && draft.name.trim().length <= 120 && draft.location_id
    && draft.public_role.length <= 160 && draft.description.length <= 1000
    && draft.secret.length <= 1000 && draft.goal.length <= 1000 && validListSizes && validReview && !busy,
  )
  const validDestination = !draft.goal_destination_id || Boolean(draft.goal.trim()
    && draft.goal_destination_id !== draft.location_id
    && locations.some((location) => location.id === draft.goal_destination_id))

  useEffect(() => {
    if (!preview) return
    const timer = window.setInterval(() => setFocusNow(Date.now()), 10_000)
    return () => window.clearInterval(timer)
  }, [preview])

  const update = <K extends keyof CharacterFormDraft>(key: K, value: CharacterFormDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setError(null)
    setRefreshRequired(false)
  }

  const refreshProjection = () => {
    onClose()
    onRefreshProjection()
  }

  const makePreview = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canPreview || !validDestination || !locations.some((location) => location.id === draft.location_id)) return
    setBusy(true)
    setError(null)
    setRefreshRequired(false)
    try {
      const body: AuthorCharacterDraft = {
        branch_id: branchId,
        expected_state_version: stateVersion,
        ...draft,
        name: draft.name.trim(),
        public_role: draft.public_role.trim(),
        description: draft.description.trim(),
        values: draft.values.map((item) => item.trim()).filter(Boolean),
        traits: draft.traits.map((item) => item.trim()).filter(Boolean),
        secret: draft.secret.trim(),
        goal: draft.goal.trim(),
        review_interval_days: reviewCount > 1 ? reviewInterval : undefined,
      }
      const result = await api.previewAuthorCharacter(worldId, body)
      setPreview(result)
      setFocusNow(Date.now())
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally { setBusy(false) }
  }

  const importCard = async (file: File) => {
    if (busy || refreshRequired || !locations.some((location) => location.id === draft.location_id)) return
    if (file.size === 0) {
      setError('角色卡文件不能为空。')
      return
    }
    const fileName = file.name.toLowerCase()
    const fileType = file.type.toLowerCase()
    const isPng = fileType === 'image/png' || fileName.endsWith('.png')
    const isJson = fileType === 'application/json' || fileName.endsWith('.json')
    if (!isPng && !isJson) {
      setError('请选择 V1、V2 或 V3 JSON / PNG 角色卡。')
      return
    }
    const sizeLimit = isPng ? MAX_CHARACTER_CARD_PNG_BYTES : MAX_CHARACTER_CARD_JSON_BYTES
    if (file.size > sizeLimit) {
      setError(isPng ? 'PNG 角色卡不能超过 8 MiB。' : '角色卡 JSON 不能超过 256 KiB。')
      return
    }
    setBusy(true)
    setError(null)
    try {
      let result: AuthorCharacterPreview
      if (isPng) {
        result = await api.previewAuthorCharacterPng(worldId, file, {
          branch_id: branchId,
          expected_state_version: stateVersion,
          location_id: draft.location_id,
        })
      } else {
        const bytes = await file.arrayBuffer()
        let cardJson: string
        try {
          cardJson = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
        } catch {
          setError('无法读取角色卡。请选择 UTF-8 编码的 JSON 文件。')
          return
        }
        result = await api.previewAuthorCharacterCard(worldId, {
          branch_id: branchId,
          expected_state_version: stateVersion,
          location_id: draft.location_id,
          card_json: cardJson,
        })
      }
      setDraft(result.draft)
      setPreview(result)
      setFocusNow(Date.now())
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally { setBusy(false) }
  }

  const commit = async () => {
    if (!preview || busy || refreshRequired || previewExpired) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.commitAuthorCharacter(worldId, { preview_id: preview.preview_id })
      onNotice({ kind: 'success', text: `人物「${preview.draft.name}」已创建 · 状态版本 ${result.state_version} · 世界时间未推进` })
      onCreated(result)
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally { setBusy(false) }
  }

  const selectedLocation = locations.find((location) => location.id === (preview?.draft.location_id ?? draft.location_id))

  return <div className="dialog-backdrop character-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section ref={dialogRef} tabIndex={-1} className="create-dialog character-dialog" role="dialog" aria-modal="true" aria-labelledby="character-create-title" aria-describedby="character-create-description">
      <div className="dialog-topline"><span className="eyebrow"><span className="eyebrow-line" />作者工作区 · 新人物</span><button type="button" className="icon-button" onClick={onClose} aria-label="关闭创建人物窗口" disabled={busy}><Icon name="close" /></button></div>
      <div className="dialog-heading character-dialog-heading"><div className="dialog-glyph"><Icon name="users" size={22} /></div><h2 id="character-create-title">创建一位人物</h2><p id="character-create-description">先填写作者设定，服务端验证并生成预览后，再明确确认提交。</p></div>
      {locations.length === 0 ? <div className="character-no-locations" role="status"><Icon name="info" size={16} /><span>作者投影中没有可选地点。请先确保世界返回了地点实体，再创建人物。</span></div> : preview ? <div className="character-preview">
        <div className="character-preview-heading"><div><span className="panel-overline">SERVER VALIDATED · {preview.expected_version}</span><h3 tabIndex={-1} data-dialog-initial-focus>确认人物设定</h3></div><span className="no-tick-effect">时间不推进</span></div>
        <p className="character-preview-note">预览由服务器生成；确认后将创建实体并写入设定。人物秘密和目标属于作者资料。</p>
        <dl className="character-preview-fields">
          <PreviewField label="人物名称" value={preview.draft.name} />
          <PreviewField label="所在地点" value={selectedLocation?.name ?? `地点 ${preview.draft.location_id.slice(0, 8)}`} />
          <PreviewField label="公开身份" value={preview.draft.public_role} />
          <PreviewField label="公开描述" value={preview.draft.description} />
          <PreviewList label="价值观" items={preview.draft.values} />
          <PreviewList label="性格特征" items={preview.draft.traits} />
          <PreviewField label="作者秘密" value={preview.draft.secret} spoiler />
          <PreviewField label="角色目标" value={preview.draft.goal} spoiler />
          {preview.draft.goal_destination_id && <PreviewField label="目标达成条件" value={`亲自抵达${locations.find((loc) => loc.id === preview.draft.goal_destination_id)?.name || preview.draft.goal_destination_id}`} spoiler />}
          {preview.review_schedule && <PreviewField label="自主目标复审" value={`最多 ${preview.review_schedule.max_runs} 次，首次在一个世界日后${preview.review_schedule.repeat_ticks ? `，此后每隔 ${preview.draft.review_interval_days} 个世界日` : ''}`} spoiler />}
        </dl>
        {preview.import_report && <div className="character-preview-field">
          <strong>导入自角色卡 V{preview.import_report.card_version.split('.')[0]}</strong>
          {preview.import_report.unmapped_fields.length > 0 && <p>{preview.import_report.source_retained_on_commit ? '未映射字段' : '未导入字段'} {preview.import_report.unmapped_fields.join('、')}</p>}
          {preview.import_report.source_retained_on_commit === true && <p>确认后，原始角色卡数据将随作者备份保存。</p>}
        </div>}
        <div className="character-preview-meta"><span>检查点序号 <strong>{preview.checkpoint_sequence}</strong></span><span>预览到期 <strong>{formatPreviewExpiry(preview.expires_at)}</strong></span></div>
        {previewExpired && <p className="character-create-error" role="alert">此预览已过期，请返回草稿并重新生成预览。</p>}
        {refreshRequired && <div className="character-conflict" role="alert"><span>世界版本已变化，请重新读取作者投影后再试。</span><button type="button" className="text-button" onClick={refreshProjection} disabled={busy}>重新读取作者投影</button></div>}
        {error && <p className="character-create-error" role="alert">{error}</p>}
        <div className="character-preview-actions"><button type="button" className="button button-secondary" onClick={() => { setPreview(null); setError(null); setRefreshRequired(false) }} disabled={busy}>返回修改</button><button type="button" className="button button-primary" onClick={() => void commit()} disabled={busy || previewExpired || refreshRequired}>{busy ? <><Icon name="spinner" size={15} />提交中…</> : <>确认创建人物 <Icon name="check" size={15} /></>}</button></div>
      </div> : <form className="character-create-form" onSubmit={(event) => void makePreview(event)}>
        <div className="character-create-grid">
          <label className="character-create-field" htmlFor="character-create-name"><span>人物名称 <b>必填</b></span><input id="character-create-name" data-dialog-initial-focus value={draft.name} maxLength={120} onChange={(event) => update('name', event.target.value)} required /></label>
          <label className="character-create-field" htmlFor="character-create-location"><span>所在地点 <b>必填</b></span><select id="character-create-location" value={draft.location_id} onChange={(event) => update('location_id', event.target.value)} required>{locations.map((location) => <option key={location.id} value={location.id}>{location.name || location.id}</option>)}</select></label>
          <label className="character-create-field" htmlFor="character-create-role"><span>公开身份 <small>160 字以内</small></span><input id="character-create-role" value={draft.public_role} maxLength={160} onChange={(event) => update('public_role', event.target.value)} /></label>
          <label className="character-create-field" htmlFor="character-create-description"><span>公开描述 <small>1000 字以内</small></span><textarea id="character-create-description" value={draft.description} maxLength={1000} rows={3} onChange={(event) => update('description', event.target.value)} /></label>
          <StringListField id="character-create-values" label="价值观" hint="最多 16 项，每项 120 字" value={draft.values} onChange={(items) => update('values', items)} />
          <StringListField id="character-create-traits" label="性格特征" hint="最多 16 项，每项 120 字" value={draft.traits} onChange={(items) => update('traits', items)} />
          <label className="character-create-field character-create-wide" htmlFor="character-create-secret"><span>作者秘密 <small>仅作者设定 · 1000 字以内</small></span><textarea id="character-create-secret" value={draft.secret} maxLength={1000} rows={3} onChange={(event) => update('secret', event.target.value)} /></label>
          <label className="character-create-field character-create-wide" htmlFor="character-create-goal"><span>角色目标 <small>1000 字以内</small></span><textarea id="character-create-goal" value={draft.goal} maxLength={1000} rows={3} onChange={(event) => update('goal', event.target.value)} /></label>
          <label className="character-create-field" htmlFor="character-create-goal-destination">
            <span>目标目的地 <small>可选</small></span>
            <select id="character-create-goal-destination" value={draft.goal_destination_id ?? ''}
              onChange={(event) => update('goal_destination_id', event.target.value || undefined)}>
              <option value="">仅记录文字目标</option>
              {locations.filter((location) => location.id !== draft.location_id || location.id === draft.goal_destination_id).map((location) =>
                <option key={location.id} value={location.id} disabled={location.id === draft.location_id}>{location.name || location.id}</option>)}
            </select>
            {draft.goal_destination_id && <small>{validDestination ? '授予普通旅行能力。自主计划实际抵达后结算目标。' : '请填写目标，并选择与出发地点不同的有效目的地。'}</small>}
          </label>
          <label className="character-create-field"><span>自主目标复审次数 <small>0 表示不开启</small></span><input type="number" min={0} max={1_000_000} step={1} required value={Number.isNaN(reviewCount) ? '' : reviewCount} onChange={(event) => update('review_count', event.target.valueAsNumber)} /></label>
          {reviewCount > 1 && <label className="character-create-field"><span>复审间隔（世界日）</span><input type="number" min={1} step={1} required value={Number.isNaN(reviewInterval) ? '' : reviewInterval} onChange={(event) => update('review_interval_days', event.target.valueAsNumber)} /></label>}
          {reviewCount > 0 && !draft.goal.trim() && <small>开启自主复审前，请填写角色目标。</small>}
        </div>
        <label className="character-create-field character-create-wide" htmlFor="character-import-file">
          <span>从角色卡导入 <small>V1 / V2 / V3 JSON 或 PNG</small></span>
          <input id="character-import-file" type="file" accept=".json,.png,application/json,image/png" disabled={busy || refreshRequired || !draft.location_id}
            onChange={(event) => {
              const file = event.currentTarget.files?.[0]
              event.currentTarget.value = ''
              if (file) void importCard(file)
            }} />
          <small>JSON 最多 256 KiB，PNG 最多 8 MiB。按当前地点生成服务端预览；请确认简介适合公开。</small>
        </label>
        {error && <p className="character-create-error" role="alert">{error}</p>}
        {refreshRequired && <div className="character-conflict" role="alert"><span>分支版本已变化。重新读取作者投影后，再重新创建预览。</span><button type="button" className="text-button" onClick={refreshProjection} disabled={busy}>重新读取作者投影</button></div>}
        <div className="character-create-actions"><span>分支版本 {stateVersion} · 预览前不会写入世界</span><button className="button button-primary" type="submit" disabled={!canPreview || !validDestination || refreshRequired}>{busy ? <><Icon name="spinner" size={15} />正在验证…</> : <>生成服务端预览 <Icon name="arrow-right" size={15} /></>}</button></div>
      </form>}
      <div className="dialog-foot"><Icon name="eye" size={13} />作者资料可能包含玩家未知的秘密；只有服务端提交成功后人物才会出现。</div>
    </section>
  </div>
}

function StringListField({ id, label, hint, value, onChange }: { id: string; label: string; hint: string; value: string[]; onChange: (value: string[]) => void }) {
  const validItems = value.filter((item) => item.trim().length > 0)
  const addItem = () => { if (value.length < 16) onChange([...value, '']) }
  const setItem = (index: number, nextValue: string) => onChange(value.map((item, current) => current === index ? nextValue : item))
  const removeItem = (index: number) => onChange(value.filter((_, current) => current !== index))
  return <fieldset className="character-create-field character-list-field">
    <legend>{label}</legend>
    <span className="character-list-hint">{hint} · 已填写 {validItems.length}/16</span>
    {value.map((item, index) => <div className="character-list-row" key={`${id}-${index}`}>
      <input value={item} maxLength={120} aria-label={`${label}第 ${index + 1} 项`} onChange={(event) => setItem(index, event.target.value)} />
      <button type="button" className="icon-button" aria-label={`删除${label}第 ${index + 1} 项`} onClick={() => removeItem(index)}><Icon name="close" size={14} /></button>
    </div>)}
    <button type="button" className="text-button character-list-add" onClick={addItem} disabled={value.length >= 16}><Icon name="plus" size={13} />添加一项</button>
  </fieldset>
}

function PreviewField({ label, value, spoiler = false }: { label: string; value: string; spoiler?: boolean }) {
  return <div className="character-preview-field"><dt>{label}{spoiler && <span className="spoiler-mini">作者视角</span>}</dt><dd>{value || '未提供'}</dd></div>
}

function PreviewList({ label, items }: { label: string; items: string[] }) {
  return <div className="character-preview-field"><dt>{label}</dt><dd>{items.length ? items.map((item, index) => <span className="character-preview-tag" key={`${label}-${index}`}>{item}</span>) : '未提供'}</dd></div>
}

function formatPreviewExpiry(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-HK', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}
