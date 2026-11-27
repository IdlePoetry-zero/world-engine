import { useState } from 'react'
import type { InstitutionalCreditContext, InstitutionalCreditContract, InstitutionalCreditCounterparty, InstitutionalCreditInstitution, InstitutionalFinanceContext, InstitutionalFinanceCurrency, InstitutionalFinanceOrganization } from '../api/client'
import { Icon } from './Icon'
import type { SceneCreditAction } from '../api/client'

const MAX_CREDIT_ROWS = 32
const MAX_CURRENCIES_PER_ROW = 16
const MAX_FINANCE_ASSETS = 32
const MAX_FINANCE_DUE_IDS = 32
const MAX_EXTENSION_OPTIONS = 32
const MAX_EXTENSION_ROWS = 8
const MIN_TICK = -(1n << 63n)
const MAX_TICK = (1n << 63n) - 1n
const MAX_MINOR_AMOUNT = (10n ** 30n) - 1n
const RESOURCE_ID_PATTERN = /^[A-Za-z][A-Za-z0-9_.-]{0,159}$/
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

type NormalizedCreditContext = {
  value: InstitutionalCreditContext
  invalidRows: boolean
}

export function InstitutionalCreditPanel({
  credit: rawCredit,
  finance: rawFinance,
  projectionLoading,
  canPrepareAction,
  canPrepareQuery,
  onPrepareAction,
  onPrepareQuery,
  onPreviewResolution,
}: {
  credit?: InstitutionalCreditContext | null
  finance?: InstitutionalFinanceContext | null
  projectionLoading: boolean
  canPrepareAction: boolean
  canPrepareQuery: boolean
  onPrepareAction: (text: string) => void
  onPrepareQuery: (text: string) => void
  onPreviewResolution: (action: SceneCreditAction, label: string) => void
}) {
  const [paymentInputs, setPaymentInputs] = useState<Record<string, string>>({})
  const [lenderId, setLenderId] = useState('')
  const [counterpartyId, setCounterpartyId] = useState('')
  const [currencyId, setCurrencyId] = useState('')
  const [principalInput, setPrincipalInput] = useState('')
  const [totalInput, setTotalInput] = useState('')
  const [acceptedThroughInput, setAcceptedThroughInput] = useState('')
  const [maturityInput, setMaturityInput] = useState('')

  const normalized = normalizeCreditContext(rawCredit)
  if (!normalized) return null
  const credit = normalized.value
  const institutions = credit.represented_institutions
  const counterparties = credit.visible_credit_counterparties
  const contracts = credit.institutional_credit_contracts
  const lenderOptions = institutions.filter((institution) => counterparties.some((row) => row.represented_organization_id === institution.organization_id && row.shared_currency_resource_ids.length > 0))
  const selectedLender = lenderOptions.find((row) => row.organization_id === lenderId) ?? lenderOptions[0]
  const lenderCounterparties = selectedLender
    ? counterparties.filter((row) => row.represented_organization_id === selectedLender.organization_id && row.shared_currency_resource_ids.length > 0)
    : []
  const selectedCounterparty = lenderCounterparties.find((row) => row.counterparty_organization_id === counterpartyId) ?? lenderCounterparties[0]
  const currencies = selectedCounterparty?.shared_currency_resource_ids ?? []
  const selectedCurrency = currencies.includes(currencyId) ? currencyId : currencies[0] ?? ''
  const currentTick = parseTick(credit.snapshot_tick)
  const principal = parsePositiveMinor(principalInput)
  const total = totalInput.trim() === '' ? null : parsePositiveMinor(totalInput)
  const acceptedThrough = parseTick(acceptedThroughInput)
  const maturity = parseTick(maturityInput)
  const totalIsValid = totalInput.trim() === '' || (principal !== null && total !== null && total >= principal)
  const offerDatesAreValid = currentTick !== null
    && acceptedThrough !== null
    && maturity !== null
    && acceptedThrough >= currentTick
    && acceptedThrough < maturity
    && maturity < MAX_TICK
  const canPrepareOffer = Boolean(
    canPrepareAction && selectedLender && selectedCounterparty && selectedCurrency
      && principal !== null && totalIsValid && offerDatesAreValid,
  )

  const prepareOffer = () => {
    if (!canPrepareOffer || !selectedLender || !selectedCounterparty || !selectedCurrency || principal === null || acceptedThrough === null || maturity === null) return
    const totalClause = total === null ? '' : `应还总额 ${total}。`
    onPrepareAction(
      `请提出借款方案，出借方机构 ID ${selectedLender.organization_id}，借方机构 ID ${selectedCounterparty.counterparty_organization_id}，币种 ${selectedCurrency}。本金 ${principal}。接受截止于世界第 ${acceptedThrough} 秒。到期于世界第 ${maturity} 秒。${totalClause}`,
    )
  }

  return <details className="shipment-panel promise-panel" aria-label="机构借款">
    <summary className="shipment-panel-summary">
      <span><Icon name="box" size={14} />机构借款</span>
      <small>{contracts.length} 笔可见记录 · {institutions.length} 个可代表机构</small>
    </summary>
    <div className="shipment-list" aria-live="polite">
      <p className="author-revision-help">资料快照世界时刻：{formatTick(credit.snapshot_tick)}。只显示当前角色可签署机构的授权资料，不显示机构账户余额。</p>

      {(credit.represented_institutions_truncated || credit.visible_credit_counterparties_truncated || credit.institutional_credit_contracts_truncated) && <p className="shipment-empty" role="status">部分授权资料列表已截断，可能未覆盖全部机构、对手方或借款记录；请只按当前列出的记录准备操作。</p>}
      {normalized.invalidRows && <p className="shipment-empty" role="status">部分资料无法精确读取，已隐藏该记录。</p>}

      <section aria-label="可代表机构">
        <header className="shipment-card-heading safe-conduct-section-heading"><strong>可代表机构</strong><small>{institutions.length} 项</small></header>
        {institutions.length ? <div className="shipment-list">
          {institutions.map((institution) => <article className="shipment-card" key={institution.organization_id}>
            <header className="shipment-card-heading"><strong>{institution.name?.trim() || '机构名称未提供'}</strong><small>{institution.organization_id}</small></header>
            <p className="occupation-id">已登记币种资源：{institution.currency_resource_ids.length ? institution.currency_resource_ids.join('、') : '无'}</p>
            {institution.currency_resource_ids_truncated && <p className="shipment-empty" role="status">该机构的币种列表已截断；未列出的币种不会用于准备还款。</p>}
          </article>)}
        </div> : <p className="shipment-empty" role="status">当前投影没有可代表的签署机构。</p>}
      </section>

      <section aria-label="可见机构借款记录">
        <header className="shipment-card-heading safe-conduct-section-heading"><strong>借款记录</strong><small>{contracts.length} 笔</small></header>
        {contracts.length ? <div className="shipment-list">
          {contracts.map((contract) => <CreditContractCard
            key={contract.credit_id}
            contract={contract}
            institutions={institutions}
            snapshotTick={credit.snapshot_tick}
            canPrepareAction={canPrepareAction}
            canPrepareQuery={canPrepareQuery}
            paymentInput={paymentInputs[contract.credit_id] ?? ''}
            onPaymentInput={(value) => setPaymentInputs((current) => ({ ...current, [contract.credit_id]: value }))}
            onPrepareAction={onPrepareAction}
            onPrepareQuery={onPrepareQuery}
            onPreviewResolution={onPreviewResolution}
          />)}
        </div> : <p className="shipment-empty" role="status">当前没有本角色可处理的机构借款记录。</p>}
      </section>

      <section aria-label="新借款方案">
        <header className="shipment-card-heading safe-conduct-section-heading"><strong>准备借款方案</strong><small>{counterparties.length} 个可见对手方</small></header>
        {selectedLender && selectedCounterparty && currencies.length > 0 ? <article className="shipment-card">
          <label className="safe-conduct-expiry">
            <span>出借机构</span>
            <select value={selectedLender.organization_id} onChange={(event) => { setLenderId(event.target.value); setCounterpartyId(''); setCurrencyId('') }} disabled={!canPrepareAction}>
              {lenderOptions.map((institution) => <option key={institution.organization_id} value={institution.organization_id}>{institution.name?.trim() || institution.organization_id}</option>)}
            </select>
          </label>
          <label className="safe-conduct-expiry">
            <span>借方机构</span>
            <select value={selectedCounterparty.counterparty_organization_id} onChange={(event) => { setCounterpartyId(event.target.value); setCurrencyId('') }} disabled={!canPrepareAction}>
              {lenderCounterparties.map((counterparty) => <option key={counterparty.counterparty_organization_id} value={counterparty.counterparty_organization_id}>{counterparty.counterparty_name?.trim() || counterparty.counterparty_organization_id}</option>)}
            </select>
          </label>
          <label className="safe-conduct-expiry">
            <span>共同登记币种资源 ID</span>
            <select value={selectedCurrency} onChange={(event) => setCurrencyId(event.target.value)} disabled={!canPrepareAction}>
              {currencies.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
            </select>
            {selectedCounterparty.shared_currency_resource_ids_truncated && <small>该对手方的共同币种列表已截断。</small>}
          </label>
          <ExactIntegerField label="本金（最小货币单位）" value={principalInput} onChange={setPrincipalInput} disabled={!canPrepareAction} placeholder="正整数，不超过 30 位" />
          <ExactIntegerField label="应还总额（可选，最小货币单位）" value={totalInput} onChange={setTotalInput} disabled={!canPrepareAction} placeholder="留空表示仅偿还本金" />
          {totalInput.trim() && principal !== null && total !== null && total < principal && <p className="shipment-empty" role="alert">应还总额不能低于本金。</p>}
          <ExactIntegerField label="接受截止世界秒" value={acceptedThroughInput} onChange={setAcceptedThroughInput} disabled={!canPrepareAction} placeholder="签约后仍可接受的最晚世界秒" />
          <ExactIntegerField label="到期世界秒" value={maturityInput} onChange={setMaturityInput} disabled={!canPrepareAction} placeholder="必须晚于接受截止世界秒" />
          <small>接受截止世界秒不得早于快照时刻，且必须早于到期世界秒。金额只输入整数最小单位。</small>
          {acceptedThroughInput.trim() && acceptedThrough === null && <p className="shipment-empty" role="alert">接受截止时间必须是有符号 64 位世界秒整数。</p>}
          {maturityInput.trim() && maturity === null && <p className="shipment-empty" role="alert">到期时间必须是有符号 64 位世界秒整数。</p>}
          {maturity === MAX_TICK && <p className="shipment-empty" role="alert">到期世界秒必须低于最大有符号值，为逾期登记保留后续时刻。</p>}
          {acceptedThrough !== null && maturity !== null && currentTick !== null && !offerDatesAreValid && maturity !== MAX_TICK && <p className="shipment-empty" role="alert">请确认接受截止世界秒不早于快照时刻，且早于到期世界秒。</p>}
          <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={prepareOffer} disabled={!canPrepareOffer} title={canPrepareAction ? '只填入行动草稿；编辑后再发送' : '角色当前不能准备行动'}>准备提出方案</button>
          </div>
          <p className="author-revision-help">方案只会填入行动草稿，不会提交或划拨资金。引擎会再次核验签署权、条款与资金。</p>
        </article> : <p className="shipment-empty" role="status">没有同时具备可代表出借机构、可见对手方和共同登记币种的方案选项。</p>}
      </section>

      <InstitutionalFinanceDetails rawFinance={rawFinance} projectionLoading={projectionLoading} canPrepareAction={canPrepareAction} onPrepareAction={onPrepareAction} />
    </div>
  </details>
}

function InstitutionalFinanceDetails({ rawFinance, projectionLoading, canPrepareAction, onPrepareAction }: {
  rawFinance?: InstitutionalFinanceContext | null
  projectionLoading: boolean
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const normalized = normalizeFinanceContext(rawFinance)
  const finance = normalized?.value
  const statusLabels: Record<InstitutionalFinanceCurrency['liquidity_status'], string> = {
    no_due_credit: '当前无到期借款',
    treasury_unknown: '国库余额未知',
    covered: '账面国库余额覆盖当前到期借款',
    cash_shortfall: '已登记国库现金存在到期缺口',
  }
  const hasTruncation = finance && (
    finance.organizations_truncated
      || finance.organizations.some((organization) => organization.currencies_truncated || organization.other_assets_truncated || organization.currencies.some((currency) => currency.due_credit_ids_truncated))
  )

  return <details className="shipment-panel promise-panel" aria-label="财务核对">
    <summary className="shipment-panel-summary">
      <span><Icon name="info" size={14} />财务核对</span>
      <small>{finance ? `${finance.organizations.length} 个机构` : '只读资料'}</small>
    </summary>
    <div className="shipment-list" aria-live="polite">
      {finance ? <>
        <p className="author-revision-help">财务快照：{formatTick(finance.snapshot_tick)}。余额与借款以最小货币单位原值显示。</p>
        {hasTruncation && <p className="shipment-empty" role="status">部分财务列表已截断，以下只显示当前列出的项目。</p>}
        {normalized?.invalidRows && <p className="shipment-empty" role="status">部分财务资料无法精确读取，已隐藏该项目。</p>}
        <p className="author-revision-help">现金缺口只表示已登记国库现金与当前到期借款的核对结果，不代表整体资不抵债。其他账面持有仅列数量，没有确认的现金估值。</p>
        {finance.organizations.length ? <div className="shipment-list">
          {finance.organizations.map((organization) => <article className="shipment-card" key={organization.organization_id}>
            <header className="shipment-card-heading">
              <strong>{organization.name?.trim() || '机构名称未提供'}</strong>
              <small>{organization.organization_id}</small>
            </header>
            {organization.currencies.length ? <div className="shipment-list">
              {organization.currencies.map((currency) => <section className="shipment-card" key={currency.currency_resource_id}>
                <header className="shipment-card-heading"><strong>{currency.currency_resource_id}</strong><small>{statusLabels[currency.liquidity_status]}</small></header>
                <dl className="shipment-parties">
                  <div><dt>国库账面余额</dt><dd>{formatFinanceMinor(currency.treasury_balance_minor)}</dd></div>
                  {currency.reserve_cash_minor !== undefined && <div><dt>未映射同币种储备（不计入当前现金覆盖）</dt><dd>{formatFinanceMinor(currency.reserve_cash_minor)}</dd></div>}
                  <div><dt>当前到期借款</dt><dd>{formatFinanceMinor(currency.due_credit_minor)}</dd></div>
                  <div><dt>其中已逾期</dt><dd>{formatFinanceMinor(currency.overdue_credit_minor)}</dd></div>
                  <div><dt>未来到期借款</dt><dd>{formatFinanceMinor(currency.future_credit_minor)}</dd></div>
                  {currency.due_cash_shortfall_minor !== null && <div><dt>测得现金缺口</dt><dd>{formatFinanceMinor(currency.due_cash_shortfall_minor)}</dd></div>}
                </dl>
                <p className="occupation-id">到期借款记录 · {currency.due_credit_ids.length ? currency.due_credit_ids.join('、') : '未列出'}</p>
                <TreasuryReserveDraft organizationId={organization.organization_id} currency={currency} canPrepareAction={canPrepareAction} onPrepareAction={onPrepareAction} />
              </section>)}
            </div> : <p className="shipment-empty" role="status">没有可显示的币种核对项目。</p>}
            <header className="shipment-card-heading safe-conduct-section-heading"><strong>其他账面持有</strong><small>{organization.other_assets.length} 项</small></header>
            {organization.other_assets.length ? <div className="shipment-list">
              {organization.other_assets.map((asset, index) => <p className="occupation-id" key={`${asset.resource_id}:${index}`}>{asset.resource_id} · 数量 {asset.quantity}</p>)}
            </div> : <p className="shipment-empty" role="status">没有额外账面持有记录。</p>}
          </article>)}
        </div> : <p className="shipment-empty" role="status">当前没有可显示的财务机构资料。</p>}
      </> : projectionLoading
        ? <p className="shipment-empty" role="status">正在读取与当前世界投影同步的财务核对资料…</p>
        : rawFinance == null
          ? <p className="shipment-empty" role="status">当前投影没有可显示的财务核对资料。</p>
          : <p className="shipment-empty" role="status">财务核对资料无法安全读取。</p>}
    </div>
  </details>
}

function TreasuryReserveDraft({ organizationId, currency, canPrepareAction, onPrepareAction }: {
  organizationId: string
  currency: InstitutionalFinanceCurrency
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const [amountInput, setAmountInput] = useState('')
  const treasuryIsKnown = currency.treasury_balance_minor !== null
  const reserveAmount = currency.reserve_cash_minor === undefined ? null : parseExactFinanceQuantity(currency.reserve_cash_minor)
  const enteredAmount = parsePositiveMinor(amountInput)
  const hasMobilizableReserve = treasuryIsKnown && reserveAmount !== null && compareFinanceDecimalToInteger(reserveAmount, 1n) >= 0
  const enteredAmountIsCovered = enteredAmount !== null && reserveAmount !== null && compareFinanceDecimalToInteger(reserveAmount, enteredAmount) >= 0
  const canPrepare = canPrepareAction
    && hasMobilizableReserve
    && enteredAmountIsCovered

  if (!hasMobilizableReserve) {
    const reason = !treasuryIsKnown
      ? '财库余额未知，暂不能准备储备调入。'
      : currency.reserve_cash_minor === undefined
        ? '没有已确认的同币种储备资料，暂不能准备调入。'
        : reserveAmount === null
          ? '同币种储备资料无法精确读取，暂不能准备调入。'
          : '同币种储备不足 1 个最小单位，暂不能准备调入。'
    return <p className="author-revision-help">{reason}</p>
  }

  return <div className="author-routine-form">
    <div className="author-field-heading"><strong>储备调入财库</strong><small>可调入 {formatFinanceMinor(currency.reserve_cash_minor ?? null)}</small></div>
    <div className="author-trait-grid">
      <label><span>调入金额（最小货币单位）</span><input type="text" inputMode="numeric" autoComplete="off" maxLength={30} value={amountInput} onChange={(event) => setAmountInput(event.target.value)} disabled={!canPrepareAction} placeholder="输入正整数" aria-label={`机构 ${organizationId} 的 ${currency.currency_resource_id} 储备调入金额`} /></label>
      <div className="promise-actions">
        <button type="button" className="button button-secondary button-small" disabled={!canPrepare} onClick={() => {
          if (!canPrepare || enteredAmount === null) return
          onPrepareAction(`请从机构 ${organizationId} 的储备调入财库 ${enteredAmount} ${currency.currency_resource_id}。`)
        }} title={canPrepareAction ? '只填入行动草稿；编辑并确认后再发送' : '角色当前不能准备行动'}>准备调入财库</button>
      </div>
    </div>
    {amountInput.trim() && enteredAmount === null && <p className="shipment-empty" role="alert">请输入最多 30 位的正整数最小货币单位。</p>}
    {enteredAmount !== null && reserveAmount !== null && !enteredAmountIsCovered && <p className="shipment-empty" role="alert">调入金额不能超过已登记同币种储备 {currency.reserve_cash_minor}。</p>}
    <p className="author-revision-help">只填入调入行动草稿，由你检查并发送；不会自动偿还借款。</p>
  </div>
}

function CreditContractCard({
  contract,
  institutions,
  snapshotTick,
  canPrepareAction,
  canPrepareQuery,
  paymentInput,
  onPaymentInput,
  onPrepareAction,
  onPrepareQuery,
  onPreviewResolution,
}: {
  contract: InstitutionalCreditContract
  institutions: InstitutionalCreditInstitution[]
  snapshotTick: string
  canPrepareAction: boolean
  canPrepareQuery: boolean
  paymentInput: string
  onPaymentInput: (value: string) => void
  onPrepareAction: (text: string) => void
  onPrepareQuery: (text: string) => void
  onPreviewResolution: (action: SceneCreditAction, label: string) => void
}) {
  const [extensionOrganizationId, setExtensionOrganizationId] = useState('')
  const [extensionMaturityInput, setExtensionMaturityInput] = useState('')
  const [extensionAcceptThroughInput, setExtensionAcceptThroughInput] = useState('')
  const revision = exactRevision(contract.revision)
  const remaining = parseMinor(contract.remaining_total_minor)
  const payment = parsePositiveMinor(paymentInput)
  const now = parseTick(snapshotTick)
  const originalMaturity = parseTick(contract.maturity_tick)
  const proposedMaturity = parseTick(extensionMaturityInput.trim())
  const proposedAcceptThrough = parseTick(extensionAcceptThroughInput.trim())
  const extensionOptions = contract.extension_options ?? []
  const selectedExtensionOrganization = extensionOptions.find((option) => option.organization_id === extensionOrganizationId) ?? extensionOptions[0]
  const extensionDeadlineValid = now !== null
    && originalMaturity !== null
    && proposedMaturity !== null
    && proposedAcceptThrough !== null
    && proposedMaturity > originalMaturity
    && proposedMaturity > now
    && proposedAcceptThrough >= now
    && proposedAcceptThrough < proposedMaturity
  const canProposeExtension = canPrepareAction
    && (contract.status === 'active' || contract.status === 'overdue')
    && selectedExtensionOrganization !== undefined
    && extensionDeadlineValid
  const borrower = institutions.find((institution) => institution.organization_id === contract.borrower_organization_id)
  const borrowerHasCurrency = borrower?.currency_resource_ids.includes(contract.currency_resource_id) ?? false
  const paymentIsWithinBalance = payment !== null && remaining !== null && payment <= remaining
  const statusLabel = contract.status === 'offered' && contract.phase === 'offer_expired'
    ? '已过接受期限'
    : creditStatusLabel(contract.status)
  const statusSpoiled = contract.status === 'overdue' || contract.status === 'declined' || contract.status === 'withdrawn'
  const partyName = (name: string | undefined, id: string) => name?.trim() || `机构 ID ${id}`
  const prepareResolution = (operation: 'accept' | 'decline' | 'withdraw' | 'pay', label: string) => {
    if (revision === null) return
    const selection = { kind: 'resolve_institutional_credit' as const, credit_id: contract.credit_id, expected_revision: revision }
    if (operation === 'pay') {
      if (payment === null || !paymentIsWithinBalance) return
      onPreviewResolution({ ...selection, operation, amount_minor: String(payment) }, `核对机构借款还款 ${payment} 最小单位。`)
    } else {
      onPreviewResolution({ ...selection, operation }, `核对${label}机构借款的条款。`)
    }
  }

  return <article className="shipment-card">
    <header className="shipment-card-heading">
      <span className={`shipment-status ${statusSpoiled ? 'shipment-status-spoiled' : ''}`}>{statusLabel}</span>
      <small>版本 {revision ?? '无法精确读取'}</small>
    </header>
    <dl className="shipment-parties">
      <div><dt>出借机构</dt><dd>{partyName(contract.lender_name, contract.lender_organization_id)}</dd></div>
      <div><dt>借方机构</dt><dd>{partyName(contract.borrower_name, contract.borrower_organization_id)}</dd></div>
      <div><dt>币种资源 ID</dt><dd>{contract.currency_resource_id}</dd></div>
      <div><dt>本金</dt><dd>{formatMinor(contract.principal_minor)} 最小单位</dd></div>
      <div><dt>应还总额</dt><dd>{formatMinor(contract.total_due_minor)} 最小单位</dd></div>
      <div><dt>已偿还</dt><dd>{formatMinor(contract.repaid_minor)} 最小单位</dd></div>
      <div><dt>剩余应还</dt><dd>{formatMinor(contract.remaining_total_minor)} 最小单位</dd></div>
      {contract.status === 'offered' && <div><dt>接受截止世界秒</dt><dd>{formatTick(contract.accepted_through_tick)}</dd></div>}
      {contract.accepted_tick !== undefined && contract.accepted_tick !== null && <div><dt>接受时世界秒</dt><dd>{formatTick(contract.accepted_tick)}</dd></div>}
      <div><dt>到期世界秒</dt><dd>{formatTick(contract.maturity_tick)}</dd></div>
      {contract.overdue_at_tick !== undefined && contract.overdue_at_tick !== null && <div><dt>逾期发生世界秒</dt><dd>{formatTick(contract.overdue_at_tick)}</dd></div>}
    </dl>
    <p className="occupation-id">借款 ID · {contract.credit_id}</p>

    {contract.can_accept && <ResolutionButton label="准备接受" enabled={canPrepareAction && revision !== null} onClick={() => prepareResolution('accept', '接受')} />}
    {contract.can_decline && <ResolutionButton label="准备拒绝" enabled={canPrepareAction && revision !== null} onClick={() => prepareResolution('decline', '拒绝')} />}
    {contract.can_withdraw && <ResolutionButton label="准备撤回" enabled={canPrepareAction && revision !== null} onClick={() => prepareResolution('withdraw', '撤回')} />}

    {contract.can_pay && <>
      {borrowerHasCurrency ? <>
        <ExactIntegerField
          label={`还款金额（最小货币单位；最多剩余 ${formatMinor(contract.remaining_total_minor)}）`}
          value={paymentInput}
          onChange={onPaymentInput}
          disabled={!canPrepareAction}
          placeholder="输入明确的整数金额"
          ariaLabel={`机构借款 ${contract.credit_id} 的还款金额`}
        />
        {paymentInput.trim() && payment === null && <p className="shipment-empty" role="alert">请输入 1 到 10³⁰−1 之间的正整数最小货币单位。</p>}
        {payment !== null && !paymentIsWithinBalance && <p className="shipment-empty" role="alert">还款金额不能超过剩余应还。</p>}
        <div className="promise-actions">
          <button type="button" className="button button-secondary button-small" onClick={() => prepareResolution('pay', '还款')}
            disabled={!canPrepareAction || !paymentIsWithinBalance || revision === null}
            title={canPrepareAction ? '预览还款金额，确认后执行' : '角色当前不能准备行动'}>准备还款</button>
        </div>
      </> : <p className="shipment-empty" role="status">本角色所代表的借方币种清单未列出 {contract.currency_resource_id}，暂不能准备还款。</p>}
    </>}

    <div className="promise-actions">
      <button type="button" className="button button-secondary button-small" onClick={() => onPrepareQuery(`请说明机构借款 ${contract.credit_id} 的当前状态、条款和下一步可处理事项。`)} disabled={!canPrepareQuery} title={canPrepareQuery ? '只填入问 GM 草稿；检查后再发送' : '当前无法准备查询'}>问 GM</button>
    </div>

    {(contract.extensions?.length || contract.extensions_truncated) ? <section className="shipment-list" aria-label="借款展期记录">
      <header className="shipment-card-heading safe-conduct-section-heading"><strong>展期记录</strong><small>{contract.extensions?.length ?? 0} 项</small></header>
      {contract.extensions?.map((extension) => <CreditExtensionCard key={extension.extension_id} extension={extension} snapshotTick={snapshotTick} currentOverdueTick={contract.overdue_at_tick} remainingTotalMinor={contract.remaining_total_minor} currencyResourceId={contract.currency_resource_id} creditId={contract.credit_id} canPrepareAction={canPrepareAction} onPrepareAction={onPrepareAction} institutions={institutions} />)}
      {contract.extensions_truncated && <p className="shipment-empty" role="status">仅显示最近的展期方案，较早记录可能未列出。</p>}
    </section> : null}

    {extensionOptions.length > 0 && (contract.status === 'active' || contract.status === 'overdue') && <details className="author-routine-form">
      <summary>准备展期方案</summary>
      <p className="author-revision-help">展期只会写入行动草稿。请填写新的到期世界时刻与接受截止；现有到期时刻和逾期记录会保留在方案记录中。</p>
      {contract.extension_options_truncated && <p className="shipment-empty" role="status">机构选项列表已截断，请从当前可见选项中选择。</p>}
      <div className="author-trait-grid">
        <label>
          <span>提议机构</span>
          <select style={{ width: '100%', minWidth: 0 }} value={selectedExtensionOrganization?.organization_id ?? ''} onChange={(event) => setExtensionOrganizationId(event.target.value)} disabled={!canPrepareAction}>
            {extensionOptions.map((option) => <option key={option.organization_id} value={option.organization_id}>{option.name?.trim() || `机构 ID ${option.organization_id}`}</option>)}
          </select>
        </label>
        <TickInput label="新的到期世界时刻（整数秒）" value={extensionMaturityInput} onChange={setExtensionMaturityInput} disabled={!canPrepareAction} />
        <TickInput label="接受截止世界时刻（整数秒）" value={extensionAcceptThroughInput} onChange={setExtensionAcceptThroughInput} disabled={!canPrepareAction} />
      </div>
      {(extensionMaturityInput.trim() && proposedMaturity === null) && <p className="shipment-empty" role="alert">新到期时刻须为 64 位有符号世界时刻整数秒。</p>}
      {(extensionAcceptThroughInput.trim() && proposedAcceptThrough === null) && <p className="shipment-empty" role="alert">接受截止须为 64 位有符号世界时刻整数秒。</p>}
      {proposedMaturity !== null && proposedAcceptThrough !== null && !extensionDeadlineValid && <p className="shipment-empty" role="alert">新到期时刻必须晚于原到期时刻和当前世界时刻；接受截止须不早于当前世界时刻且早于新到期时刻。</p>}
      <div className="promise-actions">
        <button type="button" className="button button-secondary button-small" disabled={!canProposeExtension} title={canPrepareAction ? '只填入行动草稿；编辑并确认后再发送' : '角色当前不能准备行动'} onClick={() => {
          if (!canProposeExtension || !selectedExtensionOrganization || proposedMaturity === null || proposedAcceptThrough === null) return
          onPrepareAction(`我代表机构 ${selectedExtensionOrganization.organization_id} 提出借款 ${contract.credit_id} 的展期方案。新的到期时刻为世界第 ${proposedMaturity} 秒，接受截止于世界第 ${proposedAcceptThrough} 秒。`)
        }}>准备提出展期</button>
      </div>
    </details>}
  </article>
}

function CreditExtensionCard({ extension, snapshotTick, currentOverdueTick, remainingTotalMinor, currencyResourceId, creditId, canPrepareAction, onPrepareAction, institutions }: {
  extension: NonNullable<InstitutionalCreditContract['extensions']>[number]
  snapshotTick: string
  currentOverdueTick?: string | null
  remainingTotalMinor: string
  currencyResourceId: string
  creditId: string
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
  institutions: InstitutionalCreditInstitution[]
}) {
  const snapshot = parseTick(snapshotTick)
  const acceptThrough = parseTick(extension.accept_through_tick)
  const timedOut = snapshot !== null && acceptThrough !== null && snapshot > acceptThrough
  const preservedOverdueTick = extension.previous_overdue_tick ?? currentOverdueTick
  const proposerName = institutions.find((row) => row.organization_id === extension.proposer_organization_id)?.name?.trim()
  const statusLabel = extension.status === 'proposed'
    ? timedOut ? '已过接受期限' : extension.expired ? '已失效' : '待回应'
    : ({ accepted: '已接受', declined: '已拒绝', withdrawn: '已撤回' } as const)[extension.status]
  return <article className="shipment-card">
    <header className="shipment-card-heading"><span className="shipment-status">{statusLabel}</span><small>{proposerName || `机构 ID ${extension.proposer_organization_id}`}</small></header>
    <dl className="shipment-parties">
      <div><dt>原到期时刻</dt><dd>{formatTick(extension.previous_maturity_tick)}</dd></div>
      <div><dt>拟议新到期时刻</dt><dd>{formatTick(extension.new_maturity_tick)}</dd></div>
      <div><dt>接受截止</dt><dd>{formatTick(extension.accept_through_tick)}</dd></div>
      <div><dt>提议时刻</dt><dd>{formatTick(extension.proposed_tick)}</dd></div>
      {extension.resolved_tick != null && <div><dt>处理时刻</dt><dd>{formatTick(extension.resolved_tick)}</dd></div>}
      {extension.status === 'accepted' && preservedOverdueTick != null && <div><dt>保留的原逾期起点</dt><dd>{formatTick(preservedOverdueTick)}</dd></div>}
      {extension.status === 'accepted' && <div><dt>当前剩余应还</dt><dd>{formatMinor(remainingTotalMinor)} 最小单位 · {currencyResourceId}</dd></div>}
    </dl>
    {extension.status === 'accepted' && preservedOverdueTick != null && <p className="author-revision-help">接受展期后，既有逾期起点仍保留。</p>}
    {extension.can_accept && <ResolutionButton label="准备接受展期" enabled={canPrepareAction} onClick={() => onPrepareAction(`我接受借款展期方案 ${extension.extension_id}。`)} />}
    {extension.can_decline && <ResolutionButton label="准备拒绝展期" enabled={canPrepareAction} onClick={() => onPrepareAction(`我拒绝借款展期方案 ${extension.extension_id}。`)} />}
    {extension.can_withdraw && <ResolutionButton label="准备撤回展期" enabled={canPrepareAction} onClick={() => onPrepareAction(`我撤回借款展期方案 ${extension.extension_id}。`)} />}
    <p className="occupation-id">展期方案 ID · {extension.extension_id} · 借款 {creditId}</p>
  </article>
}

function TickInput({ label, value, onChange, disabled }: { label: string; value: string; onChange: (value: string) => void; disabled: boolean }) {
  return <label>
    <span>{label}</span>
    <input type="text" inputMode="text" autoComplete="off" maxLength={20} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled} placeholder="输入有符号整数秒" />
  </label>
}

function ExactIntegerField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  disabled: boolean
  placeholder: string
  ariaLabel?: string
}) {
  return <label className="safe-conduct-expiry">
    <span>{label}</span>
    <input type="text" inputMode="numeric" autoComplete="off" maxLength={30} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled} placeholder={placeholder} aria-label={ariaLabel ?? label} />
  </label>
}

