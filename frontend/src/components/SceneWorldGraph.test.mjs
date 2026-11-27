import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const root = fileURLToPath(new URL('../..', import.meta.url))
const server = await createServer({
  root,
  configFile: false,
  appType: 'custom',
  server: { middlewareMode: true, ws: false },
})
after(() => server.close())
const { SceneWorldGraph } = await server.ssrLoadModule('/src/components/SceneWorldGraph.tsx')

const projection = {
  player_id: 'player',
  calendar: { day_seconds: 86_400, year_days: 365 },
  entities: {
    player: { id: 'player', kind: 'character', name: '旅人', location_id: 'village', status: 'active' },
    local: { id: 'local', kind: 'character', name: '同伴', location_id: 'village', status: 'active' },
    distant: { id: 'distant', kind: 'character', name: '远方居民', location_id: 'town', status: 'active' },
    village: { id: 'village', kind: 'place', name: '村口' },
    bridge: { id: 'bridge', kind: 'place', name: '东桥' },
  },
  travel_options: [{ route_id: 'east', destination_name: '东桥', travel_ticks: '90000' }],
  relations: [
    { id: 'outgoing', from_id: 'player', to_id: 'local', dimensions: { trust: 7, familiarity: 2 } },
    { id: 'incoming', from_id: 'local', to_id: 'player', label: '对旅人的关系' },
    { id: 'distant', from_id: 'player', to_id: 'distant', label: '远方关系' },
  ],
}

function render(state, props = {}) {
  return renderToStaticMarkup(createElement(SceneWorldGraph, {
    state,
    loading: false,
    viewError: false,
    canPlanRoute: true,
    onPlanRoute() {},
    ...props,
  }))
}

test('draws local known routes and only authorized outgoing relations to present people', () => {
  const html = render(projection)
  for (const text of [
    '从村口出发的已知路线', '当前位置', '村口', '东桥', '1 日 1 小时', '规划前往东桥',
    '旅人 指向 同伴', '信任 7', '熟悉度 2',
  ]) assert.ok(html.includes(text), text)
  assert.doesNotMatch(html, /远方居民|对旅人的关系|远方关系/)
  assert.match(html, /role="group" aria-label="从村口出发的已知路线"/)
  assert.match(html, /role="img" aria-label="旅人 指向 同伴"/)
})

test('missing projections show a text fallback without drawing a guessed graph', () => {
  const html = render({ ...projection, travel_options: undefined, relations: undefined })
  assert.match(html, /当前场景未提供已知路线投影/)
  assert.match(html, /当前场景未提供关系投影/)
  assert.doesNotMatch(html, /scene-world-route-map|scene-world-relation-list|规划前往/)
})

test('empty projections report no known route or local relationship record', () => {
  const html = render({ ...projection, travel_options: [], relations: [] })
  assert.match(html, /当前没有从这里显示为可走的路线/)
  assert.match(html, /当前没有与你相关的在场人物关系记录/)
  assert.doesNotMatch(html, /当前场景未提供/)
})

test('a route without an exact duration is readable but cannot be planned', () => {
  const html = render({ ...projection, travel_options: [{ route_id: 'east', destination_name: '东桥', travel_ticks: '1.5' }] })
  assert.match(html, /耗时待确认/)
  assert.match(html, /规划前往东桥/)
  assert.match(html, /disabled=""/)
})
