import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  api,
  ApiError,
  type AuthorEntityPreview,
  type AuthorEntityResult,
  type AuthorGovernanceDraft,
  type AuthorOrganizationDraft,
  type AuthorPlaceDraft,
  type AuthorPlaceRouteDraft,
  type AuthorRenewableHarvestDraft,
} from '../api/client'
import type { Notice } from '../App'
import { errorMessage } from '../utils'
import { Icon } from './Icon'
import { useModalKeyboard } from './useModalKeyboard'

export type AuthorEntityKind = 'place' | 'organization'
export type EntityPlaceOption = { id: string; name?: string | null; place_type?: string }
export type EntityCharacterOption = { id: string; name: string; status?: string }
export type EntityAccountOption = { id: string; owner_id?: string; owner_name?: string; resource_id: string; account_type?: string }

type Props = {
  kind: AuthorEntityKind
  worldId: string
  branchId: string
  stateVersion: string
  places: EntityPlaceOption[]
  characters: EntityCharacterOption[]
  accounts: EntityAccountOption[]
  resources: string[]
  onClose: () => void
  onRefreshProjection: () => void
  onCreated: (kind: AuthorEntityKind, result: AuthorEntityResult) => void
  onNotice: (notice: Notice) => void
}

const placeTypeOptions = [
  ['region', '区域'], ['settlement', '聚落'], ['building', '建筑'],
  ['wilderness', '荒野'], ['site', '场所'], ['other', '其他'],
] as const
const accessOptions = [['open', '开放'], ['restricted', '受限'], ['private', '私有']] as const
const organizationKindOptions = [
  ['polity', '政治实体'], ['organization', '组织'], ['faction', '派系'],
  ['company', '公司'], ['government', '政府'],
] as const
const resourceIdPattern = /^[a-zA-Z][a-zA-Z0-9_.-]*$/
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const decimalPattern = /^(?:0|[1-9]\d*)(?:\.\d+)?$/