function ResolutionButton({ label, enabled, onClick }: { label: string; enabled: boolean; onClick: () => void }) {
  return <div className="promise-actions"><button type="button" className="button button-secondary button-small" onClick={onClick} disabled={!enabled} title={enabled ? '只填入行动草稿；编辑后再发送' : '角色当前不能准备行动'}>{label}</button></div>
}

function normalizeCreditContext(value: unknown): NormalizedCreditContext | null {
  if (!isRecord(value) || parseTick(value.snapshot_tick) === null) return null
  let invalidRows = false
  const rawInstitutions = Array.isArray(value.represented_institutions) ? value.represented_institutions : []
  const rawCounterparties = Array.isArray(value.visible_credit_counterparties) ? value.visible_credit_counterparties : []
  const rawContracts = Array.isArray(value.institutional_credit_contracts) ? value.institutional_credit_contracts : []
  if (!Array.isArray(value.represented_institutions) || !Array.isArray(value.visible_credit_counterparties) || !Array.isArray(value.institutional_credit_contracts)) invalidRows = true
  if (rawInstitutions.length > MAX_CREDIT_ROWS || rawCounterparties.length > MAX_CREDIT_ROWS || rawContracts.length > MAX_CREDIT_ROWS) invalidRows = true

  const institutions: InstitutionalCreditInstitution[] = rawInstitutions.slice(0, MAX_CREDIT_ROWS).flatMap((row) => {
    if (!isRecord(row) || !nonEmptyString(row.organization_id)) { invalidRows = true; return [] }
    const rawCurrencies = Array.isArray(row.currency_resource_ids) ? row.currency_resource_ids : []
    const currencies = rawCurrencies.slice(0, MAX_CURRENCIES_PER_ROW).filter(isResourceId)
    if (!Array.isArray(row.currency_resource_ids) || currencies.length !== Math.min(rawCurrencies.length, MAX_CURRENCIES_PER_ROW) || rawCurrencies.length > MAX_CURRENCIES_PER_ROW) invalidRows = true
    return [{
      organization_id: row.organization_id,
      name: typeof row.name === 'string' && row.name.length <= 160 ? row.name : null,
      currency_resource_ids: currencies.slice(0, MAX_CURRENCIES_PER_ROW),
      currency_resource_ids_truncated: row.currency_resource_ids_truncated === true || rawCurrencies.length > MAX_CURRENCIES_PER_ROW,
    }]
  })
  const counterparties: InstitutionalCreditCounterparty[] = rawCounterparties.slice(0, MAX_CREDIT_ROWS).flatMap((row) => {
    if (!isRecord(row) || !nonEmptyString(row.represented_organization_id) || !nonEmptyString(row.counterparty_organization_id)) { invalidRows = true; return [] }
    const rawCurrencies = Array.isArray(row.shared_currency_resource_ids) ? row.shared_currency_resource_ids : []
    const currencies = rawCurrencies.slice(0, MAX_CURRENCIES_PER_ROW).filter(isResourceId)
    if (!Array.isArray(row.shared_currency_resource_ids) || currencies.length !== Math.min(rawCurrencies.length, MAX_CURRENCIES_PER_ROW) || rawCurrencies.length > MAX_CURRENCIES_PER_ROW) invalidRows = true
    return [{
      represented_organization_id: row.represented_organization_id,
      counterparty_organization_id: row.counterparty_organization_id,
      counterparty_name: typeof row.counterparty_name === 'string' && row.counterparty_name.length <= 160 ? row.counterparty_name : null,
      shared_currency_resource_ids: currencies.slice(0, MAX_CURRENCIES_PER_ROW),
      shared_currency_resource_ids_truncated: row.shared_currency_resource_ids_truncated === true || rawCurrencies.length > MAX_CURRENCIES_PER_ROW,
    }]
  })
  const contracts: InstitutionalCreditContract[] = rawContracts.slice(0, MAX_CREDIT_ROWS).flatMap((row) => {
    if (!isValidCreditContract(row)) { invalidRows = true; return [] }
    const normalizedContract: InstitutionalCreditContract = { ...row }
    if (Array.isArray(row.extension_options)) {
      normalizedContract.extension_options = row.extension_options.slice(0, MAX_EXTENSION_OPTIONS)
      normalizedContract.extension_options_truncated = row.extension_options_truncated === true || row.extension_options.length > MAX_EXTENSION_OPTIONS
    } else {
      delete normalizedContract.extension_options
      delete normalizedContract.extension_options_truncated
    }
    if (Array.isArray(row.extensions)) {
      normalizedContract.extensions = row.extensions.slice(0, MAX_EXTENSION_ROWS)
      normalizedContract.extensions_truncated = row.extensions_truncated === true || row.extensions.length > MAX_EXTENSION_ROWS
    } else {
      delete normalizedContract.extensions
      normalizedContract.extensions_truncated = row.extensions_truncated === true
    }
    return [normalizedContract]
  })

  const normalized: InstitutionalCreditContext = {
    snapshot_tick: value.snapshot_tick as string,
    represented_institutions: institutions,
    represented_institutions_truncated: value.represented_institutions_truncated === true || rawInstitutions.length > MAX_CREDIT_ROWS,
    visible_credit_counterparties: counterparties,
    visible_credit_counterparties_truncated: value.visible_credit_counterparties_truncated === true || rawCounterparties.length > MAX_CREDIT_ROWS,
    institutional_credit_contracts: contracts,
    institutional_credit_contracts_truncated: value.institutional_credit_contracts_truncated === true || rawContracts.length > MAX_CREDIT_ROWS,
  }
  return { value: normalized, invalidRows }
}

