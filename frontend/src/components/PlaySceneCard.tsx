import { useId } from 'react'
import type { CharacterEntity, RelationView, SceneOpportunity, WorldState } from '../api/client'
import { initials } from '../utils'
import { formatWorldDuration, formatWorldRelativeTick } from '../worldTime'
import { Icon } from './Icon'
import './PlaySceneCard.css'

export type PlayScenePortraitPosition = 'left' | 'center' | 'right'
export type PlayScenePortraitEmotion = 'calm' | 'joy' | 'concern' | 'anger' | 'surprise'

export type PlayScenePortrait = {
  entityId: string
  position: PlayScenePortraitPosition
  asset?: string
  emotion?: PlayScenePortraitEmotion
}

export type PlaySceneStage = {
  placeId: string
  background?: string
  portraits?: readonly PlayScenePortrait[]
  activeSpeakerId?: string
}

// Presentation can only select checked-in files by basename. Scene data cannot
// supply a URL, add a person to the cast, or override the player's current place.
const sceneMedia = import.meta.glob<string>('../assets/scenes/*.{avif,jpg,jpeg,png,webp}', {
  eager: true,
  import: 'default',
  query: '?url',
})

const positions = ['left', 'center', 'right'] as const
const emotionLabels: Record<PlayScenePortraitEmotion, string> = {
  calm: '平静', joy: '欣喜', concern: '担忧', anger: '生气', surprise: '惊讶',
}
const actorStatusLabels: Record<string, string> = {
  active: '可行动', dead: '已去世', retired: '已退场', incapacitated: '失去行动能力',
}
const relationLabels: Record<string, string> = {
  familiarity: '熟悉度', trust: '信任', affinity: '亲近', respect: '尊重', fear: '畏惧', resentment: '怨恨',
}
const routineLabels: Record<string, string> = {
  rest: '休息', work: '工作', study: '学习', socialize: '社交',
}

export type SceneDraftSource = { kind: 'opportunity' | 'investigation'; id: string }
type SceneDraft = { mode: 'ask_gm' | 'roleplay' | 'action'; text: string; source?: SceneDraftSource }

type PlaySceneCardProps = {
  state?: WorldState
  loading: boolean
  viewError: boolean
  stage?: PlaySceneStage
  canPrepareAction: boolean
  canPrepareQuery: boolean
  canPlanRoute: boolean
  onPrepareDraft: (draft: SceneDraft) => void
  onPlanRoute: (routeId: string) => void
  onOpenInvestigation: () => void
  onOpenJourney: () => void
  onOpenComposer: () => void
}

