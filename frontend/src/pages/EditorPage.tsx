import { useEffect, useRef, useState } from 'react'
import { api, ApiError, type AuthorCharacterResult, type AuthorEntityResult, type AuthorLeadershipBallot, type AuthorWorkAssignment, type CharacterEntity, type World, type WorldView } from '../api/client'
import { errorMessage, worldStatusLabel } from '../utils'
import { InlineState, PageEmpty } from '../components/States'
import { AuthorRevisionEditor } from '../components/AuthorRevisionEditor'
import { AuthorCharacterCreator } from '../components/AuthorCharacterCreator'
import { NpcReviewControls } from '../components/NpcReviewControls'
import { NpcRehearsalPanel } from '../components/NpcRehearsalPanel'
import { CreateEntityDialog, type AuthorEntityKind } from '../components/CreateEntityDialog'
import { AuthorKinshipEditor } from '../components/AuthorKinshipEditor'
import { WorldBookImport } from '../components/WorldBookImport'
import { Icon, type IconName } from '../components/Icon'
import type { Notice } from '../App'

type EditorTab = 'world' | 'people' | 'places' | 'organizations'
type EntityTab = Exclude<EditorTab, 'world'>
type TabDefinition = { id: EditorTab; label: string; icon: IconName }

const tabs: TabDefinition[] = [
  { id: 'world', label: '世界概览', icon: 'orbit' },
  { id: 'people', label: '人物', icon: 'users' },
  { id: 'places', label: '地点', icon: 'pin' },
  { id: 'organizations', label: '势力与组织', icon: 'layers' },
]

const organizationKinds = new Set(['polity', 'organization', 'faction', 'company', 'government'])
const entityKindLabels: Record<string, string> = {
  character: '人物', person: '人物', place: '地点', location: '地点', polity: '政治实体',
  organization: '组织', faction: '派系', company: '公司', government: '政府',
}
const componentLabels: Record<string, string> = {
  life: '生命资料', health: '健康', needs: '需求', skills: '技能', personality: '性格与价值',
  attributes: '自定义数值', capabilities: '能力', progression: '成长', known_places: '已知地点', daily_routine: '每日作息',
  place: '地点资料', market: '市场', production: '生产', governance: '治理与章程',
  treasury: '资金账户', inventory: '物品清单', population: '人口',
  public_description: '公开描述', appearance: '外貌', public_role: '公开身份',
  hidden: '隐藏标记', parent_id: '上级实体',
}
const fieldLabels: Record<string, string> = {
  windows: '每日时段', window_id: '时段 ID', start_second: '开始秒', duration_seconds: '持续秒', activity: '活动', place_id: '地点 ID',
  revision: '修订号', species_id: '物种', birth_tick: '出生时间 tick', lifespan_ticks: '寿命 tick',
  maturity_ticks: '成年 tick', population_group_id: '所属人口群体', is_immortal: '长生', life_stage: '生命阶段',
  current: '当前值', maximum: '上限', injuries: '伤势记录', conditions: '状况',
  food: '食物', water: '饮水', rest: '休息', shelter: '住所', safety: '安全', health: '健康', social: '社交', belonging: '归属',
  skills: '技能项目', level: '等级', experience: '经验', trained_ticks: '训练 tick', last_practiced_tick: '最近练习 tick',
  openness: '开放性', conscientiousness: '尽责性', extraversion: '外向性', agreeableness: '宜人性', neuroticism: '情绪敏感度',
  traits: '性格特征', values: '重视的价值', grants: '授予能力', restrictions: '限制', milestones: '成长里程碑', next_threshold: '下一门槛',
  place_type: '地点类型', region_id: '所属区域', parent_id: '上级实体', capacity: '容量', access_policy: '通行策略',
  terrain: '地貌', environment: '环境', routes: '路线', route_id: '路线 ID', from_place_id: '起点地点', to_place_id: '终点地点',
  travel_ticks: '行程 tick', risk: '风险', access_tags: '通行标签', travel_modes: '通行方式', is_open: '开放通行',
  currency_resource_id: '货币资源', offers: '市场挂单', listing_id: '挂单 ID', resource_id: '资源', stock_account_id: '库存账户',
  seller_id: '卖方 ID', unit_price_minor: '单位价格（最小单位）', minimum_quantity: '最小数量', active: '有效',
  recipes: '生产配方', quotas: '生产额度', recipe_id: '配方 ID', inputs: '投入', outputs: '产出', duration_ticks: '生产耗时 tick',
  required_skill: '所需技能', required_skill_level: '所需技能等级', quota_id: '额度 ID', source_account_id: '来源账户',
  period_ticks: '周期 tick', per_period: '每周期额度', maximum_renewal: '累计额度上限', issued_total: '已发放总量', last_issued_period: '最近发放周期',
  charter: '章程', members: '成员 ID', leader_id: '领导者 ID', offices: '职务', succession_policy: '继任方式',
  decision_threshold: '决策门槛', authority_tags: '权限标签', office_id: '职务 ID', title: '名称', holder_id: '任职者 ID',
  authority: '职权', term_end_tick: '任期结束 tick', accounts: '账户映射', lots: '库存批次', lot_id: '批次 ID',
  account_id: '账户 ID', quantity: '数量', quality: '品质', expiry_tick: '到期 tick', origin_event_id: '来源事件 ID',
  headcount: '人口数', unmodeled_headcount: '未建档人口', carrying_capacity: '承载上限', annual_birth_rate: '年出生率',
  annual_death_rate: '年死亡率', annual_migration_rate: '年迁移率', public_description: '公开描述', appearance: '外貌',
  public_role: '公开身份与职责', hidden: '作者隐藏标记',
}