function isValidCreditContract(value: unknown): value is InstitutionalCreditContract {
  if (!isRecord(value)) return false
  const statuses = new Set(['offered', 'active', 'declined', 'withdrawn', 'overdue', 'settled'])
  const principal = parseMinor(value.principal_minor)
  const total = parseMinor(value.total_due_minor)
  const repaid = parseMinor(value.repaid_minor)
  const remaining = parseMinor(value.remaining_total_minor)
  return nonEmptyString(value.credit_id)
    && typeof value.status === 'string' && statuses.has(value.status)
    && typeof value.phase === 'string'
    && exactRevision(value.revision) !== null
    && nonEmptyString(value.lender_organization_id)
    && nonEmptyString(value.borrower_organization_id)
    && isResourceId(value.currency_resource_id)
    && principal !== null && principal > 0n
    && total !== null && total >= principal
    && repaid !== null && repaid <= total
    && remaining !== null && remaining === total - repaid
    && parseTick(value.accepted_through_tick) !== null
    && parseTick(value.maturity_tick) !== null
    && (value.accepted_tick === undefined || value.accepted_tick === null || parseTick(value.accepted_tick) !== null)
    && (value.overdue_at_tick === undefined || value.overdue_at_tick === null || parseTick(value.overdue_at_tick) !== null)
    && typeof value.can_accept === 'boolean'
    && typeof value.can_decline === 'boolean'
    && typeof value.can_withdraw === 'boolean'
    && typeof value.can_pay === 'boolean'
    && (value.extension_options === undefined || value.extension_options === null || (Array.isArray(value.extension_options) && value.extension_options.every(isValidExtensionOption)))
    && (value.extension_options_truncated === undefined || typeof value.extension_options_truncated === 'boolean')
    && (value.extensions === undefined || value.extensions === null || (Array.isArray(value.extensions) && value.extensions.every(isValidCreditExtension)))
    && (value.extensions_truncated === undefined || typeof value.extensions_truncated === 'boolean')
}