export function PlaySceneCard({ state, loading, viewError, stage, canPrepareAction, canPrepareQuery, canPlanRoute, onPrepareDraft, onPlanRoute, onOpenInvestigation, onOpenJourney, onOpenComposer }: PlaySceneCardProps) {
  const headingId = useId()
  const player = state?.player_id ? state.entities[state.player_id] : undefined
  const currentPlace = player?.location_id ? state?.entities[player.location_id] : undefined
  const traveling = Boolean(state?.own_active_travel?.status === 'in_transit')
  const place = !traveling && (currentPlace?.kind === 'place' || currentPlace?.kind === 'location') ? currentPlace : undefined
  const cast = place ? Object.values(state?.entities ?? {}).filter((entity) => (
    (entity.kind === 'character' || entity.kind === 'person') && entity.id !== player?.id
    && entity.location_id === place.id && entity.status !== 'dead' && entity.status !== 'retired'
  )) : []
  const exits = place ? state?.travel_options ?? [] : []
  const opportunities = state?.scene_opportunities ?? []
  const anchors = place ? state?.available_case_anchors ?? [] : []
  const goals = (state?.goals ?? []).filter(isCurrentGoal)
  const caseFiles = state?.case_files ?? []
  const placeName = place?.name?.trim() || '未命名地点'
  const description = publicText(place?.components?.public_description)
  const currentStage = place && stage?.placeId === place.id ? stage : undefined
  const byId = new Map([...(player && place ? [player] : []), ...cast].map((person) => [person.id, person]))
  const outgoingRelations = new Map((state?.relations ?? []).filter((relation) => relation.from_id === player?.id).map((relation) => [relation.to_id, relation]))
  const available = !loading && !viewError
  const sceneName = available && place ? placeName : available && traveling ? '旅途中' : '当前场景'
  const daySeconds = state?.calendar?.day_seconds ?? 0
  const yearDays = state?.calendar?.year_days ?? 0
  const formatDate = (value: unknown) => {
    const tick = exactTick(value)
    return tick === null ? undefined : formatWorldRelativeTick(tick, daySeconds, yearDays)
  }
  const destination = traveling && state?.own_active_travel
    ? state.entities[state.own_active_travel.to_place_id]?.name
    : undefined

  return <aside className="play-scene-card" aria-labelledby={headingId}>
    <div className="play-scene-hero">
      <SceneStage stage={available ? currentStage : undefined} people={byId} />
      <div className="play-scene-hero-caption">
        {available && (place || traveling) && <span className="play-scene-location-label"><Icon name="pin" size={13} />{traveling ? '当前位置' : '当前场景'}</span>}
        <h2 id={headingId}>{sceneName}</h2>
      </div>
    </div>
    <div className="play-scene-context">
      {loading && <p className="play-scene-empty" role="status">正在读取角色可见的场景…</p>}
      {!loading && viewError && <p className="play-scene-empty" role="status">当前分支的场景暂时无法读取。</p>}
      {available && !place && !traveling && <p className="play-scene-empty">当前位置尚未出现在角色视角中。</p>}
      {available && player && <div className="play-scene-player">
        <Icon name="user" size={16} />
        <span>扮演 <strong>{player.name?.trim() || '未命名人物'}</strong></span>
        <small>{actorStatusLabels[player.status ?? ''] ?? '状态未知'}</small>
      </div>}
      {available && traveling && <div className="play-scene-travel">
        {destination && <strong>前往{destination}</strong>}
        <p className="play-scene-empty">预计抵达 {formatDate(state?.own_active_travel?.arrival_tick) ?? '时间待确认'}</p>
        <button type="button" className="play-scene-observe" onClick={onOpenJourney}><Icon name="clock" size={16} />查看行程进度<Icon name="arrow-right" size={15} /></button>
      </div>}
      {available && description && <p className="play-scene-description">{description}</p>}
      {available && player && <section className="play-scene-section play-scene-opportunities" aria-label="眼下可做">
        <h3><Icon name="compass" size={16} />眼下可做</h3>
        {opportunities.length > 0 && <SceneList label="机会" items={opportunities} render={(opportunity) => (
          <OpportunityCard key={opportunity.opportunity_id} opportunity={opportunity} formatDate={formatDate}
            canPrepareAction={canPrepareAction} canPrepareQuery={canPrepareQuery} onPrepareDraft={onPrepareDraft} />
        )} />}
        {anchors.length > 0 && <SceneList label="调查线索" items={anchors} render={(anchor) => {
          const existing = caseFiles.find((file) => file.case_id === anchor.case_id)
          return <article key={anchor.anchor_id} className="play-scene-opportunity play-scene-clue">
            <div className="play-scene-opportunity-title"><Icon name="eye" size={14} /><strong>{anchor.title}</strong></div>
            {anchor.brief && <p>{anchor.brief}</p>}
            {existing
              ? <button type="button" className="play-scene-observe" onClick={onOpenInvestigation}>查看调查日志<Icon name="arrow-right" size={15} /></button>
              : <button type="button" className="play-scene-observe" disabled={!canPrepareAction} onClick={() => onPrepareDraft({ mode: 'action', text: `请为线索「${anchor.title}」建立一份我的私人调查档案。`, source: { kind: 'investigation', id: anchor.anchor_id } })}>准备建立案卷<Icon name="arrow-right" size={15} /></button>}
          </article>
        }} />}
        {opportunities.length === 0 && anchors.length === 0 && <p className="play-scene-empty">{traveling ? '途中尚无可介入的事。' : '眼前没有已知的机会。'}</p>}
        <div className="play-scene-quick-actions">
          <button type="button" className="play-scene-observe" disabled={!canPrepareQuery} onClick={() => onPrepareDraft({ mode: 'ask_gm', text: '从我的角色视角，看看此刻周围有什么值得留意的？' })}>
            <Icon name="eye" size={16} />观察周围
          </button>
          {!traveling && <button type="button" className="play-scene-observe" disabled={!canPlanRoute} onClick={onOpenJourney}><Icon name="clock" size={16} />等待或长行动</button>}
          <button type="button" className="play-scene-observe" onClick={onOpenComposer}><Icon name="message" size={16} />前往对话</button>
        </div>
      </section>}
      {available && place && <div className="play-scene-local">
        <div className="play-scene-section">
          <h3><Icon name="users" size={16} />在场人物 <small>{cast.length}</small></h3>
          {cast.length ? <SceneList label="人物" items={cast} render={(person) => <ScenePerson key={person.id} person={person}
            relation={outgoingRelations.get(person.id)} state={state} formatDate={formatDate}
            canTalk={canPrepareAction} onPrepareDraft={onPrepareDraft} />} /> : <p className="play-scene-empty">这里暂时没有其他可见人物。</p>}
        </div>
        <div className="play-scene-section">
          <h3><Icon name="compass" size={16} />可走路线 <small>{exits.length}</small></h3>
          {exits.length ? <SceneList label="路线" items={exits} render={(route) => {
            const duration = exactTick(route.travel_ticks)
            return <button type="button" className="play-scene-exit" key={route.route_id} disabled={!canPlanRoute || duration === null || duration <= 0n}
              onClick={() => onPlanRoute(route.route_id)}>
              <span><strong>{route.destination_name}</strong><small>{duration === null ? '耗时待确认' : formatWorldDuration(duration, daySeconds, yearDays)}</small></span><Icon name="arrow-up-right" size={15} />
            </button>
          }} /> : <p className="play-scene-empty">当前没有可用的出发路线。</p>}
        </div>
      </div>}
      {available && player && <section className="play-scene-section" aria-label="已知目标">
        <h3><Icon name="crosshair" size={16} />已知目标</h3>
        {goals.length ? <SceneList label="目标" items={goals} render={(goal) => {
          const deadline = formatDate(goal.deadline)
          return <article className="play-scene-goal" key={goal.id}>
            <strong>{goal.title}</strong>
            {goal.description && <p>{goal.description}</p>}
            {deadline && <small>期限 {deadline}</small>}
            <button type="button" className="play-scene-observe" disabled={!canPrepareQuery} onClick={() => onPrepareDraft({ mode: 'ask_gm', text: `根据我已掌握的情况，我可以怎样推进目标「${goal.title}」？` })}>询问进展</button>
          </article>
        }} /> : <p className="play-scene-empty">目前没有进行中的个人目标。</p>}
      </section>}
    </div>
  </aside>
}

