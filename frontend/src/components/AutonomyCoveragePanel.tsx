import { useEffect, useRef, useState, type FormEvent } from 'react'
import { fetchAutonomyCoverage, type AutonomyCoverageCategory, type AutonomyCoverageResult } from '../api/client'
import { errorMessage } from '../utils'
import './AutonomyCoveragePanel.css'

const MAX_SEQUENCE = 9_223_372_036_854_775_807n
const MAX_VISIBLE_PERIODS = 12

type ScopedValue<T> = { key: string; value: T }
type CoverageLoad = { key: string; after: string; headSequence: string; result: AutonomyCoverageResult }
type CoverageFailure = { key: string; after: string; message: string }

export function AutonomyCoveragePanel({
  scopeKey,
  worldId,
  branchId,
  branchHeadSequence,
}: {
  scopeKey: string
  worldId: string
  branchId: string
  branchHeadSequence: string
}) {
  const [afterState, setAfterState] = useState<ScopedValue<string>>({ key: scopeKey, value: '' })
  const [load, setLoad] = useState<CoverageLoad | null>(null)
  const [failure, setFailure] = useState<CoverageFailure | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const generation = useRef(0)
  const request = useRef<AbortController | null>(null)
  const after = afterState.key === scopeKey ? afterState.value : ''
  const normalizedAfter = after.trim()
  const afterSyntaxValid = normalizedAfter === '' || /^(0|[1-9]\d{0,18})$/.test(normalizedAfter)
  const headSequenceValid = /^(0|[1-9]\d{0,18})$/.test(branchHeadSequence)
  const afterInRange = headSequenceValid && afterSyntaxValid && (normalizedAfter === '' || (
    BigInt(normalizedAfter) <= MAX_SEQUENCE && BigInt(normalizedAfter) <= BigInt(branchHeadSequence)
  ))
  const visibleLoad = load?.key === scopeKey && load.after === normalizedAfter
    && load.headSequence === branchHeadSequence ? load : null
  const visibleFailure = failure?.key === scopeKey && failure.after === normalizedAfter ? failure : null
  const isBusy = busyKey === scopeKey
  const rangeError = !headSequenceValid
    ? '当前分支序号无效，请重新载入历史后重试。'
    : !afterSyntaxValid
    ? '请输入规范的非负整数序号，例如 0 或 123。'
    : !afterInRange ? '起始序号不能超过当前分支末端或有符号 64 位整数范围。' : ''

  useEffect(() => () => {
    generation.current += 1
    request.current?.abort()
  }, [scopeKey])

  async function loadCoverage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!afterInRange || isBusy) return
    const selectedAfter = normalizedAfter
    const controller = new AbortController()
    request.current?.abort()
    request.current = controller
    const currentGeneration = ++generation.current
    setBusyKey(scopeKey)
    setLoad(null)
    setFailure(null)
    try {
      const result = await fetchAutonomyCoverage(
        worldId,
        branchId,
        selectedAfter || undefined,
        controller.signal,
      )
      if (!controller.signal.aborted && generation.current === currentGeneration) {
        setLoad({ key: scopeKey, after: selectedAfter, headSequence: branchHeadSequence, result })
      }
    } catch (reason) {
      if (!controller.signal.aborted && generation.current === currentGeneration) {
        setFailure({ key: scopeKey, after: selectedAfter, message: errorMessage(reason) })
      }
    } finally {
      if (generation.current === currentGeneration) {
        request.current = null
        setBusyKey(null)
      }
    }
  }

  function downloadDetail() {
    if (!visibleLoad) return
    const blob = new Blob([JSON.stringify(visibleLoad.result.detail, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `autonomy-coverage-${worldId.slice(0, 8)}-${branchId.slice(0, 8)}-${visibleLoad.result.through_sequence}.json`
    document.body.append(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000)
  }

  return <section className="autonomy-coverage-panel" aria-labelledby="autonomy-coverage-title">
    <div className="autonomy-coverage-heading">
      <h2 id="autonomy-coverage-title">自主运行覆盖</h2>
      {visibleLoad && <button type="button" className="button button-secondary button-small" onClick={downloadDetail}>下载作者明细 JSON</button>}
    </div>
    <p className="autonomy-coverage-intro">按已提交事件查看人物和组织的活跃窗口、行动机会与后果。</p>
    <form className="autonomy-coverage-form" onSubmit={(event) => void loadCoverage(event)}>
      <label htmlFor={`coverage-after-${scopeKey}`}>起始提交序号（可留空）</label>
      <div className="autonomy-coverage-controls">
        <input id={`coverage-after-${scopeKey}`} inputMode="numeric" autoComplete="off" spellCheck={false}
          maxLength={19}
          value={after} aria-invalid={Boolean(rangeError)} aria-describedby={`coverage-after-help-${scopeKey}`}
          disabled={isBusy} onChange={(event) => {
            const value = event.currentTarget.value
            setAfterState({ key: scopeKey, value })
            setLoad(null)
            setFailure(null)
          }} />
        <button type="submit" className="button button-secondary button-small" disabled={isBusy || !afterInRange}>
          {isBusy ? '正在汇总…' : '读取覆盖统计'}
        </button>
      </div>
      <small id={`coverage-after-help-${scopeKey}`}>留空从此分支的起始状态统计；填写完整提交点后，统计其后的提交事件。当前分支末端为 #{branchHeadSequence}。</small>
      {rangeError && <p className="autonomy-coverage-error" role="alert">{rangeError}</p>}
      {visibleFailure && <p className="autonomy-coverage-error" role="alert">覆盖统计读取失败：{visibleFailure.message}</p>}
      {isBusy && <p className="autonomy-coverage-status" role="status" aria-live="polite">正在重放并汇总已提交事件…</p>}
    </form>
    {visibleLoad && <CoverageReport result={visibleLoad.result} />}
  </section>
}

function CoverageReport({ result }: { result: AutonomyCoverageResult }) {
  const periods = result.summary.periods
  const visiblePeriods = periods.slice(-MAX_VISIBLE_PERIODS)
  const start = result.summary.window.start_tick
  const endExclusive = result.summary.window.end_tick_exclusive
  return <div className="autonomy-coverage-report" aria-live="polite">
    <div className="autonomy-coverage-report-meta">
      <span>提交范围 #{result.from_sequence}–#{result.through_sequence}</span>
      <span>世界时间 {start}–{Math.max(start, endExclusive - 1)} tick</span>
      <span>{result.summary.ledger.committed_event_count.toLocaleString()} 条已提交事件</span>
    </div>
    <div className="autonomy-coverage-categories">
      <CoverageCategory title="人物" category={result.summary.actors} />
      <CoverageCategory title="组织" category={result.summary.organizations} />
    </div>
    {periods.length > 0 && <section className="autonomy-coverage-periods" aria-labelledby="autonomy-coverage-periods-title">
      <div className="autonomy-coverage-subheading"><h3 id="autonomy-coverage-periods-title">分期活动</h3>
        {periods.length > MAX_VISIBLE_PERIODS && <span>显示最近 {MAX_VISIBLE_PERIODS} / {periods.length} 期</span>}
      </div>
      <div className="autonomy-coverage-table-wrap"><table>
        <thead><tr><th>世界时间</th><th>人物：符合资格 / 行动 / 未知</th><th>组织：符合资格 / 行动 / 未知</th></tr></thead>
        <tbody>{visiblePeriods.map((period) => <tr key={`${period.start_tick}-${period.end_tick_exclusive}`}>
          <th scope="row">{period.start_tick}–{Math.max(period.start_tick, period.end_tick_exclusive - 1)}</th>
          <td>{period.actors.eligible_count} / {period.actors.action_count} / {period.actors.no_action_reason_unknown_count}</td>
          <td>{period.organizations.eligible_count} / {period.organizations.action_count} / {period.organizations.no_action_reason_unknown_count}</td>
        </tr>)}</tbody>
      </table></div>
    </section>}
  </div>
}

function CoverageCategory({ title, category }: { title: string; category: AutonomyCoverageCategory }) {
  const metrics: [string, number][] = [
    ['符合资格实体', category.eligible_count],
    ['符合资格实体 tick', category.eligible_ticks],
    ['日常推进', category.routine_count],
    ['复核', category.review_count],
    ['决策', category.decision_count],
    ['已记录机会', category.opportunity_count],
    ['已提交行动', category.action_count],
    ['状态效果', category.effect_count],
    ['环境检查', category.ambient_check_count],
    ['环境接受', category.ambient_accepted_count],
    ['环境后续', category.ambient_followup_count],
    ['符合资格但无已记录行动', category.eligible_without_recorded_action_count],
    ['无行动且原因未知', category.no_action_reason_unknown_count],
    ['符合资格但无已记录机会', category.eligible_without_recorded_opportunity_count],
    ['无机会且原因未知', category.no_recorded_opportunity_reason_unknown_count],
    ['有机会但无已记录行动', category.opportunity_without_recorded_action_count],
    ['资格信息不完整', category.eligibility_incomplete_count],
  ]
  return <section className="autonomy-coverage-category" aria-label={`${title}自主运行统计`}>
    <h3>{title}</h3>
    <dl>{metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value.toLocaleString()}</dd></div>)}</dl>
  </section>
}
