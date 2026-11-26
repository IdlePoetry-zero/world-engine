import { useState } from 'react'
import type { AccusationRepairOfferView, AvailableCaseAnchor, InvestigationCaseFileView, PlayerKnowledgeView } from '../api/client'
import { formatWorldRelativeTick } from '../worldTime'
import { investigationRecordElementId } from '../investigationLinks'
import { Icon } from './Icon'
import './InvestigationJournal.css'

type Props = {
  anchors: AvailableCaseAnchor[]
  caseFiles: InvestigationCaseFileView[]
  repairOffers?: AccusationRepairOfferView[]
  playerId?: string
  knowledge: PlayerKnowledgeView[]
  daySeconds?: number
  yearDays?: number
  projectionAvailable: boolean
  loading: boolean
  viewError: boolean
  inputHasText: boolean
  busy: boolean
  authorMode: boolean
  onOpenCase: (id: string, title: string) => void
  onPrepareAction: (text: string) => void
}

// oxlint-disable-next-line react/only-export-components -- PlayPage uses the same IDs for receipt links.
export function InvestigationJournal({
  anchors,
  caseFiles,
  repairOffers = [],
  playerId,
  knowledge,
  daySeconds = 0,
  yearDays = 0,
  projectionAvailable,
  loading,
  viewError,
  inputHasText,
  busy,
  authorMode,
  onOpenCase,
  onPrepareAction,
}: Props) {
  const [observationId, setObservationId] = useState('')
  const [claimId, setClaimId] = useState('')
  const knowledgeById = new Map(knowledge.map((record) => [record.id, record]))
  const assessments = knowledge.filter((record) => record.investigation?.finding)
  const reviewedPairs = new Set(assessments.map(({ investigation }) => `${investigation?.observation_id}:${investigation?.claim_id}`))
  const primaryRecords = knowledge.filter((record) => record.investigation?.predicate && !record.investigation.finding)
  const observations = primaryRecords.filter((record) => record.kind === 'observation')
  const claims = primaryRecords.filter((record) => ['claim', 'rumor', 'belief'].includes(record.kind ?? ''))
  const observation = observations.find((record) => record.id === observationId)
  const matchingClaims = observation ? claims.filter((record) => canCompare(observation, record, reviewedPairs)) : []
  const claim = matchingClaims.find((record) => record.id === claimId)
  const canReview = observations.some((record) => claims.some((candidate) => canCompare(record, candidate, reviewedPairs)))
  const visibleEvidence = knowledge.filter((record) => record.investigation?.predicate || record.investigation?.finding)
  const actionDisabled = busy || inputHasText
  const describeTick = (tick: string | number) => {
    if ((typeof tick === 'number' && !Number.isSafeInteger(tick)) || !/^-?\d+$/.test(String(tick))) return '时间未知'
    return formatWorldRelativeTick(BigInt(tick), daySeconds, yearDays)
  }

  return (
    <section className="investigation-journal" aria-label="调查日志">
      <div className="investigation-journal-heading">
        <div className="investigation-journal-title">
          <Icon name="book" size={15} />
          <strong>调查日志</strong>
          {projectionAvailable && !loading && !viewError && <span>{caseFiles.length} 案卷</span>}
        </div>
        <span className="investigation-journal-scope">仅当前角色可见</span>
      </div>
      {loading ? <div className="investigation-journal-empty">正在读取角色调查记录…</div> : viewError ? (
        <div className="investigation-journal-empty">无法读取当前分支的授权视角，调查日志暂不可用。</div>
      ) : !projectionAvailable ? (
        <div className="investigation-journal-empty">载入当前角色视角后，这里会显示可用线索与私人案卷。</div>
      ) : (
        <div className="investigation-journal-content">
          {repairOffers.length > 0 && <section className="investigation-case-list" aria-label="更正后的解释">
            {repairOffers.map((offer) => <article className="investigation-case" key={offer.offer_id}
              id={investigationRecordElementId(offer.offer_id)} tabIndex={-1}>
              <div className="investigation-case-topline">
                <strong>{offer.accuser_id === playerId ? '我交出的解释' : '我收到的解释'}</strong>
                <span>{offer.status === 'accepted' ? '已接受' : offer.status === 'declined' ? '已拒绝'
                  : offer.last_operation === 'defer' ? '暂缓回应' : '等待回应'}</span>
              </div>
              <p className="investigation-case-brief">{offer.statement}</p>
              {offer.status === 'pending' && <div className="investigation-case-actions">
                {offer.accuser_id === playerId && <button type="button" className="investigation-action" disabled={actionDisabled}
                  onClick={() => onPrepareAction(`请让收信人自行回应我交出的解释 ${offer.offer_id}。`)}>准备请求回应</button>}
                {offer.suspect_id === playerId && (['接受', '拒绝', '暂缓回应'] as const).map((choice) => <button
                  type="button" className="investigation-action" disabled={actionDisabled} key={choice}
                  onClick={() => onPrepareAction(`我明确选择${choice}收到的解释 ${offer.offer_id}。`)}>{choice}</button>)}
              </div>}
            </article>)}
          </section>}
          {anchors.length > 0 && <section className="investigation-anchor-list" aria-label="当前线索">
            {anchors.map((anchor) => {
              const existing = caseFiles.find((file) => file.case_id === anchor.case_id)
              return <article className="investigation-anchor" key={anchor.anchor_id}>
                <div className="investigation-card-copy">
                  <strong>{anchor.title}</strong>
                  {anchor.brief && <p>{anchor.brief}</p>}
                </div>
                <button
                  className="investigation-action"
                  type="button"
                  disabled={actionDisabled || Boolean(existing)}
                  title={existing ? (existing.status === 'open' ? '此角色已有这宗案卷' : '此角色已有已结案记录') : inputHasText ? '先发送或清空现有输入' : '为选中的线索登记私人案卷'}
                  onClick={() => onOpenCase(anchor.anchor_id, anchor.title)}
                >
                  {existing ? (existing.status === 'open' ? '已建档' : '已结案') : <><Icon name="plus" size={13} />建立案卷</>}
                </button>
              </article>
            })}
          </section>}

          {caseFiles.length > 0 && <section className="investigation-case-list" aria-label="我的私人案卷">
            {caseFiles.map((file) => <CaseRecord key={file.id} file={file} assessments={assessments}
              knowledgeById={knowledgeById} canReview={canReview}
              describeTick={describeTick} actionDisabled={actionDisabled} onPrepareAction={onPrepareAction} />)}
          </section>}

          {visibleEvidence.length > 0 && <section className="investigation-case-list" aria-label="我的证据记录">
            <strong className="investigation-section-label">我的证据记录</strong>
            {observations.length > 0 && claims.length > 0 && <div className="investigation-case">
              <div className="investigation-evidence-controls">
                <label>亲历观察<select value={observation?.id ?? ''} disabled={actionDisabled}
                  onChange={(event) => { setObservationId(event.target.value); setClaimId('') }}>
                  <option value="">选择要复核的观察</option>
                  {observations.map((record) => <option key={record.id} value={record.id}>{evidenceOption(record)}</option>)}
                </select></label>
                <label>证词或说法<select value={claim?.id ?? ''} disabled={actionDisabled || matchingClaims.length === 0}
                  onChange={(event) => setClaimId(event.target.value)}>
                  <option value="">选择描述同一对象、同一时点的说法</option>
                  {matchingClaims.map((record) => <option key={record.id} value={record.id}>{evidenceOption(record)}</option>)}
                </select></label>
                <button className="investigation-action" type="button" disabled={actionDisabled || !observation || !claim}
                  onClick={() => {
                    if (observation && claim) onPrepareAction(`请复核我的观察记录 ${observation.id} 与说法记录 ${claim.id}，比较两条独立来源是否一致。`)
                  }}>准备证据复核</button>
              </div>
              {observation && matchingClaims.length === 0 && <p className="investigation-case-brief">现有说法中没有可与这条观察比较的新记录。</p>}
            </div>}
            {visibleEvidence.map((record) => <article className="investigation-case" key={record.id}
              id={investigationRecordElementId(record.id)} tabIndex={-1}>
              <EvidenceRecord record={record} knowledgeById={knowledgeById} describeTick={describeTick} />
            </article>)}
          </section>}

          {anchors.length === 0 && caseFiles.length === 0 && <div className="investigation-journal-empty">
            当前地点没有可开启的调查线索。发现并接近相关线索后，它会显示在这里。
          </div>}
          {anchors.length > 0 && caseFiles.length === 0 && <div className="investigation-journal-hint">在此地点发现的线索可建立为私人案卷。</div>}
        </div>
      )}
      {projectionAvailable && !loading && !viewError && <div className="investigation-journal-note">
        {inputHasText
          ? '发送或清空当前输入后，才能使用案卷快捷文字。'
          : authorMode
            ? '快捷操作会切换到角色行动并填入草稿。检查或修改后再发送。'
            : '准备的草稿会填入 GM 输入框。检查或修改后再发送。'}
      </div>}
    </section>
  )
}