function SceneList<T>({ label, items, render }: { label: string; items: readonly T[]; render: (item: T) => React.ReactNode }) {
  const previewCount = 2
  return <div className="play-scene-list">
    {items.slice(0, previewCount).map(render)}
    {items.length > previewCount && <details className="play-scene-more">
      <summary>其余{items.length - previewCount}个{label}</summary>
      <div className="play-scene-list">{items.slice(previewCount).map(render)}</div>
    </details>}
  </div>
}

function OpportunityCard({ opportunity, formatDate, canPrepareAction, canPrepareQuery, onPrepareDraft }: {
  opportunity: SceneOpportunity
  formatDate: (value: unknown) => string | undefined
  canPrepareAction: boolean
  canPrepareQuery: boolean
  onPrepareDraft: (draft: SceneDraft) => void
}) {
  const deadline = formatDate(opportunity.deadline_tick)
  const draft: SceneDraft = opportunity.draft ?? { mode: 'ask_gm', text: `根据我已知的情况，关于「${opportunity.title}」，眼下有哪些可行做法？` }
  const actionLabel = { ask_gm: '准备询问', roleplay: '准备交谈', action: '准备行动' }[draft.mode]
  return <article className="play-scene-opportunity">
    <div className="play-scene-opportunity-title"><Icon name="spark" size={15} /><strong>{opportunity.title}</strong></div>
    {opportunity.detail && <p>{opportunity.detail}</p>}
    <dl>
      {opportunity.contact_name && <div><dt>联系</dt><dd>{opportunity.contact_name}</dd></div>}
      {opportunity.place_name && <div><dt>地点</dt><dd>{opportunity.place_name}</dd></div>}
      {deadline && <div><dt>期限</dt><dd>{deadline}</dd></div>}
      <div><dt>代价</dt><dd>{opportunity.known_cost ?? '尚未得知'}</dd></div>
      {opportunity.possible_impact && <div><dt>可能影响</dt><dd>{opportunity.possible_impact}</dd></div>}
    </dl>
    <button type="button" className="play-scene-observe" disabled={draft.mode === 'ask_gm' ? !canPrepareQuery : !canPrepareAction}
      onClick={() => onPrepareDraft({ ...draft, source: { kind: 'opportunity', id: opportunity.opportunity_id } })}><Icon name="edit" size={15} />{actionLabel}<Icon name="arrow-right" size={15} /></button>
  </article>
}

