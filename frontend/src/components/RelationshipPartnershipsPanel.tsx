import { useState } from 'react'
import {
  type CharacterEntity,
  type OwnRelationshipDisclosure,
  type OwnRelationshipPartnership,
  type RelationshipDisclosureOption,
  type RelationshipDisclosureRecipientOption,
} from '../api/client'
import { Icon } from './Icon'

const partnershipStatusLabels: Record<OwnRelationshipPartnership['status'], string> = {
  pending: '等待回应',
  active: '伴侣关系已建立',
  declined: '已拒绝',
  withdrawn: '已撤回',
  ended: '已结束',
}

export function RelationshipPartnershipsPanel({
  partnerships: rawPartnerships,
  proposalTargetIds: rawProposalTargetIds,
  disclosureOptions: rawDisclosureOptions,
  receivedDisclosures: rawReceivedDisclosures,
  entities,
  playerId,
  projectionAvailable,
  loading,
  viewError,
  canPrepareAction,
  onPrepareAction,
}: {
  partnerships?: OwnRelationshipPartnership[] | null
  proposalTargetIds?: string[] | null
  disclosureOptions?: RelationshipDisclosureOption[] | null
  receivedDisclosures?: OwnRelationshipDisclosure[] | null
  entities: Record<string, CharacterEntity>
  playerId?: string
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const [selectedPartnerId, setSelectedPartnerId] = useState('')
  const [proposalTerms, setProposalTerms] = useState('')
  const [partnershipTermDrafts, setPartnershipTermDrafts] = useState<Record<string, string>>({})
  const [selectedDisclosurePartnershipId, setSelectedDisclosurePartnershipId] = useState('')
  const [selectedDisclosureRecipientId, setSelectedDisclosureRecipientId] = useState('')
  const dataAvailable = Array.isArray(rawPartnerships)
  const validRecords = dataAvailable ? rawPartnerships.filter(isPartnership) : []
  const partnerships = validRecords.filter((item) => item.proposer_id === playerId || item.partner_id === playerId)
  const invalidCount = dataAvailable ? rawPartnerships.length - validRecords.length : 0
  const pendingCount = partnerships.filter((item) => item.status === 'pending').length
  const activeCount = partnerships.filter((item) => item.status === 'active').length
  const disclosureOptionsAvailable = Array.isArray(rawDisclosureOptions)
  const receivedDisclosuresAvailable = Array.isArray(rawReceivedDisclosures)
  const disclosureOptions = disclosureOptionsAvailable
    ? rawDisclosureOptions.filter(isDisclosureOption).flatMap((option) => {
      const partnership = partnerships.find((item) => item.partnership_id === option.partnership_id)
      if (!partnership || partnership.status !== 'active' || revisionKey(option.expected_revision) !== revisionKey(partnership.revision)) return []
      const oppositeId = partnership.proposer_id === playerId ? partnership.partner_id : partnership.proposer_id
      if (option.opposite_participant_id !== oppositeId) return []
      const recipients = [...new Map(option.recipient_options.filter(isDisclosureRecipientOption)
        .filter((recipient) => ![partnership.proposer_id, partnership.partner_id].includes(recipient.recipient_id))
        .map((recipient) => [recipient.recipient_id, recipient])).values()]
      return [{ ...option, recipient_options: recipients }]
    })
    : []
  const disclosureOptionCount = disclosureOptionsAvailable ? rawDisclosureOptions.length : 0
  const invalidDisclosureOptionCount = disclosureOptionsAvailable ? disclosureOptionCount - disclosureOptions.length : 0
  const receivedDisclosures = receivedDisclosuresAvailable
    ? rawReceivedDisclosures.filter(isOwnRelationshipDisclosure)
    : []
  const invalidDisclosureCount = receivedDisclosuresAvailable
    ? rawReceivedDisclosures.length - receivedDisclosures.length
    : 0
  const proposalTargetsAvailable = Array.isArray(rawProposalTargetIds)
  const proposalPartners = proposalTargetsAvailable
    ? [...new Set(rawProposalTargetIds.filter((value): value is string => typeof value === 'string' && value.trim().length > 0))]
      .flatMap((targetId) => {
        const entity = entities[targetId]
        return entity && entity.id === targetId && (entity.kind === 'character' || entity.kind === 'person')
          ? [entity]
          : []
      })
    : []
  const selectedPartner = proposalPartners.find((entity) => entity.id === selectedPartnerId)
  const proposalAllowed = canPrepareAction && dataAvailable && proposalTargetsAvailable && Boolean(selectedPartner)
  const selectedDisclosureOption = disclosureOptions.find((item) => item.partnership_id === selectedDisclosurePartnershipId)
    ?? disclosureOptions[0]
  const selectedDisclosureRecipient = selectedDisclosureOption?.recipient_options.find(
    (item) => item.recipient_id === selectedDisclosureRecipientId,
  ) ?? selectedDisclosureOption?.recipient_options[0]

  return <details className="shipment-panel promise-panel" aria-label="我的伴侣关系">
    <summary className="shipment-panel-summary">
      <span><Icon name="users" size={14} />伴侣关系</span>
      <small>{loading
        ? '读取中'
        : !projectionAvailable
          ? '角色投影未载入'
          : !dataAvailable
            ? '关系投影未提供'
            : `${partnerships.length} 条记录 · ${pendingCount} 条待回应 · ${activeCount} 段已建立`}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取伴侣关系…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的伴侣关系。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !dataAvailable && <p className="shipment-empty">当前投影未提供伴侣关系记录。</p>}
    {!loading && !viewError && projectionAvailable && dataAvailable && <>
      {invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条伴侣关系记录无法读取，已隐藏或排除。</p>}
      {invalidDisclosureOptionCount > 0 && <p className="shipment-empty" role="alert">部分关系告知选项无法与当前有效关系匹配，已隐藏。</p>}
      <div className="shipment-list">
        {proposalPartners.length > 0 && <article className="shipment-card">
          <header className="shipment-card-heading">
            <strong>提出伴侣关系</strong>
            <small>双方明确接受后才会建立</small>
          </header>
          <div className="promise-actions">
            <label>
              <span className="shipment-empty">选择可提议的在场人物</span>
              <select
                aria-label="选择要提出伴侣关系的在场人物"
                value={selectedPartnerId}
                onChange={(event) => setSelectedPartnerId(event.target.value)}
              >
                <option value="">选择人物</option>
                {proposalPartners.map((person) => <option key={person.id} value={person.id}>
                  {person.name?.trim() || '未命名人物'} · {person.id.slice(0, 8)}
                </option>)}
              </select>
            </label>
            <label className="author-simple-field">
              <span>私下交往条件（可选）</span>
              <textarea value={proposalTerms} onChange={(event) => setProposalTerms(event.target.value)}
                maxLength={400} rows={2} placeholder="例如，双方都可以和别人约会" />
            </label>
            <button
              type="button"
              className="button button-secondary button-small"
              onClick={() => {
                if (!selectedPartner) return
                const terms = proposalTerms.trim()
                const proposal = `我明确向 ${selectedPartner.name?.trim() || '这位在场人物'}（人物 ID ${selectedPartner.id}）提议成为伴侣。`
                onPrepareAction(terms ? `${proposal}\n提议条件：${terms}` : proposal)
              }}
              disabled={!proposalAllowed}
              title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
            >准备提出</button>
          </div>
        </article>}
        {activeCount > 0 && disclosureOptionsAvailable && <article className="shipment-card">
          <header className="shipment-card-heading">
            <strong>定向告知</strong>
            <small>只记录你自己的陈述</small>
          </header>
          {disclosureOptions.length === 0
            ? <p className="shipment-empty">当前没有可告知的同场可见人物。</p>
            : <>
              <div className="promise-actions">
                <label className="author-simple-field">
                  <span>选择要告知的伴侣关系</span>
                  <select
                    aria-label="选择要告知的伴侣关系"
                    value={selectedDisclosureOption?.partnership_id ?? ''}
                    onChange={(event) => {
                      setSelectedDisclosurePartnershipId(event.target.value)
                      setSelectedDisclosureRecipientId('')
                    }}
                  >
                    {disclosureOptions.map((option) => <option key={option.partnership_id} value={option.partnership_id}>
                      我与 {option.opposite_participant_name} 的伴侣关系
                    </option>)}
                  </select>
                </label>
                <label className="author-simple-field">
                  <span>同场接收人</span>
                  <select
                    aria-label="选择接收关系告知的人物"
                    value={selectedDisclosureRecipient?.recipient_id ?? ''}
                    onChange={(event) => setSelectedDisclosureRecipientId(event.target.value)}
                    disabled={!selectedDisclosureOption?.recipient_options.length}
                  >
                    {selectedDisclosureOption?.recipient_options.map((recipient) => <option key={recipient.recipient_id} value={recipient.recipient_id}>
                      {recipient.recipient_name}
                    </option>)}
                  </select>
                  {!selectedDisclosureOption?.recipient_options.length && <small>当前没有可告知的同场可见人物。</small>}
                </label>
              </div>
              <button
                type="button"
                className="button button-secondary button-small"
                onClick={() => {
                  if (selectedDisclosureOption && selectedDisclosureRecipient) {
                    onPrepareAction(`我只私下告诉 ${selectedDisclosureRecipient.recipient_name}，我和 ${selectedDisclosureOption.opposite_participant_name} 已经是伴侣。`)
                  }
                }}
                disabled={!canPrepareAction || !selectedDisclosureRecipient}
                title={canPrepareAction ? '填入行动输入框；不会自动发送' : '清空输入框或等待当前请求完成后再操作'}
              >填入关系告知草稿</button>
            </>}
        </article>}
        {activeCount > 0 && !disclosureOptionsAvailable && <p className="shipment-empty">当前投影未提供关系告知选项。</p>}
        {partnerships.length === 0
          ? <p className="shipment-empty">目前没有伴侣关系记录。</p>
          : partnerships.map((partnership) => {
            const currentAuthorId = partnership.current_proposal_author_id ?? partnership.proposer_id
            const currentRecipientId = partnership.current_proposal_recipient_id
              ?? (currentAuthorId === partnership.proposer_id ? partnership.partner_id : partnership.proposer_id)
            const currentAuthorIsPlayer = currentAuthorId === playerId
            const currentRecipientIsPlayer = currentRecipientId === playerId
            const currentAuthorName = currentAuthorIsPlayer
              ? '你'
              : displayName(entities[currentAuthorId]?.name, '对方资料未载入')
            const currentRecipientName = currentRecipientIsPlayer
              ? '你'
              : displayName(entities[currentRecipientId]?.name, '对方资料未载入')
            const endedStatus = partnership.status === 'declined'
              || partnership.status === 'withdrawn'
              || partnership.status === 'ended'
            const actionAllowed = canPrepareAction && Boolean(partnership.partnership_id)
            const revision = formatRevision(partnership.revision)
            const isPending = partnership.status === 'pending'
            const canRevise = isPending && currentAuthorIsPlayer && partnership.can_revise === true
            const canCounter = isPending && currentRecipientIsPlayer && partnership.can_counter === true
            const canAccept = isPending && currentRecipientIsPlayer && partnership.can_accept !== false
            const canDecline = isPending && currentRecipientIsPlayer && partnership.can_decline !== false
            const canWithdraw = isPending && currentAuthorIsPlayer && partnership.can_withdraw !== false
            const canRequestResponse = isPending && currentAuthorIsPlayer && currentRecipientId !== playerId
              && partnership.can_request_response !== false
            const reviseDraftKey = `${partnership.partnership_id}:${revision}:revise`
            const counterDraftKey = `${partnership.partnership_id}:${revision}:counter`
            const reviseDraft = partnershipTermDrafts[reviseDraftKey] ?? ''
            const counterDraft = partnershipTermDrafts[counterDraftKey] ?? ''
            const reviseDraftError = partnershipTermsError(reviseDraft)
            const counterDraftError = partnershipTermsError(counterDraft)
            return <article className="shipment-card promise-card" key={partnership.partnership_id}>
              <header className="shipment-card-heading">
                <span className={`shipment-status ${endedStatus ? 'shipment-status-spoiled' : ''}`}>
                  {partnershipStatusLabels[partnership.status]}
                </span>
                <small>本轮条件由{currentAuthorName}提出</small>
              </header>
              <dl className="shipment-parties">
                <div><dt>本轮条件提出方</dt><dd>{currentAuthorName}</dd></div>
                <div><dt>本轮回应方</dt><dd>{currentRecipientName}</dd></div>
                <div><dt>提议版本</dt><dd>{revision}</dd></div>
                <div><dt>提出 tick</dt><dd><bdi>{formatTick(partnership.proposed_tick)}</bdi></dd></div>
                <div><dt>更新 tick</dt><dd><bdi>{formatTick(partnership.updated_tick)}</bdi></dd></div>
              </dl>
              {!!partnership.proposal_terms?.length && <div>
                <strong>当前版本条件</strong>
                <ul>{partnership.proposal_terms.map((term, index) => <li key={index}>{term}</li>)}</ul>
              </div>}
              <p className="occupation-id">记录 ID · {partnership.partnership_id}</p>
              {canRevise && <div className="promise-actions">
                <label className="author-simple-field">
                  <span>修改本轮完整条件</span>
                  <textarea
                    value={reviseDraft}
                    onChange={(event) => setPartnershipTermDrafts((current) => ({ ...current, [reviseDraftKey]: event.target.value }))}
                    maxLength={400}
                    rows={3}
                    aria-label="填写要替换当前伴侣提议的完整新条件"
                    aria-describedby={`partnership-revise-help-${partnership.partnership_id}`}
                    placeholder="填写将替代当前全部条件的新内容"
                  />
                  <small id={`partnership-revise-help-${partnership.partnership_id}`}>
                    请完整写出全部新条件；提交后会替换当前整组条件，不会自动沿用旧条件。最多 400 字。
                  </small>
                  {reviseDraftError && <small role="alert">{reviseDraftError}</small>}
                </label>
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确把伴侣关系提议 ${partnership.partnership_id}（版本 ${revision}）修改为以下完整的新条件。\n提议条件：${reviseDraft.trim()}`)}
                  disabled={!actionAllowed || !reviseDraft.trim() || Boolean(reviseDraftError)}
                  aria-label="准备提交替换当前条件的完整修改"
                  title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
                >准备修改条件</button>
              </div>}
              {canCounter && <div className="promise-actions">
                <label className="author-simple-field">
                  <span>提出一组完整反条件</span>
                  <textarea
                    value={counterDraft}
                    onChange={(event) => setPartnershipTermDrafts((current) => ({ ...current, [counterDraftKey]: event.target.value }))}
                    maxLength={400}
                    rows={3}
                    aria-label="填写要提交的完整伴侣关系反条件"
                    aria-describedby={`partnership-counter-help-${partnership.partnership_id}`}
                    placeholder="填写将替代当前全部条件的新内容"
                  />
                  <small id={`partnership-counter-help-${partnership.partnership_id}`}>
                    请完整写出全部反条件；提交后会替换当前整组条件，不会自动沿用旧条件。最多 400 字。
                  </small>
                  {counterDraftError && <small role="alert">{counterDraftError}</small>}
                </label>
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确对伴侣关系提议 ${partnership.partnership_id}（版本 ${revision}）提出以下完整反条件。\n提议条件：${counterDraft.trim()}`)}
                  disabled={!actionAllowed || !counterDraft.trim() || Boolean(counterDraftError)}
                  aria-label="准备提交完整伴侣关系反条件"
                  title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
                >准备提出反条件</button>
              </div>}
              {canAccept && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确接受伴侣关系提议 ${partnership.partnership_id}，版本 ${revision}。`)}
                  disabled={!actionAllowed}
                >准备接受</button>
              </div>}
              {canDecline && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确拒绝伴侣关系提议 ${partnership.partnership_id}，版本 ${revision}。`)}
                  disabled={!actionAllowed}
                >准备拒绝</button>
              </div>}
              {canWithdraw && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确撤回伴侣关系提议 ${partnership.partnership_id}，版本 ${revision}。`)}
                  disabled={!actionAllowed}
                >准备撤回</button>
              </div>}
              {canRequestResponse && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`请让${currentRecipientName}自行回应我当前提出的伴侣关系条件，记录 ${partnership.partnership_id}，版本 ${revision}。`)}
                  disabled={!actionAllowed}
                >请求对方回应</button>
              </div>}
              {partnership.status === 'active' && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确结束伴侣关系 ${partnership.partnership_id}，版本 ${revision}。`)}
                  disabled={!actionAllowed}
                >准备结束关系</button>
              </div>}
            </article>
          })}
      </div>
      {partnerships.length > 0 && <p className="shipment-empty">按钮只会填入行动草稿；不会自动发送或替任何一方作决定。</p>}
      {receivedDisclosuresAvailable && <article className="shipment-card">
        <header className="shipment-card-heading">
          <strong>你收到的关系告知</strong>
          <small>个人记忆 · 历史陈述</small>
        </header>
        {invalidDisclosureCount > 0 && <p className="shipment-empty" role="alert">有 {invalidDisclosureCount} 条关系告知无法读取，已隐藏。</p>}
        {receivedDisclosures.length === 0
          ? <p className="shipment-empty">{invalidDisclosureCount > 0 ? '没有可显示的有效告知记录。' : '目前没有收到关系告知。'}</p>
          : <div className="shipment-list" aria-label="收到的关系告知记录">
            {receivedDisclosures.map((disclosure) => {
              const discloserName = displayName(entities[disclosure.discloser_id]?.name, '来源未载入')
              const oppositeName = displayName(entities[disclosure.opposite_participant_id]?.name, '对方资料未载入')
              const locationName = displayName(entities[disclosure.location_id]?.name, '地点未载入')
              return <article className="shipment-card promise-card" key={disclosure.record_id} aria-label="收到的关系告知记录">
                <header className="shipment-card-heading">
                  <span className="shipment-status">过去陈述</span>
                  <small>来自 {discloserName}</small>
                </header>
                <p className="promise-text">{displayDisclosureStatement(disclosure, oppositeName)}</p>
                <p className="shipment-empty">这是当时听到的个人陈述，不代表关系目前仍然成立，也不说明另一方的态度或同意。</p>
                <dl className="shipment-parties">
                  <div><dt>当时提及</dt><dd>{oppositeName}</dd></div>
                  <div><dt>地点</dt><dd>{locationName}</dd></div>
                  <div><dt>发生时点</dt><dd><bdi>{formatTick(disclosure.disclosed_tick)}</bdi></dd></div>
                </dl>
              </article>
            })}
          </div>}
      </article>}
    </>}
  </details>
}