export function CreateEntityDialog({
  kind, worldId, branchId, stateVersion, places, characters, accounts, resources,
  onClose, onRefreshProjection, onCreated, onNotice,
}: Props) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const [placeType, setPlaceType] = useState<AuthorPlaceDraft['place_type']>('site')
  const [accessPolicy, setAccessPolicy] = useState<AuthorPlaceDraft['access_policy']>('open')
  const [regionId, setRegionId] = useState('')
  const [parentId, setParentId] = useState('')
  const [capacity, setCapacity] = useState('')
  const [terrain, setTerrain] = useState('')
  const [routesText, setRoutesText] = useState('[]')
  const [environmentText, setEnvironmentText] = useState('{}')
  const [harvestText, setHarvestText] = useState('[]')

  const [organizationKind, setOrganizationKind] = useState<AuthorOrganizationDraft['kind']>('organization')
  const [locationId, setLocationId] = useState('')
  const [publicRole, setPublicRole] = useState('')
  const [governanceText, setGovernanceText] = useState('')
  const [treasuryResources, setTreasuryResources] = useState<string[]>([])

  const [preview, setPreview] = useState<AuthorEntityPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [refreshRequired, setRefreshRequired] = useState(false)
  const [commitRetry, setCommitRetry] = useState(false)
  const [focusNow, setFocusNow] = useState(0)
  const dialogRef = useRef<HTMLElement>(null)
  useModalKeyboard(dialogRef, { onClose, busy, focusKey: preview ? 'preview' : 'edit' })

  const eligiblePlaces = useMemo(
    () => places.filter((place) => place.name?.trim() && place.name !== ''),
    [places],
  )
  const regions = useMemo(
    () => eligiblePlaces.filter((place) => place.place_type === 'region'),
    [eligiblePlaces],
  )
  const activeCharacters = useMemo(
    () => characters.filter((character) => character.status !== 'dead' && character.status !== 'retired'),
    [characters],
  )
  const availableResources = useMemo(() => [...new Set(resources)].sort(), [resources])
  const previewExpired = Boolean(preview && Date.parse(preview.expires_at) <= focusNow)
  const previewStale = Boolean(preview && preview.expected_version !== stateVersion)
  const trimmedName = name.trim()
  const formValid = trimmedName.length > 0 && trimmedName.length <= 240
    && description.length <= 4000 && publicRole.length <= 1000
    && (kind !== 'place' || capacity.trim() === '' || isPositiveIntegerText(capacity))
    && (kind !== 'organization' || (treasuryResources.length <= 128
      && treasuryResources.every((resource) => resource.trim().length > 0)))
    && !busy && !refreshRequired

  useEffect(() => {
    if (!preview) return
    const timer = window.setInterval(() => setFocusNow(Date.now()), 10_000)
    return () => window.clearInterval(timer)
  }, [preview])

  const clearError = () => {
    setError(null)
    setFieldErrors([])
    setRefreshRequired(false)
    setCommitRetry(false)
  }

  const refreshProjection = () => {
    setPreview(null)
    setRefreshRequired(false)
    onClose()
    onRefreshProjection()
  }

  const makePreview = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!formValid) return

    let placeDraft: AuthorPlaceDraft | null = null
    let organizationDraft: AuthorOrganizationDraft | null = null
    try {
      if (kind === 'place') {
        placeDraft = buildPlaceDraft({
          branchId, stateVersion, name: trimmedName, description, placeType, accessPolicy,
          regionId, parentId, capacity, terrain, routesText, environmentText, harvestText,
          places: eligiblePlaces,
        })
      } else {
        organizationDraft = buildOrganizationDraft({
          branchId, stateVersion, name: trimmedName, description, organizationKind,
          locationId, publicRole, governanceText, treasuryResources,
          places: eligiblePlaces, resources: availableResources,
        })
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '请修正草稿字段后再试。')
      setFieldErrors([])
      setRefreshRequired(false)
      setPreview(null)
      return
    }

    setBusy(true)
    setError(null)
    setFieldErrors([])
    setRefreshRequired(false)
    setCommitRetry(false)
    setPreview(null)
    try {
      const result = kind === 'place'
        ? await api.previewAuthorPlace(worldId, placeDraft!)
        : await api.previewAuthorOrganization(worldId, organizationDraft!)
      // This submit handler runs only after an explicit preview click, not during render.
      // oxlint-disable-next-line react/purity -- the static rule does not follow this event callback
      setFocusNow(Date.now())
      setPreview(result)
    } catch (reason) {
      showRequestError(reason)
    } finally {
      setBusy(false)
    }
  }

  const commitPreview = async () => {
    if (!preview || busy || refreshRequired || previewExpired || previewStale) return
    setBusy(true)
    setError(null)
    setFieldErrors([])
    setCommitRetry(false)
    try {
      const result = kind === 'place'
        ? await api.commitAuthorPlace(worldId, { preview_id: preview.preview_id })
        : await api.commitAuthorOrganization(worldId, { preview_id: preview.preview_id })
      const label = kind === 'place' ? '地点' : '势力或组织'
      const accountNote = result.created_accounts.length ? ` · 创建 ${result.created_accounts.length} 个零余额账户` : ''
      onNotice({ kind: 'success', text: `${label}「${preview.entity.name ?? name}」已创建 · 状态版本 ${result.state_version}${accountNote} · 世界时间未推进` })
      onCreated(kind, result)
    } catch (reason) {
      showRequestError(reason)
      setCommitRetry(!(reason instanceof ApiError) || reason.status >= 500 || reason.retryable === true)
    } finally {
      setBusy(false)
    }
  }

  const showRequestError = (reason: unknown) => {
    setError(errorMessage(reason))
    setFieldErrors(formatFieldErrors(reason))
    if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
  }

  const updateTreasuryResource = (index: number, value: string) => {
    setTreasuryResources((current) => current.map((item, itemIndex) => itemIndex === index ? value : item))
    clearError()
  }

  const selectedLocation = eligiblePlaces.find((place) => place.id === (preview?.draft && 'location_id' in preview.draft
    ? preview.draft.location_id
    : locationId))
  const isPlace = kind === 'place'
  const heading = isPlace ? '新地点' : '新势力或组织'

  return <div className="dialog-backdrop character-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section ref={dialogRef} tabIndex={-1} className="create-dialog character-dialog" role="dialog" aria-modal="true" aria-labelledby="entity-create-title" aria-describedby="entity-create-description">
      <div className="dialog-topline"><span className="eyebrow"><span className="eyebrow-line" />作者工作区 · {heading}</span><button type="button" className="icon-button" onClick={onClose} aria-label={`关闭${heading}窗口`} disabled={busy}><Icon name="close" /></button></div>
      <div className="dialog-heading character-dialog-heading"><div className="dialog-glyph"><Icon name={isPlace ? 'pin' : 'layers'} size={22} /></div><h2 id="entity-create-title">创建{heading}</h2><p id="entity-create-description">草稿只在本地编辑。点击生成预览后，服务端会验证完整实体和引用。</p></div>
      {preview ? <div className="character-preview">
        <div className="character-preview-heading"><div><span className="panel-overline">SERVER VALIDATED · {preview.expected_version}</span><h3 tabIndex={-1} data-dialog-initial-focus>确认创建定义</h3></div><span className="no-tick-effect">时间不推进</span></div>
        <p className="character-preview-note">预览由服务器验证并固定到当前分支版本。确认后提交这份预览；重试会继续使用同一个预览 ID。</p>
        <dl className="character-preview-fields">
          <PreviewField label="名称" value={preview.entity.name ?? trimmedName} />
          <PreviewField label="实体类型" value={preview.entity.kind} />
          <PreviewField label="实体 ID" value={preview.entity.id} />
          <PreviewField label="状态版本" value={preview.expected_version} />
          {preview.entity.location_id && <PreviewField label="所在地点" value={`${selectedLocation?.name ?? '已有地点'} · ${preview.entity.location_id}`} />}
          <PreviewField label="时间影响" value={preview.world_time_effect ? '会推进世界时间' : '不推进世界时间'} />
        </dl>
        <PreviewJson label="完整提交实体定义" value={preview.entity} />
        <PreviewJson label="完整作者草稿" value={preview.draft} />
        <section className="character-preview-field" aria-label="预览创建的资源账户">
          <dt>预览创建的账户</dt>
          {preview.created_accounts.length ? <pre className="entity-preview-json">{JSON.stringify(preview.created_accounts, null, 2)}</pre> : <dd>这份定义不会创建额外账户。</dd>}
        </section>
        <div className="character-preview-meta"><span>预览 ID <strong>{preview.preview_id}</strong></span><span>检查点序号 <strong>{preview.checkpoint_sequence}</strong></span><span>预览到期 <strong>{formatPreviewExpiry(preview.expires_at)}</strong></span></div>
        {previewExpired && <p className="character-create-error" role="alert">此预览已过期，请返回草稿并生成新的服务端预览。</p>}
        {(refreshRequired || previewStale) && <div className="character-conflict" role="alert"><span>分支版本已变化。刷新作者投影并重新检查这份定义后再预览。</span><button type="button" className="text-button" onClick={refreshProjection} disabled={busy}>刷新作者投影</button></div>}
        {error && <RequestError error={error} fieldErrors={fieldErrors} />}
        <div className="character-preview-actions">
          <button type="button" className="button button-secondary" onClick={() => { setPreview(null); clearError() }} disabled={busy}>返回修改</button>
          {commitRetry && !refreshRequired && !previewStale
            ? <button type="button" className="button button-primary" onClick={() => void commitPreview()} disabled={busy || previewExpired}>{busy ? <><Icon name="spinner" size={15} />重试提交中…</> : <>使用相同预览重试 <Icon name="refresh" size={15} /></>}</button>
            : <button type="button" className="button button-primary" onClick={() => void commitPreview()} disabled={busy || previewExpired || refreshRequired || previewStale}>{busy ? <><Icon name="spinner" size={15} />正在提交…</> : <>确认创建 <Icon name="check" size={15} /></>}</button>}
        </div>
      </div> : <form className="character-create-form" onSubmit={(event) => void makePreview(event)}>
        <div className="character-create-grid">
          <label className="character-create-field" htmlFor="author-entity-name"><span>名称 <b>必填</b></span><input id="author-entity-name" data-dialog-initial-focus value={name} maxLength={240} onChange={(event) => { setName(event.target.value); clearError() }} required /></label>
          {isPlace ? <>
            <label className="character-create-field" htmlFor="author-place-type"><span>地点类型 <b>必填</b></span><select id="author-place-type" value={placeType} onChange={(event) => { setPlaceType(event.target.value as AuthorPlaceDraft['place_type']); clearError() }}>{placeTypeOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label className="character-create-field" htmlFor="author-place-access"><span>通行策略 <b>必填</b></span><select id="author-place-access" value={accessPolicy} onChange={(event) => { setAccessPolicy(event.target.value as AuthorPlaceDraft['access_policy']); clearError() }}>{accessOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label className="character-create-field" htmlFor="author-place-region"><span>所属区域 <small>从现有区域选择</small></span><select id="author-place-region" value={regionId} onChange={(event) => { setRegionId(event.target.value); clearError() }}><option value="">不指定</option>{regions.map((place) => <option key={place.id} value={place.id}>{place.name} · {place.id}</option>)}</select></label>
            <label className="character-create-field" htmlFor="author-place-parent"><span>上级地点 <small>可选</small></span><select id="author-place-parent" value={parentId} onChange={(event) => { setParentId(event.target.value); clearError() }}><option value="">不指定</option>{eligiblePlaces.map((place) => <option key={place.id} value={place.id}>{place.name} · {place.id}</option>)}</select></label>
            <label className="character-create-field" htmlFor="author-place-capacity"><span>容量 <small>正整数；留空表示未设</small></span><input id="author-place-capacity" type="number" min="1" step="1" value={capacity} onChange={(event) => { setCapacity(event.target.value); clearError() }} /></label>
            <label className="character-create-field" htmlFor="author-place-terrain"><span>地貌 <small>资源标识；可选</small></span><input id="author-place-terrain" value={terrain} maxLength={160} onChange={(event) => { setTerrain(event.target.value); clearError() }} placeholder="terrain.coastal" /></label>
            <label className="character-create-field character-create-wide" htmlFor="author-place-description"><span>公开描述 <small>4000 字以内</small></span><textarea id="author-place-description" value={description} maxLength={4000} rows={3} onChange={(event) => { setDescription(event.target.value); clearError() }} /></label>
            <JsonField id="author-place-routes" label="路线 JSON" hint="每条路线填写目标地点 ID、travel_ticks、risk 和 is_open；route_id 与起点由服务端生成。" value={routesText} onChange={(value) => { setRoutesText(value); clearError() }} example={'[{"to_place_id":"现有地点 UUID","travel_ticks":3600,"risk":"0","is_open":true,"access_tags":[],"travel_modes":[]}]'} />
            <JsonField id="author-place-environment" label="环境 JSON" hint="对象键是资源或环境标识；值只能是字符串、布尔值或整数。" value={environmentText} onChange={(value) => { setEnvironmentText(value); clearError() }} example={'{"environment.fertility":"high","environment.flood_risk":false}'} />
            <JsonField id="author-place-harvests" label="可再生收获 JSON" hint={placeType === 'region' ? '仅区域可以登记收获政策；账户 ID 必须来自有效人物的既有账户。' : '仅区域可登记收获政策；请先把地点类型设为区域。'} value={harvestText} onChange={(value) => { setHarvestText(value); clearError() }} example={'[{"policy_id":"harvest.herb","resource_id":"core.herb","period_ticks":86400,"maximum_total":"100","recipients":[{"entity_id":"人物 UUID","account_id":"该人物的账户 UUID","per_period":"1"}]}]'} />
            {eligiblePlaces.length > 0 && <ReferenceList title="可用于路线、区域和上级地点的现有位置 ID" items={eligiblePlaces.map((place) => ({ id: place.id, label: place.name || '未命名地点' }))} />}
            {accounts.length > 0 && <ReferenceList title="可用于收获政策的现有账户 ID" items={accounts.filter((account) => account.account_type === 'holder').map((account) => ({ id: account.id, label: `${account.owner_name ?? account.owner_id ?? '持有人'} · ${account.resource_id}` }))} />}
          </> : <>
            <label className="character-create-field" htmlFor="author-organization-kind"><span>组织类型 <b>必填</b></span><select id="author-organization-kind" value={organizationKind} onChange={(event) => { setOrganizationKind(event.target.value as AuthorOrganizationDraft['kind']); clearError() }}>{organizationKindOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label className="character-create-field" htmlFor="author-organization-location"><span>所在地点 <small>可选</small></span><select id="author-organization-location" value={locationId} onChange={(event) => { setLocationId(event.target.value); clearError() }}><option value="">不指定</option>{eligiblePlaces.map((place) => <option key={place.id} value={place.id}>{place.name} · {place.id}</option>)}</select></label>
            <label className="character-create-field" htmlFor="author-organization-role"><span>公开身份 <small>1000 字以内</small></span><input id="author-organization-role" value={publicRole} maxLength={1000} onChange={(event) => { setPublicRole(event.target.value); clearError() }} /></label>
            <label className="character-create-field character-create-wide" htmlFor="author-organization-description"><span>公开描述 <small>4000 字以内</small></span><textarea id="author-organization-description" value={description} maxLength={4000} rows={3} onChange={(event) => { setDescription(event.target.value); clearError() }} /></label>
            <JsonField id="author-organization-governance" label="治理与章程 JSON（可选）" hint="完整 GovernanceComponent。成员、领导者和职务持有人引用现有人物 ID；不填写表示暂不登记治理组件。" value={governanceText} onChange={(value) => { setGovernanceText(value); clearError() }} example={'{"charter":"","members":[],"leader_id":null,"offices":[],"succession_policy":"none","decision_threshold":"0.5","authority_tags":[],"charter_clause_ids":[],"membership_recruitment":[]}'} />
            {activeCharacters.length > 0 && <ReferenceList title="可用于治理成员、领导者和职务持有人的现有人物 ID" items={activeCharacters.map((character) => ({ id: character.id, label: character.name }))} />}
            <fieldset className="character-create-field character-list-field character-create-wide">
              <legend>国库资源账户</legend>
              <span className="character-list-hint">选择世界中已有的资源 ID。每项会创建一个初始余额为 0 的账户；最多 128 项。</span>
              <datalist id="author-entity-resource-ids">{availableResources.map((resource) => <option key={resource} value={resource} />)}</datalist>
              {treasuryResources.map((resource, index) => <div className="character-list-row" key={`treasury-resource-${index}`}>
                <input value={resource} list="author-entity-resource-ids" maxLength={160} aria-label={`国库资源第 ${index + 1} 项`} onChange={(event) => updateTreasuryResource(index, event.target.value)} />
                <button type="button" className="icon-button" aria-label={`删除国库资源第 ${index + 1} 项`} onClick={() => { setTreasuryResources((current) => current.filter((_item, itemIndex) => itemIndex !== index)); clearError() }}><Icon name="close" size={14} /></button>
              </div>)}
              <button type="button" className="text-button character-list-add" onClick={() => { setTreasuryResources((current) => current.length < 128 ? [...current, ''] : current); clearError() }} disabled={treasuryResources.length >= 128}><Icon name="plus" size={13} />添加资源</button>
              {availableResources.length === 0 && <small className="character-list-hint">世界尚未登记资源目录；预览前可输入资源 ID，但服务端只接受已定义资源。</small>}
            </fieldset>
          </>}
        </div>
        {error && <RequestError error={error} fieldErrors={fieldErrors} />}
        {refreshRequired && <div className="character-conflict" role="alert"><span>分支版本已变化。请刷新作者投影，再重新检查和预览草稿。</span><button type="button" className="text-button" onClick={refreshProjection} disabled={busy}>刷新作者投影</button></div>}
        <div className="character-create-actions"><span>分支版本 {stateVersion} · 预览前不会调用创建接口</span><button className="button button-primary" type="submit" disabled={!formValid || refreshRequired}>{busy ? <><Icon name="spinner" size={15} />正在验证…</> : <>生成服务端预览 <Icon name="arrow-right" size={15} /></>}</button></div>
      </form>}
      <div className="dialog-foot"><Icon name="eye" size={13} />创建只会在确认服务端预览后提交；服务端会再次检查分支版本和完整引用。</div>
    </section>
  </div>
}

function buildPlaceDraft(input: {
  branchId: string; stateVersion: string; name: string; description: string
  placeType: AuthorPlaceDraft['place_type']; accessPolicy: AuthorPlaceDraft['access_policy']
  regionId: string; parentId: string; capacity: string; terrain: string
  routesText: string; environmentText: string; harvestText: string; places: EntityPlaceOption[]
}): AuthorPlaceDraft {
  const routes = parsePlaceRoutes(input.routesText)
  const environment = parseEnvironment(input.environmentText)
  const renewableHarvests = parseHarvests(input.harvestText)
  if (renewableHarvests.length && input.placeType !== 'region') throw new Error('只有地点类型为「区域」时才能填写可再生收获政策。')
  if (input.regionId && !input.places.some((place) => place.id === input.regionId && place.place_type === 'region')) {
    throw new Error('请选择现有区域，或清除所属区域。')
  }
  if (input.parentId && !input.places.some((place) => place.id === input.parentId)) {
    throw new Error('请选择有效的上级地点，或清除该选项。')
  }
  if (routes.some((route) => !input.places.some((place) => place.id === route.to_place_id))) {
    throw new Error('路线目标必须是作者投影中现有的地点。请核对路线 JSON 中的 to_place_id。')
  }
  const capacity = input.capacity.trim() ? parsePositiveInteger(input.capacity, '容量') : null
  const terrain = input.terrain.trim()
  if (terrain && !resourceIdPattern.test(terrain)) throw new Error('地貌需使用字母开头的资源标识，只能包含字母、数字、点、下划线和连字符。')
  return {
    branch_id: input.branchId,
    expected_state_version: input.stateVersion,
    name: input.name,
    place_type: input.placeType,
    access_policy: input.accessPolicy,
    region_id: input.regionId || null,
    parent_id: input.parentId || null,
    capacity,
    terrain: terrain || null,
    environment,
    routes,
    renewable_harvests: renewableHarvests,
    description: input.description.trim(),
  }
}

function buildOrganizationDraft(input: {
  branchId: string; stateVersion: string; name: string; description: string
  organizationKind: AuthorOrganizationDraft['kind']; locationId: string; publicRole: string
  governanceText: string; treasuryResources: string[]; places: EntityPlaceOption[]; resources: string[]
}): AuthorOrganizationDraft {
  if (input.locationId && !input.places.some((place) => place.id === input.locationId)) {
    throw new Error('请选择作者投影中的现有地点，或清除所在地点。')
  }
  const resources = input.treasuryResources.map((resource) => resource.trim()).filter(Boolean)
  const invalidResource = resources.find((resource) => !resourceIdPattern.test(resource))
  if (invalidResource) throw new Error(`资源标识「${invalidResource}」格式无效。`)
  if (new Set(resources).size !== resources.length) throw new Error('国库资源不能重复。')
  const unknownResource = resources.find((resource) => !input.resources.includes(resource))
  if (unknownResource) throw new Error(`资源「${unknownResource}」尚未在本世界定义。`)
  let governance: AuthorGovernanceDraft | null = null
  if (input.governanceText.trim()) {
    const parsed = parseJson(input.governanceText, '治理与章程')
    if (!isRecord(parsed)) throw new Error('治理与章程 JSON 顶层必须是对象。')
    governance = parsed as AuthorGovernanceDraft
  }
  return {
    branch_id: input.branchId,
    expected_state_version: input.stateVersion,
    name: input.name,
    kind: input.organizationKind,
    location_id: input.locationId || null,
    public_role: input.publicRole.trim(),
    description: input.description.trim(),
    governance,
    treasury_resources: resources,
  }
}

function parsePlaceRoutes(text: string): AuthorPlaceRouteDraft[] {
  const parsed = parseJson(text, '路线')
  if (!Array.isArray(parsed)) throw new Error('路线 JSON 顶层必须是数组。')
  return parsed.map((value, index) => {
    if (!isRecord(value)) throw new Error(`路线第 ${index + 1} 项必须是对象。`)
    const toPlaceId = value.to_place_id
    if (typeof toPlaceId !== 'string' || !uuidPattern.test(toPlaceId)) throw new Error(`路线第 ${index + 1} 项需要有效的 to_place_id。`)
    if (!isPositiveInteger(value.travel_ticks)) throw new Error(`路线第 ${index + 1} 项的 travel_ticks 必须是正整数。`)
    if (value.capacity !== undefined && value.capacity !== null && !isPositiveInteger(value.capacity)) throw new Error(`路线第 ${index + 1} 项的 capacity 必须是正整数或 null。`)
    const risk = value.risk
    if ((typeof risk !== 'number' && typeof risk !== 'string') || !Number.isFinite(Number(risk)) || Number(risk) < 0 || Number(risk) > 1) {
      throw new Error(`路线第 ${index + 1} 项的 risk 必须在 0 到 1 之间。`)
    }
    if (typeof value.is_open !== 'boolean') throw new Error(`路线第 ${index + 1} 项的 is_open 必须是 true 或 false。`)
    const accessTags = parseStringArray(value.access_tags, `路线第 ${index + 1} 项的 access_tags`)
    const travelModes = parseStringArray(value.travel_modes, `路线第 ${index + 1} 项的 travel_modes`)
    for (const tag of [...accessTags, ...travelModes]) {
      if (!resourceIdPattern.test(tag)) throw new Error(`路线第 ${index + 1} 项包含格式无效的资源标识「${tag}」。`)
    }
    return {
      to_place_id: toPlaceId,
      travel_ticks: value.travel_ticks,
      capacity: value.capacity === undefined ? undefined : value.capacity as number | null,
      risk,
      access_tags: accessTags,
      travel_modes: travelModes,
      is_open: value.is_open,
    }
  })
}

function parseEnvironment(text: string): Record<string, string | number | boolean> {
  const parsed = parseJson(text, '环境')
  if (!isRecord(parsed)) throw new Error('环境 JSON 顶层必须是对象。')
  const result: Record<string, string | number | boolean> = {}
  for (const [key, value] of Object.entries(parsed)) {
    if (!resourceIdPattern.test(key)) throw new Error(`环境键「${key}」不是有效资源标识。`)
    if (typeof value !== 'string' && typeof value !== 'boolean' && !isSafeInteger(value)) {
      throw new Error(`环境值「${key}」只能是字符串、布尔值或整数。`)
    }
    result[key] = value
  }
  return result
}

function parseHarvests(text: string): AuthorRenewableHarvestDraft[] {
  const parsed = parseJson(text, '可再生收获')
  if (!Array.isArray(parsed)) throw new Error('可再生收获 JSON 顶层必须是数组。')
  const policies = new Set<string>()
  const harvestResources = new Set<string>()
  return parsed.map((value, index) => {
    if (!isRecord(value)) throw new Error(`可再生收获第 ${index + 1} 项必须是对象。`)
    const policyId = requiredResourceId(value.policy_id, `收获第 ${index + 1} 项的 policy_id`)
    const resourceId = requiredResourceId(value.resource_id, `收获第 ${index + 1} 项的 resource_id`)
    if (policies.has(policyId.toLocaleLowerCase())) throw new Error('可再生收获 policy_id 不能重复。')
    if (harvestResources.has(resourceId.toLocaleLowerCase())) throw new Error('每种资源只能登记一项可再生收获政策。')
    policies.add(policyId.toLocaleLowerCase())
    harvestResources.add(resourceId.toLocaleLowerCase())
    if (!isPositiveInteger(value.period_ticks)) throw new Error(`收获第 ${index + 1} 项的 period_ticks 必须是正整数。`)
    const maximumTotal = positiveDecimal(value.maximum_total, `收获第 ${index + 1} 项的 maximum_total`)
    if (!Array.isArray(value.recipients) || value.recipients.length === 0) throw new Error(`收获第 ${index + 1} 项至少需要一名领取人。`)
    const recipientEntities = new Set<string>()
    const recipientAccounts = new Set<string>()
    const recipients = value.recipients.map((entry, recipientIndex) => {
      if (!isRecord(entry)) throw new Error(`收获第 ${index + 1} 项的领取人第 ${recipientIndex + 1} 项必须是对象。`)
      if (typeof entry.entity_id !== 'string' || !uuidPattern.test(entry.entity_id)) throw new Error(`收获第 ${index + 1} 项的领取人需要有效的 entity_id。`)
      if (typeof entry.account_id !== 'string' || !uuidPattern.test(entry.account_id)) throw new Error(`收获第 ${index + 1} 项的领取人需要有效的 account_id。`)
      if (recipientEntities.has(entry.entity_id) || recipientAccounts.has(entry.account_id)) throw new Error(`收获第 ${index + 1} 项的领取人物和账户不能重复。`)
      recipientEntities.add(entry.entity_id)
      recipientAccounts.add(entry.account_id)
      return {
        entity_id: entry.entity_id,
        account_id: entry.account_id,
        per_period: positiveDecimal(entry.per_period, `收获第 ${index + 1} 项领取人的 per_period`),
      }
    })
    return { policy_id: policyId, resource_id: resourceId, period_ticks: value.period_ticks, maximum_total: maximumTotal, recipients }
  })
}

function parseStringArray(value: unknown, label: string): string[] {
  if (value === undefined || value === null) return []
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) throw new Error(`${label} 必须是字符串数组。`)
  return value.map((item) => item.trim()).filter(Boolean)
}

function parseJson(text: string, label: string): unknown {
  try { return JSON.parse(text) as unknown } catch { throw new Error(`${label} JSON 格式无效，请修正后再生成预览。`) }
}

function requiredResourceId(value: unknown, label: string): string {
  if (typeof value !== 'string' || !resourceIdPattern.test(value)) throw new Error(`${label} 格式无效。`)
  return value
}

function positiveDecimal(value: unknown, label: string): string {
  if ((typeof value !== 'string' && typeof value !== 'number') || !decimalPattern.test(String(value)) || Number(value) <= 0) {
    throw new Error(`${label} 必须是大于 0 的十进制数值；精确小数请用字符串。`)
  }
  return String(value)
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function isPositiveIntegerText(value: string): boolean {
  return value.trim() === '' || (/^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value)))
}