type CurrentGoal = { id: string; title: string; description?: string; deadline?: string | number | null }

function isCurrentGoal(value: unknown): value is CurrentGoal {
  if (!value || typeof value !== 'object') return false
  const goal = value as Partial<CurrentGoal & { status: string }>
  return typeof goal.id === 'string' && typeof goal.title === 'string'
    && (goal.status === 'active' || goal.status === 'paused')
    && (goal.description === undefined || typeof goal.description === 'string')
}

function exactTick(value: unknown): bigint | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) ? BigInt(value) : null
  return typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : null
}

function ScenePerson({ person, relation, state, formatDate, canTalk, onPrepareDraft }: {
  person: CharacterEntity
  relation?: RelationView
  state?: WorldState
  formatDate: (value: unknown) => string | undefined
  canTalk: boolean
  onPrepareDraft: (draft: SceneDraft) => void
}) {
  const name = person.name?.trim()
  const role = publicText(person.components?.public_role)
  const dimensions = Object.entries(relation?.dimensions ?? {})
  const schedule = state?.scene_npc_readback?.[person.id]?.public_schedule ?? []
  const knownChanges = state?.scene_npc_readback?.[person.id]?.known_changes ?? []
  const lastContact = formatDate(state?.scene_npc_readback?.[person.id]?.last_contact_tick)
  const promises = (state?.own_promises ?? []).filter((promise) => promise.counterparty_id === person.id
    && promise.status === 'active' && Boolean(promise.text))
  const meetings = (state?.own_relationship_invitations ?? []).filter((invitation) => invitation.meeting
    && (invitation.status === 'pending' || invitation.status === 'accepted')
    && (invitation.proposer_id === person.id || invitation.invitee_id === person.id))
  const daySeconds = state?.calendar?.day_seconds ?? 0
  const yearDays = state?.calendar?.year_days ?? 0
  return <article className="play-scene-person">
    <div className="play-scene-person-row">
      <span className="play-scene-initial" aria-hidden="true">{initials(name ?? '未')}</span>
      <span><strong>{name || '未命名人物'}</strong>{role && <small>{role}</small>}</span>
      {name && person.status === 'active' && <button type="button" className="play-scene-talk" aria-label={`与${name}交谈`} disabled={!canTalk} onClick={() => onPrepareDraft({ mode: 'roleplay', text: `我向「${name}」开口：“你好。”` })}><Icon name="message" size={15} />交谈</button>}
    </div>
    {(schedule.length > 0 || knownChanges.length > 0 || promises.length > 0 || meetings.length > 0 || lastContact) && <details className="play-scene-relation play-scene-person-readback">
      <summary>作息、约定与近况</summary>
      {lastContact && <p>上次交谈 · {lastContact}</p>}
      {schedule.length > 0 && <>
        <strong>通常的公开作息</strong>
        <ul>{schedule.map((window, index) => <li key={index}>
          {window.place_name} · {routineLabels[window.activity] ?? window.activity} ·
          {' '}{window.every_days > 1 ? `每 ${window.every_days} 日` : '每日'} {formatSceneClock(window.start_second)} 起，
          持续 {formatWorldDuration(BigInt(window.duration_seconds), daySeconds, yearDays)}
        </li>)}</ul>
      </>}
      {(promises.length > 0 || meetings.length > 0) && <>
        <strong>与我的约定</strong>
        <ul>
          {promises.map((promise) => <li key={promise.promise_id}>{promise.text}{promise.deadline_tick != null && <small>{formatDate(promise.deadline_tick)}</small>}</li>)}
          {meetings.map((invitation) => <li key={invitation.invitation_id}>
            {invitation.status === 'accepted' ? '已接受的会面' : '待回应的邀约'}{invitation.meeting_venue_name && ` · ${invitation.meeting_venue_name}`}
            <small>{formatDate(invitation.meeting?.start_tick)}</small>
          </li>)}
        </ul>
      </>}
      {knownChanges.length > 0 && <>
        <strong>我获知的近况</strong>
        <ul>{knownChanges.map((record, index) => <li key={index}><q>{record.claim}</q><small>{formatDate(record.received_tick)}</small></li>)}</ul>
      </>}
    </details>}
    {dimensions.length > 0 && <details className="play-scene-relation">
      <summary>我对{name || '这位人物'}的关系记录</summary>
      <dl>{dimensions.map(([dimension, value]) => <div key={dimension}><dt>{relationLabels[dimension] ?? dimension}</dt><dd>{value}</dd></div>)}</dl>
      {relation?.label && <p>{relation.label}</p>}
    </details>}
  </article>
}