type EvidenceProps = {
  knowledgeById: Map<string, PlayerKnowledgeView>
  describeTick: (tick: string | number) => string
}

function CaseRecord({ file, assessments, knowledgeById, canReview, describeTick, actionDisabled, onPrepareAction }: EvidenceProps & {
  file: InvestigationCaseFileView
  assessments: PlayerKnowledgeView[]
  canReview: boolean
  actionDisabled: boolean
  onPrepareAction: (text: string) => void
}) {
  const [selectedId, setSelectedId] = useState('')
  const available = assessments.filter((record) => !file.evidence_refs.includes(record.id))
  const selected = available.find((record) => record.id === selectedId)
  const correctedTick = file.public_accusation_corrected_tick
  const hasConflict = file.evidence_refs.some((id) => knowledgeById.get(id)?.investigation?.finding === 'conflict')

  return <article className="investigation-case" id={investigationRecordElementId(file.id)} tabIndex={-1}>
    <div className="investigation-case-topline">
      <strong>{file.title}</strong>
      <span className={`investigation-status investigation-status-${file.status}`}>
        {file.status === 'open' ? '进行中' : '已结案'}
      </span>
    </div>
    {file.brief && <p className="investigation-case-brief">{file.brief}</p>}
    <div className="investigation-case-progress">
      <span>复核记录 {file.evidence_refs.length} 条</span>
      <span>最近更新 · {describeTick(file.updated_tick)}</span>
    </div>
    {file.evidence_refs.length > 0 && <ul className="investigation-evidence-list" aria-label="已附加的证据复核">
      {file.evidence_refs.map((reference) => {
        const record = knowledgeById.get(reference)
        return <li key={reference}>
          {record ? <EvidenceRecord record={record} knowledgeById={knowledgeById} describeTick={describeTick} attached />
            : <span>这条已附加复核的详情当前不可见。</span>}
        </li>
      })}
    </ul>}
    {file.status === 'closed' ? <>
      <div className="investigation-outcome">
        <span>结案结果</span>
        <strong>{outcomeLabel(file.outcome)}</strong>
        {file.suspect_name && <small>{file.public_accusation ? '被指认人' : '所选嫌疑人'} · {file.suspect_name}</small>}
        {file.public_accusation && <small>曾公开指认</small>}
        {file.closed_tick != null && <small>{describeTick(file.closed_tick)}</small>}
        {correctedTick != null && <small>已当面更正 · {describeTick(correctedTick)}</small>}
      </div>
      {file.public_accusation && file.outcome === 'disconfirmed' && <p className="investigation-case-brief">
        {correctedTick != null ? '你可以向被指认人解释曾经误读的证据，并给对方决定如何回应的机会。'
          : '证据排除了被指认人。与对方同场时，可以当面撤回指认。'}
      </p>}
      {file.can_correct_public_accusation && correctedTick == null && <div className="investigation-case-actions">
        <button type="button" className="investigation-action" disabled={actionDisabled}
          onClick={() => onPrepareAction(`我现在当面撤回并更正自己在「${file.title}」案卷 ${file.id} 的公开指认。`)}>准备当面更正</button>
      </div>}
      {file.repair_offer_id && <a href={`#${investigationRecordElementId(file.repair_offer_id)}`}>查看已交出的解释与回应</a>}
    </> : <>
      <p className="investigation-case-brief">
        {hasConflict ? '已有来源互相冲突。可以继续找独立记录，查清差异来自何处。'
          : file.evidence_refs.length > 0 ? '已附加复核。核对来源与所述时间后，可以继续取证或记录私人结论。'
            : available.length > 0 ? '已有复核可以选入案卷。请核对两条来源与本案的关联。'
              : canReview ? '先选定描述同一对象与时点的观察和证词进行复核。'
                : '先观察相关地点、检查物件或询问在场证人，寻找可以互相核对的独立来源。'}
        {' '}公开指认可能影响对方对你的信任。
      </p>
      <div className="investigation-case-actions">
        {available.length > 0 && <div className="investigation-evidence-controls">
          <label>要附加的复核<select value={selected?.id ?? ''} disabled={actionDisabled}
            onChange={(event) => setSelectedId(event.target.value)}>
            <option value="">选择一条复核记录</option>
            {available.map((record) => <option key={record.id} value={record.id}>{evidenceOption(record)}</option>)}
          </select></label>
          <button className="investigation-action" type="button" disabled={actionDisabled || !selected}
            onClick={() => {
              if (selected) onPrepareAction(`请将复核记录 ${selected.id} 附加到我自己的「${file.title}」案卷 ${file.id}。`)
            }}><Icon name="plus" size={13} />准备附加复核</button>
        </div>}
        <button className="investigation-action investigation-action-muted" type="button" disabled={actionDisabled}
          onClick={() => onPrepareAction(`请将我的「${file.title}」案卷 ${file.id} 以未决结论结案。我暂不指认任何人。`)}>
          准备以未决结论结案
        </button>
      </div>
    </>}
  </article>
}