function parsePositiveInteger(value: string, label: string): number {
  if (!/^[1-9]\d*$/.test(value.trim()) || !Number.isSafeInteger(Number(value))) throw new Error(`${label} 必须是正整数。`)
  return Number(value)
}

function isSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function formatFieldErrors(reason: unknown): string[] {
  if (!(reason instanceof ApiError) || !reason.field_errors) return []
  if (Array.isArray(reason.field_errors)) return reason.field_errors.map((item) => {
    const path = item.path.split('.').map((part) => fieldPathLabel(part)).join(' · ')
    const message = item.message ? `：${item.message}` : `：${fieldErrorTypeLabel(item.type)}`
    return `${path || '字段'}${message}`
  })
  return Object.entries(reason.field_errors).map(([path, message]) => `${path.split('.').map(fieldPathLabel).join(' · ')}：${message}`)
}

function fieldPathLabel(part: string): string {
  const labels: Record<string, string> = {
    name: '名称', place_type: '地点类型', access_policy: '通行策略', region_id: '所属区域', parent_id: '上级地点',
    capacity: '容量', terrain: '地貌', environment: '环境', routes: '路线', to_place_id: '目标地点', travel_ticks: '行程 tick',
    risk: '风险', is_open: '路线是否开放', access_tags: '通行标签', travel_modes: '通行方式', renewable_harvests: '可再生收获',
    policy_id: '政策标识', resource_id: '资源标识', period_ticks: '周期 tick', maximum_total: '总量上限', recipients: '领取人',
    entity_id: '人物 ID', account_id: '账户 ID', per_period: '每周期数量', kind: '组织类型', location_id: '所在地点',
    public_role: '公开身份', description: '公开描述', governance: '治理与章程', treasury_resources: '国库资源',
    branch_id: '分支', expected_state_version: '状态版本',
  }
  return labels[part] ?? (/^\d+$/.test(part) ? `第 ${Number(part) + 1} 项` : part)
}

function fieldErrorTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    missing: '必填字段缺失', string_too_short: '文本过短', string_too_long: '超过长度限制',
    int_type: '必须是整数', greater_than: '数值过小', less_than_equal: '数值超过上限',
    uuid_parsing: 'ID 格式无效', literal_error: '不是可用选项', decimal_parsing: '十进制数值格式无效',
    string_pattern_mismatch: '标识格式无效', value_error: '值不符合规则',
  }
  return labels[type] ?? `格式检查失败（${type}）`
}

function JsonField({ id, label, hint, value, example, onChange }: {
  id: string; label: string; hint: string; value: string; example: string; onChange: (value: string) => void
}) {
  return <label className="character-create-field character-create-wide" htmlFor={id}>
    <span>{label}</span>
    <textarea id={id} value={value} spellCheck={false} rows={4} onChange={(event) => onChange(event.target.value)} />
    <small>{hint}</small>
    <small>格式示例：<code>{example}</code></small>
  </label>
}

function ReferenceList({ title, items }: { title: string; items: { id: string; label: string }[] }) {
  return <section className="character-preview-field character-create-wide" aria-label={title}>
    <dt>{title}</dt>
    <dd>{items.map((item) => <span className="character-preview-tag" key={item.id}>{item.label} · {item.id}</span>)}</dd>
  </section>
}

function PreviewField({ label, value }: { label: string; value: string }) {
  return <div className="character-preview-field"><dt>{label}</dt><dd>{value || '未提供'}</dd></div>
}

function PreviewJson({ label, value }: { label: string; value: unknown }) {
  return <section className="character-preview-field" aria-label={label}>
    <dt>{label}</dt>
    <pre className="entity-preview-json">{JSON.stringify(value, null, 2)}</pre>
  </section>
}

function RequestError({ error, fieldErrors }: { error: string; fieldErrors: string[] }) {
  return <div className="author-entity-error" role="alert">
    <p className="character-create-error">{error}</p>
    {fieldErrors.length > 0 && <ul>{fieldErrors.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>}
  </div>
}

function formatPreviewExpiry(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-HK', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}