function isValidExtensionOption(value: unknown): value is NonNullable<InstitutionalCreditContract['extension_options']>[number] {
  return isRecord(value)
    && isUuid(value.organization_id)
    && (value.name === null || (typeof value.name === 'string' && value.name.length <= 160))
}

function isValidCreditExtension(value: unknown): value is NonNullable<InstitutionalCreditContract['extensions']>[number] {
  const statuses = new Set(['proposed', 'accepted', 'declined', 'withdrawn'])
  return isRecord(value)
    && isUuid(value.extension_id)
    && isUuid(value.proposer_organization_id)
    && parseTick(value.previous_maturity_tick) !== null
    && parseTick(value.new_maturity_tick) !== null
    && parseTick(value.accept_through_tick) !== null
    && parseTick(value.proposed_tick) !== null
    && (value.resolved_tick === undefined || value.resolved_tick === null || parseTick(value.resolved_tick) !== null)
    && (value.previous_overdue_tick === undefined || value.previous_overdue_tick === null || parseTick(value.previous_overdue_tick) !== null)
    && typeof value.status === 'string' && statuses.has(value.status)
    && typeof value.expired === 'boolean'
    && typeof value.can_accept === 'boolean'
    && typeof value.can_decline === 'boolean'
    && typeof value.can_withdraw === 'boolean'
}