function EvidenceRecord({ record, knowledgeById, describeTick, attached }: EvidenceProps & {
  record: PlayerKnowledgeView
  attached?: boolean
}) {
  const evidence = record.investigation
  const label = evidence?.finding ? findingLabel(evidence.finding) : attached ? '已附加复核'
    : record.kind === 'observation' ? '亲历观察' : '证词或说法'
  return <div className="investigation-evidence-record">
    <div className="investigation-case-topline">
      <strong>{label}</strong>
      {evidence?.source_name && <span className="investigation-journal-scope">来源 · {evidence.source_name}</span>}
    </div>
    <p className="investigation-case-brief">{evidence?.finding || attached ? record.summary || record.claim : record.claim}</p>
    <div className="investigation-case-progress">
      {evidence?.effective_tick != null && <span>所述时间 · {describeTick(evidence.effective_tick)}</span>}
      {record.received_tick != null && <span>收录时间 · {describeTick(record.received_tick)}</span>}
    </div>
    {evidence?.finding && <ul className="investigation-evidence-list" aria-label="复核来源">
      {([['观察', evidence.observation_id], ['说法', evidence.claim_id]] as const).map(([label, id]) => {
        const source = id ? knowledgeById.get(id) : undefined
        return <li key={label}>
          {source ? <a href={`#${investigationRecordElementId(source.id)}`} aria-label={`查看${label}记录：${source.claim}`}>
            {label}{source.investigation?.source_name ? ` · ${source.investigation.source_name}` : ''} · {source.summary || source.claim}
          </a> : <span>{label}的原始记录当前不可见。</span>}
        </li>
      })}
    </ul>}
  </div>
}

