import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const server = await createServer({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  configFile: false,
  appType: 'custom',
  server: { middlewareMode: true, ws: false },
})
after(() => server.close())
const { InvestigationJournal } = await server.ssrLoadModule('/src/components/InvestigationJournal.tsx')
const { investigationRecordElementId } = await server.ssrLoadModule('/src/investigationLinks.ts')

const observation = {
  id: 'observation', kind: 'observation', claim: '检修记录上写着值班站在运营。', received_tick: 72_300,
  investigation: {
    source_name: '检修档案柜', subject_id: 'depot', predicate: 'entity_status', effective_tick: -3_600,
  },
}
const claim = {
  id: 'claim', kind: 'rumor', claim: '我记得车站那天歇业了。', summary: '车站当日情况', received_tick: 72_030,
  investigation: {
    source_name: '调度员林乔', subject_id: 'depot', predicate: 'entity_status', effective_tick: '-3600',
  },
}
const assessment = {
  id: 'assessment', kind: 'observation', claim: '复核记录', summary: '两条来源对当日运营的说法不同。', received_tick: 72_600,
  investigation: {
    subject_id: 'depot', predicate: 'entity_status', effective_tick: -3_600,
    finding: 'conflict', observation_id: observation.id, claim_id: claim.id,
  },
}
const caseFile = {
  id: 'file', case_id: 'depot-case', title: '值班站案', status: 'open', revision: 1,
  updated_tick: 72_600, evidence_refs: [],
}

function render(props = {}) {
  return renderToStaticMarkup(createElement(InvestigationJournal, {
    anchors: [], caseFiles: [caseFile], knowledge: [], playerId: 'player',
    projectionAvailable: true, loading: false, viewError: false, inputHasText: false,
    busy: false, authorMode: false, daySeconds: 72_000, yearDays: 360,
    onPrepareAction: assert.fail,
    ...props,
  }))
}

test('renders actor-known observation and original testimony with source and both clocks', () => {
  const html = render({ knowledge: [observation, claim, { id: 'memory', kind: 'memory', claim: '普通聊天记录' }] })
  assert.match(html, /先选定描述同一对象与时点的观察和证词进行复核/)
  assert.match(html, /来源 · 检修档案柜.*检修记录上写着值班站在运营/s)
  assert.match(html, /来源 · 调度员林乔.*我记得车站那天歇业了/s)
  assert.match(html, /所述时间 · 世界历第 0 年第 360 日 19:00:00/)
  assert.match(html, /收录时间 · 世界历第 1 年第 2 日 00:05:00/)
  assert.match(html, /<button[^>]+disabled[^>]*>准备证据复核<\/button>/)
  assert.doesNotMatch(html, /普通聊天记录|已附加的证据复核/)
})

test('only offers confirmed review records for selection and keeps attached sources linked by ID', () => {
  const secondClaim = {
    ...claim, id: 'second-claim', claim: '当晚我照常到值班站交接。',
    investigation: { ...claim.investigation, source_name: '值班员周红' },
  }
  const second = {
    ...assessment, id: 'second-assessment', summary: '值班证词与检修记录一致。',
    investigation: { ...assessment.investigation, claim_id: secondClaim.id, finding: 'corroborated' },
  }
  const html = render({
    caseFiles: [{ ...caseFile, evidence_refs: [assessment.id] }],
    knowledge: [observation, claim, assessment, secondClaim, second],
  })
  assert.match(html, /aria-label="已附加的证据复核".*来源相互冲突/s)
  assert.match(html, /已有来源互相冲突/)
  assert.match(html, /<option value="second-assessment">来源相互印证/)
  assert.doesNotMatch(html, /<option value="assessment">|<option value="claim">/)
  assert.match(html, /<button[^>]+disabled[^>]*><svg[^>]*>.*?<\/svg>准备附加复核/s)
  for (const id of [observation.id, claim.id]) {
    const anchor = investigationRecordElementId(id)
    assert.ok(html.includes(`href="#${anchor}"`))
    assert.ok(html.includes(`id="${anchor}" tabindex="-1"`))
  }
})

test('describes missing evidence and only exposes original sources present in the actor projection', () => {
  const html = render({ knowledge: [observation] })
  assert.match(html, /先观察相关地点、检查物件或询问在场证人/)
  assert.doesNotMatch(html, /准备附加复核|准备证据复核|结案结果/)
  const partial = render({ knowledge: [assessment], caseFiles: [{ ...caseFile, evidence_refs: [assessment.id] }] })
  assert.match(partial, /观察的原始记录当前不可见.*说法的原始记录当前不可见/s)
  assert.doesNotMatch(partial, /href="#investigation-record-(observation|claim)"|调度员林乔/)
})

test('shows authorized closed outcome and an editable correction only when the projection permits it', () => {
  const closed = {
    ...caseFile, status: 'closed', outcome: 'disconfirmed', public_accusation: true, suspect_name: '调度员林乔', closed_tick: 72_600,
  }
  const html = render({ caseFiles: [closed] })
  assert.match(html, /结案结果.*现有证据排除了所选嫌疑人.*被指认人 · 调度员林乔/s)
  assert.match(html, /与对方同场时，可以当面撤回指认/)
  assert.doesNotMatch(html, /准备当面更正|准备附加复核/)
  assert.match(render({ caseFiles: [{ ...closed, can_correct_public_accusation: true }] }), /准备当面更正/)
  const corrected = render({ caseFiles: [{ ...closed, can_correct_public_accusation: true, public_accusation_corrected_tick: 72_900 }] })
  assert.match(corrected, /已当面更正.*世界历第 1 年第 2 日 00:15:00.*给对方决定如何回应/s)
  assert.doesNotMatch(corrected, /准备当面更正/)
})

test('unavailable projection hides evidence and action drafts', () => {
  const html = render({ viewError: true, knowledge: [observation, claim, assessment] })
  assert.match(html, /无法读取当前分支的授权视角/)
  assert.doesNotMatch(html, /检修档案柜|调度员林乔|准备证据复核|值班站案/)
})