type NormalizedFinanceContext = { value: InstitutionalFinanceContext; invalidRows: boolean }

function normalizeFinanceContext(value: unknown): NormalizedFinanceContext | null {
  if (!isRecord(value) || parseTick(value.snapshot_tick) === null || !Array.isArray(value.organizations)) return null
  let invalidRows = false
  const rawOrganizations = value.organizations
  const organizations: InstitutionalFinanceOrganization[] = rawOrganizations.slice(0, MAX_CREDIT_ROWS).flatMap((row) => {
    if (!isRecord(row) || !nonEmptyString(row.organization_id)) { invalidRows = true; return [] }
    const rawCurrencies = Array.isArray(row.currencies) ? row.currencies : []
    const rawAssets = Array.isArray(row.other_assets) ? row.other_assets : []
    if (!Array.isArray(row.currencies) || !Array.isArray(row.other_assets)) invalidRows = true
    if (row.name !== undefined && row.name !== null && (typeof row.name !== 'string' || row.name.length > 160)) invalidRows = true

    const currencies: InstitutionalFinanceCurrency[] = rawCurrencies.slice(0, MAX_CURRENCIES_PER_ROW).flatMap((currency) => {
      if (!isRecord(currency) || !isResourceId(currency.currency_resource_id)) { invalidRows = true; return [] }
      const liquidityStatuses = new Set(['no_due_credit', 'treasury_unknown', 'covered', 'cash_shortfall'])
      const treasuryBalanceValid = currency.treasury_balance_minor === null || isExactSignedFinanceQuantity(currency.treasury_balance_minor)
      const reserveCashValid = currency.reserve_cash_minor === undefined || currency.reserve_cash_minor === null || isExactFinanceQuantity(currency.reserve_cash_minor)
      const dueCreditIds = Array.isArray(currency.due_credit_ids) ? currency.due_credit_ids : []
      const safeDueCreditIds = dueCreditIds.slice(0, MAX_FINANCE_DUE_IDS).filter(nonEmptyString)
      const currencyIsValid = treasuryBalanceValid
        && reserveCashValid
        && isUnsignedFinanceMinor(currency.due_credit_minor)
        && isUnsignedFinanceMinor(currency.overdue_credit_minor)
        && isUnsignedFinanceMinor(currency.future_credit_minor)
        && (currency.due_cash_shortfall_minor === null || isExactFinanceQuantity(currency.due_cash_shortfall_minor))
        && typeof currency.liquidity_status === 'string' && liquidityStatuses.has(currency.liquidity_status)
        && Array.isArray(currency.due_credit_ids) && safeDueCreditIds.length === Math.min(dueCreditIds.length, MAX_FINANCE_DUE_IDS)
        && typeof currency.due_credit_ids_truncated === 'boolean'
      if (!currencyIsValid) {
        invalidRows = true
        return []
      }
      return [{
        currency_resource_id: currency.currency_resource_id,
        treasury_balance_minor: currency.treasury_balance_minor as string | null,
        ...(typeof currency.reserve_cash_minor === 'string' ? { reserve_cash_minor: currency.reserve_cash_minor } : {}),
        due_credit_minor: currency.due_credit_minor as string,
        overdue_credit_minor: currency.overdue_credit_minor as string,
        future_credit_minor: currency.future_credit_minor as string,
        due_cash_shortfall_minor: currency.due_cash_shortfall_minor as string | null,
        liquidity_status: currency.liquidity_status as InstitutionalFinanceCurrency['liquidity_status'],
        due_credit_ids: safeDueCreditIds,
        due_credit_ids_truncated: (currency.due_credit_ids_truncated as boolean) || dueCreditIds.length > MAX_FINANCE_DUE_IDS,
      }]
    })
    const otherAssets = rawAssets.slice(0, MAX_FINANCE_ASSETS).flatMap((asset) => {
      if (!isRecord(asset) || !isResourceId(asset.resource_id) || !isExactFinanceQuantity(asset.quantity)) { invalidRows = true; return [] }
      return [{ resource_id: asset.resource_id, quantity: asset.quantity }]
    })
    if (typeof row.currencies_truncated !== 'boolean' || typeof row.other_assets_truncated !== 'boolean') invalidRows = true
    return [{
      organization_id: row.organization_id,
      name: typeof row.name === 'string' && row.name.length <= 160 ? row.name : null,
      currencies,
      currencies_truncated: row.currencies_truncated === true || rawCurrencies.length > MAX_CURRENCIES_PER_ROW,
      other_assets: otherAssets,
      other_assets_truncated: row.other_assets_truncated === true || rawAssets.length > MAX_FINANCE_ASSETS,
    }]
  })
  if (typeof value.organizations_truncated !== 'boolean') invalidRows = true
  return {
    value: {
      snapshot_tick: value.snapshot_tick as string,
      organizations,
      organizations_truncated: value.organizations_truncated === true || rawOrganizations.length > MAX_CREDIT_ROWS,
    },
    invalidRows,
  }
}

