import { useState } from 'react'
import { api, type NpcRehearsalReport, type NpcRehearsalScenario } from '../api/client'
import { errorMessage } from '../utils'

const scenarios: Record<NpcRehearsalScenario, string> = {
  inducement: '利诱',
  threat: '资源威胁',
  family_conflict: '亲友与目标冲突',
  rebuttal: '证据反驳',
  scarcity: '补给短缺',
  absence: '合作方长期失联',
}

const choices: Record<NpcRehearsalReport['result']['choice'], string> = {
  accept: '接受', refuse: '拒绝', negotiate: '议价或协商', defer: '暂缓',
}

export function NpcRehearsalPanel({ worldId, branchId, actorId, stateVersion }: {
  worldId: string
  branchId: string
  actorId: string
  stateVersion: string
}) {
  const [scenario, setScenario] = useState<NpcRehearsalScenario>('inducement')
  const [report, setReport] = useState<NpcRehearsalReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function rehearse() {
    if (busy) return
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      setReport(await api.rehearseNpc(worldId, {
        request_id: crypto.randomUUID(), branch_id: branchId, actor_id: actorId,
        expected_state_version: stateVersion, scenario,
      }))
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy(false)
    }
  }

  return <details className="character-create-field">
    <summary>人物试演</summary>
    <p>从当前存档设定生成一份假设报告。试演使用已配置的作者辅助模型，世界时间不推进。</p>
    <label className="character-create-field">
      <span>试演情境</span>
      <select value={scenario} disabled={busy} onChange={(event) => setScenario(event.target.value as NpcRehearsalScenario)}>
        {Object.entries(scenarios).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select>
    </label>
    <div className="character-preview-actions">
      <button className="button button-primary" type="button" disabled={busy} onClick={() => void rehearse()}>
        {busy ? '正在试演…' : '开始试演'}
      </button>
    </div>
    {error && <p role="alert">{error}</p>}
    {report && <section aria-label="人物试演报告" aria-live="polite">
      <h3>{scenarios[report.scenario]} · {choices[report.result.choice]}</h3>
      <p>{report.scenario_text}</p>
      <blockquote>{report.result.reply}</blockquote>
      <dl className="character-preview-fields">
        <div className="character-preview-field"><dt>模型给出的动机</dt><dd>{report.result.motivation}</dd></div>
        <div className="character-preview-field"><dt>仍需核对</dt><dd>{report.result.uncertainty}</dd></div>
      </dl>
      {report.references.length > 0 && <details>
        <summary>试演引用的角色资料</summary>
        <ul>{report.references.map((reference) => <li key={reference.id}>{reference.claim}</li>)}</ul>
      </details>}
      <small>取自状态版本 {report.state_version} · 世界第 {report.tick} 秒</small>
    </section>}
  </details>
}