function isPartnership(value: unknown): value is OwnRelationshipPartnership {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const partnership = value as Partial<OwnRelationshipPartnership>
  return typeof partnership.partnership_id === 'string'
    && partnership.partnership_id.trim().length > 0
    && typeof partnership.proposer_id === 'string'
    && partnership.proposer_id.trim().length > 0
    && typeof partnership.partner_id === 'string'
    && partnership.partner_id.trim().length > 0
    && partnership.proposer_id !== partnership.partner_id
    && isPartnershipCurrentRolesValid(partnership)
    && arePartnershipFlagsValid(partnership)
    && (partnership.status === 'pending'
      || partnership.status === 'active'
      || partnership.status === 'declined'
      || partnership.status === 'withdrawn'
      || partnership.status === 'ended')
    && isWorldTick(partnership.proposed_tick)
    && isWorldTick(partnership.updated_tick)
    && isRevision(partnership.revision)
    && (partnership.proposal_terms === undefined || (Array.isArray(partnership.proposal_terms)
      && partnership.proposal_terms.length <= 6
      && partnership.proposal_terms.every((term) => typeof term === 'string' && term.trim().length > 0 && term.length <= 400)))
}

function isPartnershipCurrentRolesValid(partnership: Partial<OwnRelationshipPartnership>): boolean {
  const authorId = partnership.current_proposal_author_id
  const recipientId = partnership.current_proposal_recipient_id
  if (authorId === undefined && recipientId === undefined) return true
  const participants = [partnership.proposer_id, partnership.partner_id]
  return typeof authorId === 'string' && participants.includes(authorId)
    && typeof recipientId === 'string' && participants.includes(recipientId)
    && authorId !== recipientId
}