export function EditorPage({ world, branchId, onSelectWorld, onNotice }: { world: World | null; branchId: string | null; onSelectWorld: () => void; onNotice: (notice: Notice) => void }) {
  const [tab, setTab] = useState<EditorTab>('world')
  const [selectedIds, setSelectedIds] = useState<Record<EntityTab, string | null>>({ people: null, places: null, organizations: null })
  const [createCharacterOpen, setCreateCharacterOpen] = useState(false)
  const [createEntityKind, setCreateEntityKind] = useState<AuthorEntityKind | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const vacancyCommand = useRef<{ scope: string; id: string } | null>(null)
  const ballotCancellationCommands = useRef(new Map<string, string>())
  const [projection, setProjection] = useState<{ key: string; view: WorldView | null; loading: boolean; error: string | null }>({ key: '', view: null, loading: true, error: null })
  const worldId = world?.world_id
  const branchForView = branchId ?? world?.active_branch_id ?? undefined
  const projectionKey = `${worldId ?? ''}:${branchForView ?? ''}:${reloadKey}`
  const projectionMatches = projection.key === projectionKey
  const view = projectionMatches ? projection.view : null
  const loading = !projectionMatches || projection.loading
  const error = projectionMatches ? projection.error : null
  const entities = Object.values(view?.state.entities ?? {})
  const people = entities.filter((entity) => entity.kind === 'character' || entity.kind === 'person')
  const places = entities.filter((entity) => entity.kind === 'place' || entity.kind === 'location')
  const organizations = entities.filter((entity) => organizationKinds.has(entity.kind))
  const locationsById = new Map(places.map((entity) => [entity.id, entity]))
  const groupedEntities: Record<EntityTab, CharacterEntity[]> = { people, places, organizations }
  const currentEntities = tab === 'world' ? [] : groupedEntities[tab]
  const selectedEntity = tab === 'world' ? null : currentEntities.find((entity) => entity.id === selectedIds[tab]) ?? currentEntities[0] ?? null
  const canRehearseNpc = tab === 'people' && selectedEntity?.status === 'active' && selectedEntity.id !== view?.state.player_id
  const queuedNpcReview = tab === 'people' && selectedEntity
    ? view?.state.scheduled_events?.map(recordValue).find((event) =>
      event.handler_id === 'npc.goal_review' && event.status === 'queued'
      && recordValue(event.payload).actor_id === selectedEntity.id)
    : undefined
  const entityPlaceOptions = places.map((place) => ({
    id: place.id,
    name: place.name,
    place_type: typeof recordValue(place.components?.place).place_type === 'string'
      ? recordValue(place.components?.place).place_type as string
      : undefined,
  }))
  const entityCharacterOptions = people.map((person) => ({ id: person.id, name: person.name ?? '未命名人物', status: person.status }))
  const entityNamesById = new Map(entities.map((entity) => [entity.id, entity.name ?? '']))
  const entityAccountOptions = Object.entries(view?.state.resources ?? {}).map(([id, account]) => ({
    id,
    resource_id: account.resource_id,
    owner_id: account.owner_id,
    owner_name: account.owner_id ? entityNamesById.get(account.owner_id) : undefined,
    account_type: account.account_type,
  }))
  const entityResourceIds = [...new Set([
    ...entityAccountOptions.map((account) => account.resource_id),
    ...Object.keys(recordValue(view?.state.metadata?.resource_catalog)),
  ])]

  useEffect(() => {
    if (!worldId) return
    let active = true
    const requestedKey = `${worldId}:${branchForView ?? ''}:${reloadKey}`
    void api.worldView(worldId, branchForView, 'author').then((authorView) => {
      if (active) setProjection({ key: requestedKey, view: authorView, loading: false, error: null })
    }).catch((reason) => { if (active) setProjection({ key: requestedKey, view: null, loading: false, error: errorMessage(reason) }) })
    return () => { active = false }
  }, [worldId, branchForView, reloadKey])

  const reloadProjection = () => setReloadKey((key) => key + 1)
  const commandIdForVacancyScope = (scope: string) => {
    if (vacancyCommand.current?.scope !== scope) vacancyCommand.current = { scope, id: crypto.randomUUID() }
    return vacancyCommand.current.id
  }
  const commandIdForBallotCancellation = (scope: string) => {
    const existing = ballotCancellationCommands.current.get(scope)
    if (existing) return existing
    const storageKey = `world-engine:cancel-invalidated-ballot:${scope}`
    try {
      const stored = sessionStorage.getItem(storageKey)
      if (stored && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(stored)) {
        ballotCancellationCommands.current.set(scope, stored)
        return stored
      }
    } catch { /* keep the stable ID in this editor instance if storage is unavailable */ }
    const commandId = crypto.randomUUID()
    ballotCancellationCommands.current.set(scope, commandId)
    try { sessionStorage.setItem(storageKey, commandId) } catch { /* no persistent storage needed to retry in this view */ }
    return commandId
  }

  const onCharacterCreated = (result: AuthorCharacterResult) => {
    setSelectedIds((current) => ({ ...current, people: result.entity_id }))
    setCreateCharacterOpen(false)
    reloadProjection()
  }

  const onEntityCreated = (kind: AuthorEntityKind, result: AuthorEntityResult) => {
    const targetTab = kind === 'place' ? 'places' : 'organizations'
    setSelectedIds((current) => ({ ...current, [targetTab]: result.entity_id }))
    setCreateEntityKind(null)
    reloadProjection()
  }

  if (!world) return <PageEmpty eyebrow="作者工作区" title="选择一个世界以查看档案。" body="作者设定需要通过服务端的作者权限校验后才能载入。" action={<button className="button button-primary" onClick={onSelectWorld}>前往世界库 <Icon name="arrow-right" size={16} /></button>} />

  return <section className="editor-page page-enter">
    <div className="editor-heading"><div><div className="eyebrow"><span className="eyebrow-line" />作者工作区</div><h1>设定档案</h1><p>浏览当前分支中服务端返回的地点、人物与组织；作者组件变更仍需预览和版本检查。</p></div><span className="spoiler-badge"><Icon name="eye" size={13} />作者视角 · 剧透</span></div>
    <div className="editor-toolbar">
      <div className="editor-world-select"><span className="mini-world-mark"><Icon name="orbit" size={15} /></span><span><strong>{world.title}</strong><small>{world.world_id}</small></span></div>
      <div className="editor-tabs" role="group" aria-label="作者档案范围">{tabs.map((item) => <button key={item.id} type="button" className={tab === item.id ? 'active' : ''} aria-pressed={tab === item.id} onClick={() => { setTab(item.id); setCreateEntityKind(null) }}><Icon name={item.icon} size={13} />{item.label}{item.id !== 'world' && <span>{groupedEntities[item.id].length}</span>}</button>)}</div>
      <button className="button button-secondary button-small" onClick={() => document.querySelector('.author-revision-panel')?.scrollIntoView({ behavior: 'smooth', block: 'center' })} disabled={!view}><Icon name="edit" size={14} />编辑组件</button>
    </div>
    {loading ? <div className="editor-loading" role="status"><Icon name="spinner" />读取作者投影…</div> : error ? <InlineState icon="alert" title="无法打开作者档案" description={error} action={<button className="button button-secondary button-small" onClick={reloadProjection}>重试作者投影</button>} /> : !view ? <InlineState icon="book" title="作者投影为空" description="服务端未返回当前分支的档案数据。" /> : tab === 'world' ? <WorldOverview world={world} view={view} entities={entities} people={people} places={places} organizations={organizations} onNotice={onNotice} onCommitted={reloadProjection} commandIdForVacancyScope={commandIdForVacancyScope} commandIdForBallotCancellation={commandIdForBallotCancellation} /> : <>
      <EntityWorkspace
        tab={tab}
        entities={currentEntities}
        referenceEntities={entities}
        selectedEntity={selectedEntity}
        locationsById={locationsById}
        worldId={world.world_id}
        branchId={view.branch.branch_id}
        stateVersion={view.branch.state_version}
        playerId={view.state.player_id}
        daySeconds={view.state.calendar?.day_seconds ?? 86_400}
        queuedNpcReview={queuedNpcReview}
        canRehearseNpc={canRehearseNpc}
        canScheduleNpcReview={Boolean(canRehearseNpc)}
        hasActiveNpcGoal={Boolean(view.state.goals?.some((goal) => {
            const row = recordValue(goal)
            return row.owner_id === selectedEntity?.id && row.status === 'active'
          }))}
        onSelect={(id) => setSelectedIds((current) => ({ ...current, [tab]: id }))}
        onNotice={onNotice}
        onCommitted={reloadProjection}
        onCreateCharacter={() => setCreateCharacterOpen(true)}
        onCreatePlace={() => setCreateEntityKind('place')}
        onCreateOrganization={() => setCreateEntityKind('organization')}
      />
      {tab === 'people' && <AuthorKinshipEditor
        key={`${world.world_id}:${view.branch.branch_id}:${view.branch.state_version}`}
        worldId={world.world_id}
        branchId={view.branch.branch_id}
        stateVersion={view.branch.state_version}
        people={people}
        links={view.state.kinship_links}
        onNotice={onNotice}
        onCommitted={reloadProjection}
      />}
    </>}
    {createCharacterOpen && view && tab === 'people' && <AuthorCharacterCreator worldId={world.world_id} branchId={view.branch.branch_id} stateVersion={view.branch.state_version} locations={places.map((location) => ({ id: location.id, name: location.name }))} onClose={() => setCreateCharacterOpen(false)} onRefreshProjection={reloadProjection} onCreated={onCharacterCreated} onNotice={onNotice} />}
    {createEntityKind && view && tab === (createEntityKind === 'place' ? 'places' : 'organizations') && <CreateEntityDialog
      key={`${world.world_id}:${view.branch.branch_id}:${view.branch.state_version}:${createEntityKind}`}
      kind={createEntityKind}
      worldId={world.world_id}
      branchId={view.branch.branch_id}
      stateVersion={view.branch.state_version}
      places={entityPlaceOptions}
      characters={entityCharacterOptions}
      accounts={entityAccountOptions}
      resources={entityResourceIds}
      onClose={() => setCreateEntityKind(null)}
      onRefreshProjection={reloadProjection}
      onCreated={onEntityCreated}
      onNotice={onNotice}
    />}
  </section>
}