function isUnsignedFinanceMinor(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 30 || !/^\d+$/.test(value)) return false
  try {
    return BigInt(value) <= MAX_MINOR_AMOUNT
  } catch {
    return false
  }
}

type ExactFinanceQuantity = { coefficient: bigint; scale: number }

function parseExactFinanceQuantity(value: unknown): ExactFinanceQuantity | null {
  return parseExactFinanceDecimal(value, false)
}

function isExactFinanceQuantity(value: unknown): value is string {
  return parseExactFinanceQuantity(value) !== null
}

function isExactSignedFinanceQuantity(value: unknown): value is string {
  return parseExactFinanceDecimal(value, true) !== null
}

function parseExactFinanceDecimal(value: unknown, allowNegative: boolean): ExactFinanceQuantity | null {
  if (typeof value !== 'string' || value.length > 38) return null
  const match = (allowNegative ? /^(-?)(\d+)(?:\.(\d{1,6}))?$/ : /^(\d+)(?:\.(\d{1,6}))?$/).exec(value)
  if (!match) return null
  const sign = allowNegative ? match[1] : ''
  const integer = allowNegative ? match[2] : match[1]
  const fraction = allowNegative ? match[3] ?? '' : match[2] ?? ''
  if (integer.length > 30) return null
  try {
    const coefficient = BigInt(`${integer}${fraction}`) * (sign === '-' ? -1n : 1n)
    return { coefficient, scale: fraction.length }
  } catch {
    return null
  }
}

