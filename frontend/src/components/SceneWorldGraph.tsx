import type { WorldState } from '../api/client'
import { formatWorldDuration } from '../worldTime'
import './SceneWorldGraph.css'

type SceneWorldGraphProps = {
  state?: WorldState
  loading: boolean
  viewError: boolean
  canPlanRoute: boolean
  departureHint?: string
  onPlanRoute: (routeId: string) => void
}

const relationLabels: Record<string, string> = {
  familiarity: '熟悉度', trust: '信任', affinity: '亲近', respect: '尊重', fear: '畏惧', resentment: '怨恨',
}

export function SceneWorldGraph({ state, loading, viewError, canPlanRoute, departureHint, onPlanRoute }: SceneWorldGraphProps) {
  const player = state?.player_id ? state.entities[state.player_id] : undefined
  const currentPlaceEntity = player?.location_id ? state?.entities[player.location_id] : undefined
  const currentPlace = currentPlaceEntity && (currentPlaceEntity.kind === 'place' || currentPlaceEntity.kind === 'location')
    ? currentPlaceEntity
    : undefined
  const placeName = currentPlace?.name?.trim() || '当前位置'
  const cast = currentPlace ? Object.values(state?.entities ?? {}).filter((entity) => (
    (entity.kind === 'character' || entity.kind === 'person') && entity.id !== player?.id
    && entity.location_id === currentPlace.id && entity.status !== 'dead' && entity.status !== 'retired'
  )) : []
  const visibleCast = new Map(cast.map((person) => [person.id, person]))
  const projectedRoutes = state?.travel_options
  const routes = Array.isArray(projectedRoutes) ? projectedRoutes : undefined
  const routeProjectionAvailable = routes !== undefined
  const relationProjectionAvailable = Array.isArray(state?.relations)
  const relations = relationProjectionAvailable
    ? state.relations.filter((relation) => relation.from_id === player?.id && visibleCast.has(relation.to_id))
    : []
  const daySeconds = state?.calendar?.day_seconds ?? 0
  const yearDays = state?.calendar?.year_days ?? 0
  if (loading || viewError || !state || !player || !currentPlace) {
    return <div className="scene-world-graph" aria-label="已知路线与人物关系">
      {loading && <p className="play-scene-empty" role="status">正在读取已知路线与人物关系…</p>}
      {!loading && viewError && <p className="play-scene-empty" role="status">当前分支的路线与关系暂时无法读取。</p>}
      {!loading && !viewError && state && !player && <p className="play-scene-empty">当前角色资料未载入，暂时无法显示路线与关系。</p>}
      {!loading && !viewError && !state && <p className="play-scene-empty">角色视角的场景投影尚未载入。</p>}
      {!loading && !viewError && state && player && !currentPlace && <p className="play-scene-empty">当前地点资料未载入，暂时无法绘制本地路线。</p>}
    </div>
  }

  return <div className="scene-world-graph" aria-label="已知路线与人物关系">
    <section className="play-scene-section scene-world-routes" aria-labelledby="scene-world-routes-heading">
      <h3 id="scene-world-routes-heading">可走路线 <small>{routes?.length ?? '—'}</small></h3>
      {departureHint && <p className="play-scene-empty">{departureHint}</p>}
      {!routeProjectionAvailable && <p className="play-scene-empty">当前场景未提供已知路线投影。</p>}
      {routes && routes.length === 0 && <p className="play-scene-empty">当前没有从这里显示为可走的路线。</p>}
      {routes && routes.length > 0 && <div className="scene-world-route-map" role="group" aria-label={`从${placeName}出发的已知路线`}>
        <div className="scene-world-place scene-world-origin">
          <span>当前位置</span>
          <strong>{placeName}</strong>
        </div>
        <ol className="scene-world-route-list">
          {routes.map((route) => {
            const destination = route.destination_name?.trim() || '未命名地点'
            const duration = exactTick(route.travel_ticks)
            const durationLabel = duration === null
              ? '耗时待确认'
              : formatWorldDuration(duration, daySeconds, yearDays)
            return <li className="scene-world-route" key={route.route_id} aria-label={`${placeName} 到 ${destination}，${durationLabel}`}>
              <span className="scene-world-route-arrow" aria-hidden="true">→</span>
              <div className="scene-world-place scene-world-destination">
                <strong>{destination}</strong>
                <small>{durationLabel}</small>
              </div>
              <button type="button" className="play-scene-observe" disabled={!canPlanRoute || duration === null || duration <= 0n}
                aria-label={`规划前往${destination}`} title={canPlanRoute ? '核对路线与授权时间后再提交' : '当前不能规划新行程'}
                onClick={() => onPlanRoute(route.route_id)}>规划行程</button>
            </li>
          })}
        </ol>
      </div>}
    </section>

    <section className="play-scene-section scene-world-relations" aria-labelledby="scene-world-relations-heading">
      <h3 id="scene-world-relations-heading">我与在场人物的关系</h3>
      {!relationProjectionAvailable && <p className="play-scene-empty">当前场景未提供关系投影。</p>}
      {relationProjectionAvailable && relations.length === 0 && <p className="play-scene-empty">当前没有与你相关的在场人物关系记录。</p>}
      {relationProjectionAvailable && relations.length > 0 && <>
        <p className="scene-world-relation-note">箭头表示记录方向，仅显示你对当前在场人物的已知记录。</p>
        <ul className="scene-world-relation-list">
          {relations.map((relation, index) => {
            const person = visibleCast.get(relation.to_id)
            if (!person) return null
            const dimensions = Object.entries(relation.dimensions ?? {})
            return <li className="scene-world-relation" key={relation.id ?? `${relation.from_id}:${relation.to_id}:${index}`}>
              <div className="scene-world-relation-edge" role="img" aria-label={`${player.name?.trim() || '我'} 指向 ${person.name?.trim() || '未命名人物'}`}>
                <span className="scene-world-relation-person">{player.name?.trim() || '我'}</span>
                <span className="scene-world-relation-arrow" aria-hidden="true">→</span>
                <span className="scene-world-relation-person">{person.name?.trim() || '未命名人物'}</span>
              </div>
              {(relation.label || dimensions.length > 0) && <div className="scene-world-relation-values">
                {relation.label && <span className="scene-world-relation-label">{relation.label}</span>}
                {dimensions.map(([key, value]) => <span className="scene-world-relation-value" key={key}>
                  {relationLabels[key] ?? key} {String(value)}
                </span>)}
              </div>}
            </li>
          })}
        </ul>
      </>}
    </section>
  </div>
}

function exactTick(value: unknown): bigint | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) ? BigInt(value) : null
  return typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : null
}