function WorldOverview({ world, view, entities, people, places, organizations, onNotice, onCommitted, commandIdForVacancyScope, commandIdForBallotCancellation }: {
  world: World
  view: WorldView
  entities: CharacterEntity[]
  people: CharacterEntity[]
  places: CharacterEntity[]
  organizations: CharacterEntity[]
  onNotice: (notice: Notice) => void
  onCommitted: () => void
  commandIdForVacancyScope: (scope: string) => string
  commandIdForBallotCancellation: (scope: string) => string
}) {
  const populationSummary = regionalPopulationSummary(entities, places)
  const namedPeople = entities.filter(isNamedPerson)
  const activeNamedPeople = namedPeople.filter((entity) => entity.status !== 'dead' && entity.status !== 'retired').length
  const deceasedNamedPeople = namedPeople.filter((entity) => entity.status === 'dead').length
  const retiredNamedPeople = namedPeople.filter((entity) => entity.status === 'retired').length
  return <div className="editor-content">
    <div className="editor-main">
      <div className="editor-panel-title"><div><span className="panel-overline">WORLD PROFILE</span><h2>世界概览</h2></div><span className="version-chip">版本 {view.branch.state_version}</span></div>
      <div className="profile-grid">
        <ReadField label="世界名称" value={view.world.title} />
        <ReadField label="存档状态" value={worldStatusLabel(view.world.status)} />
        <ReadField label="当前分支" value={view.branch.label || view.branch.branch_id} />
        <ReadField label="世界时间" value={view.branch.current_tick} />
        <ReadField label="历法" value={view.state.calendar?.name ?? view.state.calendar?.calendar_id ?? '投影未提供'} />
        <ReadField label="规则版本" value={view.world.ruleset_version ?? '投影未提供'} />
      </div>
      <div className="metadata-panel"><div className="metadata-heading"><span>作者备注与基础描述</span><span>作者投影</span></div>{view.state.metadata && Object.keys(view.state.metadata).length ? <pre>{JSON.stringify(view.state.metadata, null, 2)}</pre> : <p>服务端没有返回可展示的元数据。</p>}</div>
      <WorldBookImport key={`world-book-${view.branch.branch_id}-${view.branch.state_version}`} worldId={world.world_id} branchId={view.branch.branch_id} stateVersion={view.branch.state_version} entries={view.state.world_book_entries} onNotice={onNotice} onCommitted={onCommitted} />
      <AuthorRevisionEditor key={`world-${view.branch.branch_id}-${view.branch.state_version}`} worldId={world.world_id} branchId={view.branch.branch_id} stateVersion={view.branch.state_version} entities={entities} playerId={view.state.player_id} daySeconds={view.state.calendar?.day_seconds ?? 86_400} onNotice={onNotice} onCommitted={onCommitted} />
      <LegacyVacancySettlement
        key={`vacancy-settlement:${world.world_id}:${view.branch.branch_id}:${view.branch.state_version}`}
        worldId={world.world_id}
        branchId={view.branch.branch_id}
        stateVersion={view.branch.state_version}
        assignments={view.state.work_assignments}
        entities={entities}
        onNotice={onNotice}
        onCommitted={onCommitted}
        commandIdForScope={commandIdForVacancyScope}
      />
      <InvalidatedLeadershipBallots
        key={`invalidated-leadership-ballots:${world.world_id}:${view.branch.branch_id}:${view.branch.state_version}`}
        worldId={world.world_id}
        branchId={view.branch.branch_id}
        stateVersion={view.branch.state_version}
        invalidatedIds={view.state.invalidated_leadership_ballot_ids}
        ballots={view.state.leadership_ballots}
        onNotice={onNotice}
        onCommitted={onCommitted}
        commandIdForScope={commandIdForBallotCancellation}
      />
    </div>
    <aside className="editor-side"><div className="side-title">作者投影</div><ProjectionStat label="实体" value={entities.length} caption="当前作者视角可见" /><ProjectionStat label="人物" value={people.length} caption="含当前分支人物实体" /><ProjectionStat label="地点" value={places.length} caption="place / location 实体" /><ProjectionStat label="势力与组织" value={organizations.length} caption="polity / organization 等类型" /><ProjectionStat label="资源账目" value={Object.keys(view.state.resources ?? {}).length} caption="按当前分支读取" /><div className="side-title population-side-title">人物与区域人口</div><ProjectionStat label="活跃具名人物" value={activeNamedPeople} caption="有姓名且状态非 dead / retired" /><ProjectionStat label="已故具名人物" value={deceasedNamedPeople} caption="有姓名且状态标记为 dead" /><ProjectionStat label="退场具名人物" value={retiredNamedPeople} caption="有姓名且状态标记为 retired" /><ProjectionStat label="区域聚合人口" value={populationSummary.value} caption={populationSummary.caption} /><p className="population-count-note">聚合 headcount 已包含关联具名人物。以上为并列指标，不应相加。</p><div className="privacy-note spoiler-note"><Icon name="eye" size={14} /><span>此视角可能包含玩家未知的秘密与真相。</span></div></aside>
  </div>
}