function compareFinanceDecimalToInteger(value: ExactFinanceQuantity, integer: bigint): -1 | 0 | 1 {
  const integerAtScale = integer * (10n ** BigInt(value.scale))
  return value.coefficient < integerAtScale ? -1 : value.coefficient > integerAtScale ? 1 : 0
}

function formatFinanceMinor(value: string | null): string {
  return value === null ? '未知' : `${value} 最小单位`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 160 && value.trim().length > 0
}

function isResourceId(value: unknown): value is string {
  return typeof value === 'string' && RESOURCE_ID_PATTERN.test(value)
}

function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_PATTERN.test(value)
}

function parseTick(value: unknown): bigint | null {
  if (typeof value !== 'string' || value.length > 20 || !/^-?\d+$/.test(value)) return null
  try {
    const tick = BigInt(value)
    return tick >= MIN_TICK && tick <= MAX_TICK ? tick : null
  } catch {
    return null
  }
}

function formatTick(value: string): string {
  return parseTick(value) === null ? '无法精确读取' : `世界时刻 ${value} 秒`
}

function parseMinor(value: unknown): bigint | null {
  if (typeof value !== 'string' || !/^\d+$/.test(value) || value.length > 30) return null
  try {
    const amount = BigInt(value)
    return amount <= MAX_MINOR_AMOUNT ? amount : null
  } catch {
    return null
  }
}

function parsePositiveMinor(value: string): bigint | null {
  const amount = parseMinor(value.trim())
  return amount !== null && amount > 0n ? amount : null
}

function formatMinor(value: string): string {
  const amount = parseMinor(value)
  return amount === null ? '无法精确读取' : amount.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function exactRevision(value: unknown): string | null {
  let revision: bigint
  if (typeof value === 'string' && /^\d+$/.test(value)) {
    try { revision = BigInt(value) } catch { return null }
  } else if (typeof value === 'number' && Number.isSafeInteger(value)) {
    revision = BigInt(value)
  } else {
    return null
  }
  return revision >= 1n && revision <= MAX_TICK ? revision.toString() : null
}

function creditStatusLabel(status: InstitutionalCreditContract['status']): string {
  return ({
    offered: '待接受',
    active: '还款中',
    declined: '已拒绝',
    withdrawn: '已撤回',
    overdue: '已逾期',
    settled: '已结清',
  })[status]
}
