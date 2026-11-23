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
const { PlaySceneCard } = await server.ssrLoadModule('/src/components/PlaySceneCard.tsx')

const projection = {
  player_id: 'player',
  calendar: { day_seconds: 72_000, year_days: 360 },
  entities: {
    player: { id: 'player', kind: 'character', name: '行者', location_id: 'market', status: 'active' },
    clerk: { id: 'clerk', kind: 'character', name: '店员', location_id: 'market', status: 'active' },
    distant: { id: 'distant', kind: 'character', name: '远方的人', location_id: 'station', status: 'active' },
    market: { id: 'market', kind: 'place', name: '市集' },
    station: { id: 'station', kind: 'place', name: '车站' },
  },
  resources: {},
  relations: [],
  knowledge: [],
  goals: [
    { id: 'active', title: '找到失物', status: 'active' },
    { id: 'closed', title: '归还借书', status: 'succeeded' },
  ],
  available_case_anchors: [{ anchor_id: 'note', case_id: 'case', title: '未签名的便条', brief: '便条落在柜台边。' }],
  scene_opportunities: [{
    opportunity_id: 'sale',
    title: '摊位打烊前询价',
    contact_name: '店员',
    place_name: '市集',
    deadline_tick: '72300',
    known_cost: '往返至少半小时',
    possible_impact: '可能在打烊前留下报价',
    draft: { mode: 'roleplay', text: '这件商品怎么卖？' },
  }],
  travel_options: [{ route_id: 'local', destination_name: '车站', travel_ticks: '72600' }],
}

function render(state, props = {}) {
  return renderToStaticMarkup(createElement(PlaySceneCard, {
    state,
    loading: false,
    viewError: false,
    canPrepareQuery: true,
    canPrepareAction: true,
    canPlanRoute: true,
    onPrepareDraft() {},
    onPlanRoute() {},
    onOpenInvestigation() {},
    onOpenJourney() {},
    onOpenComposer() {},
    ...props,
  }))
}

test('scene displays current cast, opportunities, investigation and goals with world-calendar times', () => {
  const html = render(projection)
  for (const text of ['市集', '与店员交谈', '摊位打烊前询价', '准备交谈', '往返至少半小时',
    '可能在打烊前留下报价', '世界历第 1 年第 2 日 00:05:00', '未签名的便条',
    '准备建立案卷', '找到失物', '车站', '1 日 10 分钟']) assert.ok(html.includes(text), text)
  assert.ok(!html.includes('远方的人'))
  assert.ok(!html.includes('归还借书'))
})

test('unavailable projection suppresses every actionable entry', () => {
  const html = render(projection, { viewError: true })
  assert.match(html, /当前分支的场景暂时无法读取/)
  for (const text of ['市集', '准备建立案卷', '摊位打烊前询价', '找到失物', '车站']) assert.ok(!html.includes(text), text)
})

test('older projections have usable empty states, and projecting an existing case links to its journal', () => {
  const oldState = { ...projection, scene_opportunities: undefined, available_case_anchors: undefined, goals: [] }
  assert.match(render(oldState), /眼前没有已知的机会.*观察周围.*目前没有进行中的个人目标/s)
  const filed = render({ ...projection, case_files: [{ case_id: 'case' }] })
  assert.match(filed, /查看调查日志/)
  assert.doesNotMatch(filed, /准备建立案卷/)
})

test('travel shows the known destination and arrival without showing previous local actions', () => {
  const traveling = render({
    ...projection,
    scene_opportunities: [],
    own_active_travel: { status: 'in_transit', route_id: 'local', to_place_id: 'station', arrival_tick: '72600' },
  })
  assert.match(traveling, /前往车站.*世界历第 1 年第 2 日 00:10:00/s)
  assert.match(traveling, /查看行程进度/)
  assert.doesNotMatch(traveling, /店员|准备建立案卷|可走路线/)
})

test('a present character shows public hours, the player’s own promises and dated known changes', () => {
  const html = render({
    ...projection,
    scene_npc_readback: {
      clerk: {
        public_schedule: [{ activity: 'work', place_name: '市集', start_second: 28_800, duration_seconds: 3_600, every_days: 2 }],
        known_changes: [{ claim: '班次已经调整。', received_tick: '72300' }],
      },
      distant: { public_schedule: [], known_changes: [{ claim: '远方消息', received_tick: '0' }] },
    },
    own_promises: [{ promise_id: 'promise', counterparty_id: 'clerk', status: 'active', text: '替店员找回钥匙' }],
  })
  assert.match(html, /通常的公开作息.*市集.*工作.*每 2 日 08:00 起.*持续 1 小时/s)
  assert.match(html, /与我的约定.*替店员找回钥匙.*我获知的近况.*<q>班次已经调整。<\/q>.*世界历第 1 年第 2 日 00:05:00/s)
  assert.doesNotMatch(html, /远方消息/)
})