function LegacyVacancySettlement({ worldId, branchId, stateVersion, assignments, entities, onNotice, onCommitted, commandIdForScope }: {
  worldId: string
  branchId: string
  stateVersion: string
  assignments?: AuthorWorkAssignment[]
  entities: CharacterEntity[]
  onNotice: (notice: Notice) => void
  onCommitted: () => void
  commandIdForScope: (scope: string) => string
}) {
  const [submitting, setSubmitting] = useState(false)
  const mounted = useRef(true)
  const scope = `${worldId}:${branchId}:${stateVersion}`
  const entityById = new Map(entities.map((entity) => [entity.id, entity]))
  const vacancies = (assignments ?? []).filter((assignment) => {
    const actor = entityById.get(assignment.actor_id)
    return (assignment.ended_tick === undefined || assignment.ended_tick === null) && actor?.status === 'dead'
  })

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  if (!vacancies.length) return null

  const settle = async () => {
    if (submitting) return
    const commandId = commandIdForScope(scope)
    setSubmitting(true)
    try {
      const result = await api.settleLegacyOccupationVacancies(worldId, branchId, {
        command_id: commandId,
        expected_state_version: stateVersion,
      })
      if (!mounted.current) return
      onNotice(result.settled
        ? { kind: 'success', text: result.commit?.duplicate ? '岗位记录已完成此前的释放操作。' : '已结束已故任职者的历史岗位记录。' }
        : { kind: 'info', text: '当前分支没有需要释放的历史岗位。' })
      onCommitted()
    } catch (reason) {
      if (!mounted.current) return
      onNotice({ kind: 'error', text: reason instanceof ApiError && reason.status === 409
        ? '分支版本已变化，已刷新作者投影；请重新检查岗位空缺。'
        : errorMessage(reason) })
      if (reason instanceof ApiError && reason.status === 409) onCommitted()
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }

  return <section className="metadata-panel" aria-labelledby="legacy-vacancy-title">
    <div className="metadata-heading"><span id="legacy-vacancy-title">历史岗位空缺</span><span>作者维护</span></div>
    <div className="legacy-vacancy-action">
      <p>作者投影中有 {vacancies.length} 条仍有效的任职记录，其任职者已故。结束这些记录会保留历史并释放岗位名额。</p>
      <button className="button button-secondary button-small" type="button" onClick={() => void settle()} disabled={submitting}>
        <Icon name={submitting ? 'spinner' : 'check'} size={14} />{submitting ? '正在更新岗位记录…' : '结束已故任职记录'}
      </button>
    </div>
  </section>
}

function InvalidatedLeadershipBallots({ worldId, branchId, stateVersion, invalidatedIds, ballots, onNotice, onCommitted, commandIdForScope }: {
  worldId: string
  branchId: string
  stateVersion: string
  invalidatedIds?: string[]
  ballots?: AuthorLeadershipBallot[]
  onNotice: (notice: Notice) => void
  onCommitted: () => void
  commandIdForScope: (scope: string) => string
}) {
  const [submittingBallotId, setSubmittingBallotId] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const mounted = useRef(true)
  const ids = Array.isArray(invalidatedIds)
    ? [...new Set(invalidatedIds.filter((id) => typeof id === 'string' && id.trim().length > 0))]
    : []
  const recordById = new Map((ballots ?? []).filter(isAuthorLeadershipBallot).map((ballot) => [ballot.ballot_id, ballot]))

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  if (ids.length === 0) return null

  const cancel = async (ballotId: string, revision: number) => {
    if (submittingBallotId) return
    const scope = `${worldId}:${branchId}:${ballotId}:${stateVersion}`
    const commandId = commandIdForScope(scope)
    setFailure(null)
    setSubmittingBallotId(ballotId)
    try {
      const result = await api.cancelInvalidatedLeadershipBallot(worldId, branchId, ballotId, {
        command_id: commandId,
        expected_state_version: stateVersion,
        expected_ballot_revision: revision,
      })
      if (!mounted.current) return
      setFailure(null)
      onNotice(result.commit
        ? { kind: 'success', text: result.commit.duplicate ? '这项失效表决此前已取消。' : '已取消失效的领导表决。' }
        : { kind: 'info', text: '当前分支没有需要取消的失效表决。' })
      onCommitted()
    } catch (reason) {
      if (!mounted.current) return
      const stale = reason instanceof ApiError && reason.status === 409
      const message = stale
        ? '分支或表决版本已变化，已刷新作者投影；请重新检查失效表决。'
        : errorMessage(reason)
      setFailure(message)
      onNotice({ kind: 'error', text: message })
      if (stale) onCommitted()
    } finally {
      if (mounted.current) setSubmittingBallotId(null)
    }
  }

  return <section className="metadata-panel" aria-labelledby="invalidated-ballot-title">
    <div className="metadata-heading"><span id="invalidated-ballot-title">失效的领导表决</span><span>作者维护 · {ids.length} 项</span></div>
    <div className="legacy-vacancy-action">
      <p>仅列出服务端标记为失效的表决。取消会关闭该表决并保留审计记录，不会更改领导职位。</p>
    </div>
    {failure && <p className="shipment-empty" role="alert">{failure}</p>}
    <div className="shipment-list" aria-live="polite">
      {ids.map((ballotId) => {
        const ballot = recordById.get(ballotId)
        const revision = ballot ? safeBallotRevision(ballot.revision) : null
        const status = ballot?.status === 'open' ? '进行中 · 条件已失效' : ballot?.status === 'cancelled' ? '已取消' : ballot?.status ?? '投影未提供'
        return <article className="shipment-card" key={ballotId}>
          <header className="shipment-card-heading">
            <span className="shipment-status shipment-status-spoiled">{status}</span>
            <small>{ballot ? '领导表决' : '记录待刷新'}</small>
          </header>
          <p className="occupation-id">表决 ID · {ballotId}</p>
          {ballot && revision !== null
            ? <button
              className="button button-secondary button-small"
              type="button"
              onClick={() => void cancel(ballotId, revision)}
              disabled={submittingBallotId !== null}
              title="使用当前分支版本和表决修订号提交；网络重试会复用同一命令 ID"
            ><Icon name={submittingBallotId === ballotId ? 'spinner' : 'check'} size={14} />{submittingBallotId === ballotId ? '正在取消…' : '取消失效表决'}</button>
            : <p className="shipment-empty" role="status">缺少可精确读取的表决修订号，暂不能安全提交取消操作。</p>}
        </article>
      })}
    </div>
  </section>
}

function isAuthorLeadershipBallot(value: unknown): value is AuthorLeadershipBallot {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const ballot = value as Partial<AuthorLeadershipBallot>
  return typeof ballot.ballot_id === 'string'
    && typeof ballot.organization_id === 'string'
    && typeof ballot.nominee_id === 'string'
    && (typeof ballot.revision === 'number' || typeof ballot.revision === 'string')
    && typeof ballot.status === 'string'
}

function safeBallotRevision(value: number | string): number | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0 ? value : null
  if (!/^[1-9]\d*$/.test(value)) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

function EntityWorkspace({ tab, entities, referenceEntities, selectedEntity, locationsById, worldId, branchId, stateVersion, playerId, daySeconds, queuedNpcReview, canRehearseNpc, canScheduleNpcReview, hasActiveNpcGoal, onSelect, onNotice, onCommitted, onCreateCharacter, onCreatePlace, onCreateOrganization }: {
  tab: EntityTab
  entities: CharacterEntity[]
  referenceEntities: CharacterEntity[]
  selectedEntity: CharacterEntity | null
  locationsById: Map<string, CharacterEntity>
  worldId: string
  branchId: string
  stateVersion: string
  playerId?: string
  daySeconds: number
  queuedNpcReview?: Record<string, unknown>
  canRehearseNpc: boolean
  canScheduleNpcReview: boolean
  hasActiveNpcGoal: boolean
  onSelect: (id: string) => void
  onNotice: (notice: Notice) => void
  onCommitted: () => void
  onCreateCharacter: () => void
  onCreatePlace: () => void
  onCreateOrganization: () => void
}) {
  const label = scopeLabel(tab)
  const canCreateCharacter = tab === 'people' && locationsById.size > 0
  return <div className="people-editor-layout entity-editor-layout">
    <aside className="people-index entity-index">
      <div className="people-index-heading"><div><span className="panel-overline">{tab === 'people' ? 'CAST' : tab === 'places' ? 'GEOGRAPHY' : 'POLITIES & ORGANIZATIONS'}</span><h2>{label}</h2></div><div className="people-index-actions"><span>{entities.length}</span>{tab === 'people' && <button className="button button-secondary button-small" onClick={onCreateCharacter} disabled={!canCreateCharacter}><Icon name="plus" size={13} />新增人物</button>}{tab === 'places' && <button className="button button-secondary button-small" type="button" onClick={onCreatePlace}><Icon name="plus" size={13} />新增地点</button>}{tab === 'organizations' && <button className="button button-secondary button-small" type="button" onClick={onCreateOrganization}><Icon name="plus" size={13} />新增势力或组织</button>}</div></div>
      {entities.length ? <div className="entity-index-list">{entities.map((entity) => <button key={entity.id} className={`people-index-item ${selectedEntity?.id === entity.id ? 'selected' : ''}`} onClick={() => onSelect(entity.id)} aria-pressed={selectedEntity?.id === entity.id}>
        <span className="entity-kind-avatar"><Icon name={tabIcon(tab)} size={14} /></span><span><strong>{entity.name || '未命名实体'}</strong><small>{entityStatusLabel(entity.status) || entityKindLabel(entity.kind) || entity.kind}</small></span><Icon name="chevron" size={15} />
      </button>)}</div> : <div className="entity-scope-empty"><Icon name={tabIcon(tab)} size={18} /><strong>当前投影没有{label}</strong><span>作者视图未返回对应实体；此处不会生成示例内容。</span></div>}
      {tab === 'people' && !locationsById.size && <p className="character-location-note">作者投影没有可选地点，因此不能创建人物。</p>}
    </aside>
    {selectedEntity ? <article className="person-profile entity-profile">
      <div className="person-profile-top"><span className="entity-kind-avatar entity-kind-avatar-large"><Icon name={tabIcon(tab)} size={20} /></span><div><span className="panel-overline">{entityKindLabel(selectedEntity.kind) || 'AUTHOR ENTITY'} · {selectedEntity.kind}</span><h2>{selectedEntity.name || '未命名实体'}</h2><p>{entityStatusLabel(selectedEntity.status) || '状态由实体组件定义'}</p></div><span className="spoiler-badge compact-spoiler"><Icon name="eye" size={12} />作者视角</span></div>
      <div className="character-fields">
        <ReadField label="实体类型" value={entityKindLabel(selectedEntity.kind) || selectedEntity.kind} />
        {selectedEntity.status && <ReadField label="当前状态" value={entityStatusLabel(selectedEntity.status)} />}
        {selectedEntity.location_id && <ReadField label={tab === 'places' ? '所属位置 ID' : '所在地点'} value={displayEntityReference(selectedEntity.location_id, locationsById)} />}
        <ReadField label="实体标识" value={selectedEntity.id} />
        <ReadField label="返回组件" value={Object.keys(selectedEntity.components ?? {}).length} />
      </div>
      <EntityComponentDetails components={selectedEntity.components ?? {}} />
      {queuedNpcReview ? <p className="character-location-note" role="status">
        自主目标复审已安排。下次在世界第 {String(queuedNpcReview.due_tick)} 秒，已复审 {String(queuedNpcReview.completed_runs)} / {String(queuedNpcReview.max_runs)} 次。
      </p> : canScheduleNpcReview && <NpcReviewControls key={`${branchId}:${selectedEntity.id}:${stateVersion}`} worldId={worldId} branchId={branchId} stateVersion={stateVersion} actorId={selectedEntity.id}
        destinations={[...locationsById.values()].filter((place) => place.status === 'active' && place.id !== selectedEntity.location_id)} hasActiveGoal={hasActiveNpcGoal}
        onCommitted={() => { onNotice({ kind: 'success', text: '自主目标复审已安排。' }); onCommitted() }} />}
      {canRehearseNpc && <NpcRehearsalPanel key={`${branchId}:${selectedEntity.id}:${stateVersion}`} worldId={worldId} branchId={branchId} stateVersion={stateVersion} actorId={selectedEntity.id} />}
      <AuthorRevisionEditor key={`${selectedEntity.id}-${stateVersion}`} worldId={worldId} branchId={branchId} stateVersion={stateVersion} entities={[selectedEntity]} referenceEntities={referenceEntities} playerId={playerId} daySeconds={daySeconds} onNotice={onNotice} onCommitted={onCommitted} />
    </article> : <div className="person-profile-empty"><Icon name={tabIcon(tab)} size={22} /><strong>没有可打开的{label}</strong><span>实体详情只会在服务端返回后显示。</span></div>}
  </div>
}

function EntityComponentDetails({ components }: { components: Record<string, unknown> }) {
  const entries = Object.entries(components).sort(([left], [right]) => componentOrder(left) - componentOrder(right) || left.localeCompare(right))
  return <section className="entity-components" aria-label="实体公开与类型化组件">
    <div className="entity-components-heading"><div><span className="panel-overline">RETURNED COMPONENTS</span><h3>公开资料与类型化数据</h3></div><span>{entries.length} 项</span></div>
    {entries.length ? <div className="entity-component-list">{entries.map(([name, value]) => <article className="entity-component-card" key={name}>
      <div className="entity-component-card-heading"><div><span className="entity-component-key">{name}</span><h4>{componentLabels[name] ?? humanize(name)}</h4></div>{typeof value === 'object' && value !== null && !Array.isArray(value) && 'revision' in value && <span className="version-chip">修订 {String((value as Record<string, unknown>).revision)}</span>}</div>
      {typeof value === 'string' ? <p className="entity-component-text">{value || '未提供内容'}</p> : value !== null && typeof value === 'object' && !Array.isArray(value) ? <TypedObjectFields value={value as Record<string, unknown>} /> : <TypedValue value={value} />}
    </article>)}</div> : <p className="entity-components-empty">服务端未返回此实体的组件。</p>}
  </section>
}

function TypedObjectFields({ value }: { value: Record<string, unknown> }) {
  const fields = Object.entries(value).filter(([key]) => key !== 'revision')
  if (!fields.length) return <p className="entity-components-empty">没有其他字段。</p>
  return <dl className="entity-component-fields">{fields.map(([key, item]) => <div className="entity-component-field" key={key}><dt>{fieldLabels[key] ?? humanize(key)}</dt><dd><TypedValue value={item} /></dd></div>)}</dl>
}

function TypedValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="entity-component-absent">未提供</span>
  if (typeof value === 'boolean') return <span>{value ? '是' : '否'}</span>
  if (typeof value === 'string' || typeof value === 'number') return <span className={typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(value) ? 'entity-reference-value' : ''}>{value}</span>
  const count = Array.isArray(value) ? value.length : Object.keys(value as Record<string, unknown>).length
  const description = Array.isArray(value) ? `${count} 项` : `${count} 个字段`
  return <details className="entity-nested-value"><summary>展开数据 · {description}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>
}

