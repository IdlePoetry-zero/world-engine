import { useId } from 'react'
import type { CharacterEntity, RelationView, TravelOption, WorldState } from '../api/client'
import { initials } from '../utils'
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

type SceneDraft = { mode: 'ask_gm' | 'roleplay' | 'action'; text: string }

type PlaySceneCardProps = {
  state?: WorldState
  loading: boolean
  viewError: boolean
  stage?: PlaySceneStage
  canPrepareAction: boolean
  canPrepareQuery: boolean
  onPrepareDraft: (draft: SceneDraft) => void
}

export function PlaySceneCard({ state, loading, viewError, stage, canPrepareAction, canPrepareQuery, onPrepareDraft }: PlaySceneCardProps) {
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
  const placeName = place?.name?.trim() || '未命名地点'
  const description = publicText(place?.components?.public_description)
  const currentStage = place && stage?.placeId === place.id ? stage : undefined
  const byId = new Map([...(player && place ? [player] : []), ...cast].map((person) => [person.id, person]))
  const outgoingRelations = new Map((state?.relations ?? []).filter((relation) => relation.from_id === player?.id).map((relation) => [relation.to_id, relation]))
  const available = !loading && !viewError
  const sceneName = available && place ? placeName : available && traveling ? '旅途中' : '当前场景'

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
      {available && traveling && <p className="play-scene-empty">抵达后将显示当地人物和可走的路线。</p>}
      {available && player && <div className="play-scene-player">
        <Icon name="user" size={16} />
        <span>扮演 <strong>{player.name?.trim() || '未命名人物'}</strong></span>
        <small>{actorStatusLabels[player.status ?? ''] ?? '状态未知'}</small>
      </div>}
      {available && place && <>
        {description && <p className="play-scene-description">{description}</p>}
        <button type="button" className="play-scene-observe" disabled={!canPrepareQuery} onClick={() => onPrepareDraft({ mode: 'ask_gm', text: '从我的角色视角，看看此刻周围有什么值得留意的？' })}>
          <Icon name="eye" size={16} />观察周围<Icon name="arrow-right" size={15} />
        </button>
        <div className="play-scene-section">
          <h3><Icon name="users" size={16} />在场人物 <small>{cast.length}</small></h3>
          {cast.length ? <SceneList label="人物" items={cast} render={(person) => <ScenePerson key={person.id} person={person} relation={outgoingRelations.get(person.id)} canTalk={canPrepareAction} onPrepareDraft={onPrepareDraft} />} /> : <p className="play-scene-empty">这里暂时没有其他可见人物。</p>}
        </div>
        <div className="play-scene-section">
          <h3><Icon name="compass" size={16} />可走路线 <small>{exits.length}</small></h3>
          {exits.length ? <SceneList label="路线" items={exits} render={(route: TravelOption) => (
            <button type="button" className="play-scene-exit" key={route.route_id} disabled={!canPrepareAction} onClick={() => onPrepareDraft({ mode: 'action', text: `我沿路线 ${route.route_id} 前往「${route.destination_name}」。` })}>
              <span><strong>{route.destination_name}</strong><small>{route.travel_ticks} tick</small></span><Icon name="arrow-up-right" size={15} />
            </button>
          )} /> : <p className="play-scene-empty">当前没有可用的出发路线。</p>}
        </div>
      </>}
    </div>
  </aside>
}

function SceneList<T>({ label, items, render }: { label: string; items: readonly T[]; render: (item: T) => React.ReactNode }) {
  const previewCount = 3
  return <div className="play-scene-list">
    {items.slice(0, previewCount).map(render)}
    {items.length > previewCount && <details className="play-scene-more">
      <summary>其余{items.length - previewCount}个{label}</summary>
      <div className="play-scene-list">{items.slice(previewCount).map(render)}</div>
    </details>}
  </div>
}

function ScenePerson({ person, relation, canTalk, onPrepareDraft }: { person: CharacterEntity; relation?: RelationView; canTalk: boolean; onPrepareDraft: (draft: SceneDraft) => void }) {
  const name = person.name?.trim()
  const role = publicText(person.components?.public_role)
  const dimensions = Object.entries(relation?.dimensions ?? {})
  return <article className="play-scene-person">
    <div className="play-scene-person-row">
      <span className="play-scene-initial" aria-hidden="true">{initials(name ?? '未')}</span>
      <span><strong>{name || '未命名人物'}</strong>{role && <small>{role}</small>}</span>
      {name && person.status === 'active' && <button type="button" className="play-scene-talk" aria-label={`与${name}交谈`} disabled={!canTalk} onClick={() => onPrepareDraft({ mode: 'roleplay', text: `我向「${name}」开口：“你好。”` })}><Icon name="message" size={15} />交谈</button>}
    </div>
    {dimensions.length > 0 && <details className="play-scene-relation">
      <summary>我对{name || '这位人物'}的关系记录</summary>
      <dl>{dimensions.map(([dimension, value]) => <div key={dimension}><dt>{relationLabels[dimension] ?? dimension}</dt><dd>{value}</dd></div>)}</dl>
      {relation?.label && <p>{relation.label}</p>}
    </details>}
  </article>
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