function arePartnershipFlagsValid(partnership: Partial<OwnRelationshipPartnership>): boolean {
  return [partnership.can_revise, partnership.can_counter, partnership.can_accept,
    partnership.can_decline, partnership.can_withdraw, partnership.can_request_response]
    .every((value) => value === undefined || typeof value === 'boolean')
}

function partnershipTermsError(value: string): string {
  const term = value.trim()
  return term.length > 400 ? '完整新条件最多 400 字。' : ''
}

function isDisclosureOption(value: unknown): value is RelationshipDisclosureOption {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const option = value as Partial<RelationshipDisclosureOption>
  return typeof option.partnership_id === 'string'
    && option.partnership_id.trim().length > 0
    && isRevision(option.expected_revision)
    && typeof option.opposite_participant_id === 'string'
    && option.opposite_participant_id.trim().length > 0
    && typeof option.opposite_participant_name === 'string'
    && option.opposite_participant_name.trim().length > 0
    && Array.isArray(option.recipient_options)
}

function isDisclosureRecipientOption(value: unknown): value is RelationshipDisclosureRecipientOption {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const recipient = value as Partial<RelationshipDisclosureRecipientOption>
  return typeof recipient.recipient_id === 'string'
    && recipient.recipient_id.trim().length > 0
    && typeof recipient.recipient_name === 'string'
    && recipient.recipient_name.trim().length > 0
}