function componentOrder(name: string) {
  const order = ['public_description', 'public_role', 'appearance', 'place', 'governance', 'daily_routine', 'personality', 'life', 'health', 'needs', 'skills', 'capabilities', 'progression', 'market', 'production', 'treasury', 'inventory', 'population']
  const index = order.indexOf(name)
  return index < 0 ? order.length : index
}

function scopeLabel(tab: EntityTab) { return tab === 'people' ? '人物' : tab === 'places' ? '地点' : '势力与组织' }
function entityStatusLabel(status: string | null | undefined) { return status === 'incapacitated' ? '重伤倒下' : status ?? '' }
function tabIcon(tab: EntityTab): IconName { return tab === 'people' ? 'users' : tab === 'places' ? 'pin' : 'layers' }
function entityKindLabel(kind: string) { return entityKindLabels[kind] }
function displayEntityReference(id: string, entities: Map<string, CharacterEntity>) { const entity = entities.get(id); return entity?.name ? `${entity.name} · ${id}` : id }
function humanize(key: string) { return key.replaceAll('_', ' ') }

function ReadField({ label, value }: { label: string; value: string | number }) { return <div className="read-field"><span>{label}</span><strong>{value}</strong></div> }
function ProjectionStat({ label, value, caption }: { label: string; value: number | string; caption: string }) { return <div className="projection-stat"><span>{label}</span><strong>{typeof value === 'number' ? value.toLocaleString('zh-HK') : value}</strong><small>{caption}</small></div> }