function formatSceneClock(seconds: number) {
  const hours = Math.floor(seconds / 3_600)
  const minutes = Math.floor(seconds / 60) % 60
  const remainingSeconds = seconds % 60
  return (remainingSeconds ? [hours, minutes, remainingSeconds] : [hours, minutes])
    .map((part) => String(part).padStart(2, '0')).join(':')
}

function SceneStage({ stage, people }: { stage?: PlaySceneStage; people: Map<string, CharacterEntity> }) {
  const background = mediaSource(stage?.background)
  const portraits = positions.flatMap((position) => {
    const portrait = stage?.portraits?.find((entry) => entry.position === position && people.has(entry.entityId))
    if (!portrait) return []
    const person = people.get(portrait.entityId)
    if (!person) return []
    return [{ portrait, name: person.name?.trim() || '未命名人物' }]
  })

  return <>
    <div className="play-scene-landscape" aria-hidden="true">
      <div className="play-scene-ridge" />
      {background && <img src={background} alt="" className="play-scene-backdrop" />}
    </div>
    {portraits.length > 0 && <div className="play-scene-stage" aria-label="场景立绘">
      {portraits.map(({ portrait, name }) => {
        const source = mediaSource(portrait.asset)
        const speaking = portrait.entityId === stage?.activeSpeakerId
        const emotion = portrait.emotion ? emotionLabels[portrait.emotion] : undefined
        return <figure key={portrait.position} data-position={portrait.position} data-speaking={speaking}>
          {source ? <img src={source} alt="" /> : <span className="play-scene-stage-initial" aria-hidden="true">{initials(name)}</span>}
          <figcaption><strong>{name}</strong>{(speaking || emotion) && <small>{[speaking ? '正在说话' : '', emotion].filter(Boolean).join(' · ')}</small>}</figcaption>
        </figure>
      })}
    </div>}
  </>
}

function mediaSource(basename?: string) {
  return basename && /^[\w.-]+\.(?:avif|jpe?g|png|webp)$/.test(basename) ? sceneMedia[`../assets/scenes/${basename}`] : undefined
}

function publicText(value: unknown) {
  return typeof value === 'string' ? value.trim() : ''
}