function isOwnRelationshipDisclosure(value: unknown): value is OwnRelationshipDisclosure {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const disclosure = value as Partial<OwnRelationshipDisclosure>
  return typeof disclosure.record_id === 'string'
    && disclosure.record_id.trim().length > 0
    && typeof disclosure.statement === 'string'
    && disclosure.statement.trim().length > 0
    && disclosure.statement.length <= 600
    && disclosure.source === 'past_statement_by_discloser'
    && typeof disclosure.discloser_id === 'string'
    && disclosure.discloser_id.trim().length > 0
    && typeof disclosure.opposite_participant_id === 'string'
    && disclosure.opposite_participant_id.trim().length > 0
    && typeof disclosure.location_id === 'string'
    && disclosure.location_id.trim().length > 0
    && isWorldTick(disclosure.disclosed_tick)
}

function isWorldTick(value: unknown): value is string | number {
  if (typeof value === 'string') return /^-?\d+$/.test(value.trim())
  return typeof value === 'number' && Number.isSafeInteger(value)
}

function isRevision(value: unknown): value is string | number {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 1
  if (typeof value !== 'string' || !/^\d+$/.test(value.trim())) return false
  try {
    return BigInt(value.trim()) >= 1n
  } catch {
    return false
  }
}

function formatTick(value: string | number): string {
  if (typeof value === 'string') return /^-?\d+$/.test(value.trim()) ? value.trim() : '无法精确读取'
  return Number.isSafeInteger(value) ? String(value) : '无法精确读取'
}

function formatRevision(value: string | number): string {
  return isRevision(value) ? String(value) : '无法读取'
}

function revisionKey(value: unknown): string | null {
  if (!isRevision(value)) return null
  try {
    return BigInt(String(value).trim()).toString()
  } catch {
    return null
  }
}

function displayName(value: string | null | undefined, fallback: string): string {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : fallback
}

function displayDisclosureStatement(disclosure: OwnRelationshipDisclosure, oppositeName: string): string {
  const statement = disclosure.statement.trim()
  const prefix = 'I am in an active relationship with '
  if (statement.startsWith(prefix)) return `“我当时表示与 ${oppositeName} 处于伴侣关系。”`
  const safeStatement = statement
    .replaceAll(disclosure.opposite_participant_id, oppositeName)
    .replace(/\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b/gi, '未载入的人物')
  return `“${safeStatement}”`
}