function canCompare(observation: PlayerKnowledgeView, claim: PlayerKnowledgeView, reviewedPairs: Set<string>) {
  const observed = observation.investigation
  const alleged = claim.investigation
  return Boolean(observed?.subject_id && observed.subject_id === alleged?.subject_id
    && observed.predicate === alleged.predicate && observed.effective_tick != null && alleged.effective_tick != null
    && String(observed.effective_tick) === String(alleged.effective_tick)
    && !reviewedPairs.has(`${observation.id}:${claim.id}`))
}

function evidenceOption(record: PlayerKnowledgeView) {
  const evidence = record.investigation
  return [evidence?.finding && findingLabel(evidence.finding), evidence?.source_name,
    (record.summary || record.claim).slice(0, 100)].filter(Boolean).join(' · ')
}

function findingLabel(finding: 'corroborated' | 'conflict') {
  return finding === 'corroborated' ? '来源相互印证' : '来源相互冲突'
}

function outcomeLabel(outcome: InvestigationCaseFileView['outcome']) {
  return ({
    supported: '现有独立证据支持该判断',
    disconfirmed: '现有证据排除了所选嫌疑人',
    contested: '证据同时支持与排除，结论有争议',
    unresolved: '证据不足，暂未形成结论',
  } as Record<string, string>)[outcome ?? ''] ?? '未记录结论'
}