function isNamedPerson(entity: CharacterEntity) {
  return (entity.kind === 'character' || entity.kind === 'person') && typeof entity.name === 'string' && entity.name.trim().length > 0
}

function regionalPopulationSummary(entities: CharacterEntity[], places: CharacterEntity[]) {
  const regionIds = new Set(places.filter((place) => recordValue(place.components?.place).place_type === 'region').map((place) => place.id))
  const populationGroups = entities.filter((entity) => {
    const kindSupportsPopulation = ['group', 'population', 'population_group', 'cohort'].includes(entity.kind)
    return kindSupportsPopulation && typeof entity.location_id === 'string' && regionIds.has(entity.location_id) && recordValue(entity.components?.population).headcount !== undefined
  })
  if (!populationGroups.length) return { value: '未提供', caption: '作者投影没有区域人口聚合记录' }
  let total = 0n
  for (const group of populationGroups) {
    const parsed = parseCount(recordValue(group.components?.population).headcount)
    if (parsed === null) return { value: '数据不完整', caption: `${populationGroups.length} 个区域聚合记录中含无法读取的 headcount` }
    total += parsed
  }
  return { value: total.toLocaleString('zh-HK'), caption: `${populationGroups.length} 个区域聚合记录；包含关联具名人物` }
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function parseCount(value: unknown): bigint | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0 ? BigInt(value) : null
  if (typeof value === 'string' && /^\d+$/.test(value)) return BigInt(value)
  return null
}
