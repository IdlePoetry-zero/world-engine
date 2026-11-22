import type { AccusationRepairOfferView, AvailableCaseAnchor, InvestigationCaseFileView, PlayerKnowledgeView } from '../api/client'
import { Icon } from './Icon'
import './InvestigationJournal.css'

type Props = {
  anchors: AvailableCaseAnchor[]
  caseFiles: InvestigationCaseFileView[]
  repairOffers?: AccusationRepairOfferView[]
  playerId?: string
  knowledge: PlayerKnowledgeView[]
  projectionAvailable: boolean
  loading: boolean
  viewError: boolean
  inputHasText: boolean
  busy: boolean
  authorMode: boolean
  onPrepareAction: (text: string) => void
}

export function InvestigationJournal({
  anchors,
  caseFiles,
  repairOffers = [],
  playerId,
  knowledge,
  projectionAvailable,
  loading,
  viewError,
  inputHasText,
  busy,
  authorMode,
  onPrepareAction,
}: Props) {
  const knowledgeById = new Map(knowledge.map((record) => [record.id, record]))
  const actionDisabled = busy || inputHasText

  return (
    <section className="investigation-journal" aria-label="调查日志">
      <div className="investigation-journal-heading">
        <div className="investigation-journal-title">
          <Icon name="book" size={15} />
          <strong>调查日志</strong>
          <span>{caseFiles.length} 案卷</span>
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
            {repairOffers.map((offer) => <article className="investigation-case" key={offer.offer_id}>
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
                  title={existing ? (existing.status === 'open' ? '此角色已有这宗案卷' : '此角色已有已结案记录') : inputHasText ? '先发送或清空现有输入' : '将一条可编辑的行动文字填入 GM 输入框'}
                  onClick={() => onPrepareAction(`请为线索「${anchor.title}」建立一份我的私人调查档案。`)}
                >
                  {existing ? (existing.status === 'open' ? '已建档' : '已结案') : <><Icon name="plus" size={13} />建立案卷</>}
                </button>
              </article>
            })}
          </section>}

          {caseFiles.length > 0 && <section className="investigation-case-list" aria-label="我的私人案卷">
            {caseFiles.map((file) => {
              const attachedCount = file.evidence_refs.length
              return <article className="investigation-case" key={file.id}>
                <div className="investigation-case-topline">
                  <strong>{file.title}</strong>
                  <span className={`investigation-status investigation-status-${file.status}`}>
                    {file.status === 'open' ? '进行中' : '已结案'}
                  </span>
                </div>
                {file.brief && <p className="investigation-case-brief">{file.brief}</p>}
                <div className="investigation-case-progress">
                  <span>复核记录 {attachedCount} 条</span>
                  <span>修订 {file.revision}</span>
                  {file.status === 'open' && <span>最近更新 · {file.updated_tick}</span>}
                </div>
                {attachedCount > 0 && <ul className="investigation-evidence-list" aria-label="已附加的证据复核">
                  {file.evidence_refs.map((reference) => {
                    const record = knowledgeById.get(reference)
                    return <li key={reference}>
                      <Icon name="check" size={12} />
                      <span>{record?.summary || record?.claim || `复核记录 ${reference.slice(0, 8)}`}</span>
                    </li>
                  })}
                </ul>}
                {file.status === 'closed' ? (
                  <div className="investigation-outcome">
                    <span>结案结果</span>
                    <strong>{outcomeLabel(file.outcome)}</strong>
                    {file.public_accusation && <small>曾公开指认</small>}
                    {file.public_accusation_corrected_tick != null && <small>已当面更正</small>}
                  </div>
                ) : <div className="investigation-case-actions">
                  <button
                    className="investigation-action"
                    type="button"
                    disabled={actionDisabled}
                    title={inputHasText ? '先发送或清空现有输入' : '将一条可编辑的行动文字填入 GM 输入框'}
                    onClick={() => onPrepareAction(`请将我最近完成且尚未附加的私人证据复核记录，加入「${file.title}」案卷。`)}
                  >
                    <Icon name="plus" size={13} />附加复核
                  </button>
                  <button
                    className="investigation-action investigation-action-muted"
                    type="button"
                    disabled={actionDisabled}
                    title={inputHasText ? '先发送或清空现有输入' : '将一条可编辑的行动文字填入 GM 输入框'}
                    onClick={() => onPrepareAction(`我暂不指认任何人，也不公开指认；请将「${file.title}」案卷以未决结论结案。`)}
                  >
                    暂不指认并结案
                  </button>
                </div>}
              </article>
            })}
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
            ? '快捷操作会切换到角色行动，并填入可编辑文字；不会直接执行作者指令。'
            : '快捷操作只会填入 GM 输入框；检查或修改文字后再发送。'}
      </div>}
    </section>
  )
}

function outcomeLabel(outcome: InvestigationCaseFileView['outcome']) {
  return ({
    supported: '现有独立证据支持该判断',
    disconfirmed: '现有证据排除了所选嫌疑人',
    contested: '证据同时支持与排除，结论有争议',
    unresolved: '证据不足，暂未形成结论',
  } as Record<string, string>)[outcome ?? ''] ?? '未记录结论'
}
