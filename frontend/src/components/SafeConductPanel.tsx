import { useState } from 'react'
import { type CharacterEntity, type OwnSafeConductTreaty, type SafeConductProposalOption } from '../api/client'
import { Icon } from './Icon'

const statusLabels: Record<string, string> = {
  pending: '待回应',
  countered: '期限反提案待回应',
  active: '已生效',
  declined: '已拒绝',
  withdrawn: '已撤回',
  revoked: '已撤销',
}

export function SafeConductPanel({
  proposalOptions: rawOptions,
  treaties: rawTreaties,
  entities,
  currentTick,
  projectionAvailable,
  loading,
  viewError,
  canPrepareAction,
  onPrepareAction,
}: {
  proposalOptions?: SafeConductProposalOption[] | null
  treaties?: OwnSafeConductTreaty[] | null
  entities: Record<string, CharacterEntity>
  currentTick: string
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const [expiresAtTick, setExpiresAtTick] = useState('')
  const [counterExpiresAtTicks, setCounterExpiresAtTicks] = useState<Record<string, string>>({})
  const optionsAvailable = Array.isArray(rawOptions)
  const treatiesAvailable = Array.isArray(rawTreaties)
  const options = optionsAvailable ? rawOptions.filter(isProposalOption) : []
  const treaties = treatiesAvailable ? rawTreaties.filter(isOwnTreaty) : []
  const invalidCount = (optionsAvailable ? rawOptions.length - options.length : 0)
    + (treatiesAvailable ? rawTreaties.length - treaties.length : 0)
  const parsedExpiry = validFutureTick(expiresAtTick, currentTick)
  const activeCount = treaties.filter((treaty) => treaty.status === 'active' && !treaty.expired).length

  return <details className="shipment-panel promise-panel" aria-label="跨势力通行协定">
    <summary className="shipment-panel-summary">
      <span><Icon name="route" size={14} />通行协定</span>
      <small>{loading ? '读取中' : !projectionAvailable ? '角色投影未载入' : `${treaties.length} 项协定 · ${options.length} 条可提议路线`}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取通行协定…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的通行协定。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !optionsAvailable && !treatiesAvailable && <p className="shipment-empty">当前投影未提供通行协定资料。</p>}
    {!loading && !viewError && projectionAvailable && optionsAvailable && treatiesAvailable && options.length === 0 && treaties.length === 0 && <p className="shipment-empty">当前没有可用的协定路线或相关协定。</p>}
    {!loading && !viewError && projectionAvailable && <>
      {invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条协定资料无法读取，已隐藏。</p>}

      {optionsAvailable && options.length > 0 && <section aria-label="可提议的登记路线">
        <header className="shipment-card-heading safe-conduct-section-heading">
          <strong>发起通行协定</strong>
          <small>仅使用当前投影列出的路线</small>
        </header>
        <label className="safe-conduct-expiry">
          <span>绝对到期 tick</span>
          <input
            type="text"
            inputMode="numeric"
            value={expiresAtTick}
            onChange={(event) => setExpiresAtTick(event.target.value)}
            aria-label="通行协定绝对到期 tick"
            placeholder="输入未来的世界 tick"
            disabled={!canPrepareAction}
          />
          <small>当前 tick：{currentTick}。到期时间必须是晚于当前时间的有符号整数。</small>
        </label>
        {expiresAtTick.trim() && parsedExpiry === null && <p className="shipment-empty" role="alert">请输入有效的未来绝对 tick（范围 −2⁶³ 至 2⁶³−1）。</p>}
        <div className="shipment-list" aria-live="polite">
          {options.map((option, index) => <article className="shipment-card" key={`${option.route_id}:${option.recipient_organization_id}:${index}`}>
            <header className="shipment-card-heading">
              <span className="shipment-status">可提议</span>
              <small>{option.proposer_name} → {option.recipient_name}</small>
            </header>
            <p className="shipment-route" aria-label={`路线：${option.from_place_name} 至 ${option.to_place_name}`}>
              <span>{option.from_place_name}</span><span aria-hidden="true">→</span><span>{option.to_place_name}</span>
            </p>
            <p className="occupation-id">路线 ID · {option.route_id}</p>
            <button
              type="button"
              className="button button-secondary button-small"
              onClick={() => onPrepareAction(
                `我明确提出一条路线的跨势力通行协定：提议方 ${option.proposer_organization_id}，对方 ${option.recipient_organization_id}；从 ${option.from_place_id} 沿路线 ${option.route_id} 前往 ${option.to_place_id}，到期时间为绝对世界 tick ${parsedExpiry}。`,
              )}
              disabled={!canPrepareAction || parsedExpiry === null}
              aria-label={`把 ${option.proposer_name} 与 ${option.recipient_name} 的通行协定提议填入输入框`}
              title={!canPrepareAction ? '清空输入框或等待当前请求完成后再操作' : parsedExpiry === null ? '先输入晚于当前时间的绝对到期 tick' : '填入行动草稿；编辑后再发送'}
            >准备提议</button>
          </article>)}
        </div>
      </section>}

      {treatiesAvailable && treaties.length > 0 && <section aria-label="本人参与的通行协定">
        <header className="shipment-card-heading safe-conduct-section-heading">
          <strong>我的协定</strong>
          <small>{activeCount} 项仍生效</small>
        </header>
        <div className="shipment-list" aria-live="polite">
          {treaties.map((treaty) => {
            const expiredCurrent = treaty.expired && ['pending', 'countered', 'active'].includes(treaty.status)
            const statusLabel = expiredCurrent ? '已到期' : statusLabels[treaty.status] ?? '状态未知'
            const roleLabel = treaty.represents_proposer && treaty.represents_recipient
              ? '你代表双方'
              : treaty.represents_proposer ? '你代表提议方' : treaty.represents_recipient ? '你代表对方' : '相关协定'
            const revision = exactPositiveInteger(treaty.revision)
            const resolvable = canPrepareAction && revision !== null
            const counterExpiryInput = counterExpiresAtTicks[treaty.treaty_id] ?? ''
            const counterExpiryTick = validCounterTick(counterExpiryInput, currentTick, treaty.expires_at_tick)
            const fromName = (typeof treaty.from_place_name === 'string' ? treaty.from_place_name.trim() : '')
              || entities[treaty.from_place_id]?.name?.trim() || '起点地点资料未载入'
            const toName = (typeof treaty.to_place_name === 'string' ? treaty.to_place_name.trim() : '')
              || entities[treaty.to_place_id]?.name?.trim() || '终点地点资料未载入'
            return <article className="shipment-card" key={treaty.treaty_id}>
              <header className="shipment-card-heading">
                <span className={`shipment-status ${treaty.status !== 'active' || expiredCurrent ? 'shipment-status-spoiled' : ''}`}>{statusLabel}</span>
                <small>{roleLabel}</small>
              </header>
              <dl className="shipment-parties">
                <div><dt>提议方</dt><dd>{treaty.proposer_name?.trim() || '势力资料未载入'}</dd></div>
                <div><dt>对方</dt><dd>{treaty.recipient_name?.trim() || '势力资料未载入'}</dd></div>
                <div><dt>通行路线</dt><dd>{fromName} → {toName}</dd></div>
                <div><dt>到期 tick</dt><dd>{formatExactTick(treaty.expires_at_tick)}</dd></div>
                {treaty.status === 'countered' && treaty.counter_expires_at_tick !== undefined && treaty.counter_expires_at_tick !== null && <div><dt>反提到期 tick</dt><dd>{formatExactTick(treaty.counter_expires_at_tick)} · 待回应</dd></div>}
              </dl>
              <p className="occupation-id">协定 ID · {treaty.treaty_id} · 修订 {revision ?? '无法精确读取'}</p>

              {treaty.status === 'pending' && treaty.represents_recipient && treaty.can_counter === true && !treaty.expired && <>
                <label className="safe-conduct-expiry">
                  <span>期限反提案绝对 tick</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    value={counterExpiryInput}
                    onChange={(event) => setCounterExpiresAtTicks((current) => ({ ...current, [treaty.treaty_id]: event.target.value }))}
                    aria-label={`通行协定 ${treaty.treaty_id} 的期限反提案绝对 tick`}
                    placeholder="输入较早的未来 tick"
                    disabled={!canPrepareAction}
                  />
                  <small>原到期 tick：{formatExactTick(treaty.expires_at_tick)}。新时间必须晚于当前 tick 且早于原到期时间。</small>
                </label>
                {counterExpiryInput.trim() && counterExpiryTick === null && <p className="shipment-empty" role="alert">请输入晚于当前 tick 且早于原到期时间的有效整数。</p>}
                <div className="promise-actions">
                  <button
                    type="button"
                    className="button button-secondary button-small"
                    onClick={() => onPrepareAction(`我代表对方提出通行协定 ${treaty.treaty_id} 的期限反提案，版本 ${revision}，缩短至世界绝对时刻 ${counterExpiryTick} 秒。`)}
                    disabled={!resolvable || counterExpiryTick === null}
                    aria-label={`把通行协定 ${treaty.treaty_id} 的期限反提案填入输入框`}
                    title={canPrepareAction ? '只填入期限反提案草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
                  >准备期限反提案</button>
                </div>
              </>}

              {treaty.status === 'pending' && treaty.represents_proposer && treaty.can_request_response === true && !treaty.expired && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`请本通行协定 ${treaty.treaty_id} 的对方代表独立回应，协定版本 ${revision}。由对方自行决定。`)}
                  disabled={!resolvable}
                  aria-label="把请求对方代表独立回应的行动填入输入框"
                  title={canPrepareAction ? '只请求对方自行回应，不替对方选择批准或拒绝' : '清空输入框或等待当前请求完成后再操作'}
                >请求对方独立回应</button>
              </div>}

              {treaty.status === 'pending' && treaty.represents_recipient && <div className="promise-actions">
                {treaty.can_ratify === true && !treaty.expired && <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我代表对方势力明确批准通行协定 ${treaty.treaty_id}，版本 ${revision}。`)}
                  disabled={!resolvable}
                  title={canPrepareAction ? '填入 ratify 行动草稿；发送后由规则核验' : '清空输入框或等待当前请求完成后再操作'}
                >准备批准</button>}
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我代表对方势力明确拒绝通行协定 ${treaty.treaty_id}，版本 ${revision}。`)}
                  disabled={!resolvable}
                  title={canPrepareAction ? '填入拒绝行动草稿；发送后由规则核验' : '清空输入框或等待当前请求完成后再操作'}
                >准备拒绝</button>
              </div>}

              {treaty.status === 'countered' && treaty.represents_proposer && treaty.can_accept_counter === true && !treaty.expired && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我代表提议方明确接受通行协定 ${treaty.treaty_id} 的期限反提案，版本 ${revision}。`)}
                  disabled={!resolvable}
                  title={canPrepareAction ? '填入接受期限反提案的行动草稿；发送后由规则核验' : '清空输入框或等待当前请求完成后再操作'}
                >准备接受期限反提案</button>
              </div>}

              {(treaty.status === 'pending' || treaty.status === 'countered') && treaty.represents_proposer && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我代表提议方势力明确撤回通行协定 ${treaty.treaty_id}，版本 ${revision}。`)}
                  disabled={!resolvable}
                  title={canPrepareAction ? '填入撤回行动草稿；发送后由规则核验' : '清空输入框或等待当前请求完成后再操作'}
                >准备撤回</button>
              </div>}

              {treaty.status === 'active' && (treaty.represents_proposer || treaty.represents_recipient) && <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我代表当前一方明确撤销已生效的通行协定 ${treaty.treaty_id}，版本 ${revision}。`)}
                  disabled={!resolvable}
                  title={canPrepareAction ? '填入撤销行动草稿；发送后由规则核验当前代表权限' : '清空输入框或等待当前请求完成后再操作'}
                >准备撤销</button>
              </div>}
            </article>
          })}
        </div>
      </section>}
      {treatiesAvailable && treaties.length > 0 && <p className="shipment-empty">只有已生效且未到期的协定提供该方向路线许可；按钮只填入草稿，不会自动批准或推进世界。</p>}
    </>}
  </details>
}

function isProposalOption(value: unknown): value is SafeConductProposalOption {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const option = value as Partial<SafeConductProposalOption>
  return ['proposer_organization_id', 'proposer_name', 'recipient_organization_id', 'recipient_name',
    'from_place_id', 'from_place_name', 'to_place_id', 'to_place_name', 'route_id']
    .every((key) => typeof option[key as keyof SafeConductProposalOption] === 'string'
      && (option[key as keyof SafeConductProposalOption] as string).trim().length > 0)
}

function isOwnTreaty(value: unknown): value is OwnSafeConductTreaty {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const treaty = value as Partial<OwnSafeConductTreaty>
  return typeof treaty.treaty_id === 'string' && treaty.treaty_id.trim().length > 0
    && typeof treaty.proposer_organization_id === 'string'
    && typeof treaty.recipient_organization_id === 'string'
    && typeof treaty.from_place_id === 'string'
    && typeof treaty.to_place_id === 'string'
    && typeof treaty.route_id === 'string'
    && (typeof treaty.expires_at_tick === 'string' || typeof treaty.expires_at_tick === 'number')
    && typeof treaty.status === 'string'
    && (typeof treaty.revision === 'string' || typeof treaty.revision === 'number')
    && typeof treaty.expired === 'boolean'
    && typeof treaty.represents_proposer === 'boolean'
    && typeof treaty.represents_recipient === 'boolean'
    && (treaty.can_ratify === undefined || typeof treaty.can_ratify === 'boolean')
    && (treaty.can_request_response === undefined || typeof treaty.can_request_response === 'boolean')
    && (treaty.counter_expires_at_tick === undefined || treaty.counter_expires_at_tick === null
      || typeof treaty.counter_expires_at_tick === 'string' || typeof treaty.counter_expires_at_tick === 'number')
    && (treaty.can_counter === undefined || typeof treaty.can_counter === 'boolean')
    && (treaty.can_accept_counter === undefined || typeof treaty.can_accept_counter === 'boolean')
}

function validFutureTick(value: string, currentTick: string): string | null {
  const text = value.trim()
  if (!/^-?(0|[1-9]\d*)$/.test(text) || !/^-?(0|[1-9]\d*)$/.test(currentTick)) return null
  try {
    const tick = BigInt(text)
    const current = BigInt(currentTick)
    if (tick < -(1n << 63n) || tick > (1n << 63n) - 1n || tick <= current) return null
    return text
  } catch {
    return null
  }
}

function exactPositiveInteger(value: string | number): string | null {
  if (typeof value === 'string') return /^[1-9]\d*$/.test(value) ? value : null
  return Number.isSafeInteger(value) && value > 0 ? String(value) : null
}

function validCounterTick(value: string, currentTick: string, originalExpiry: string | number): string | null {
  const candidate = validFutureTick(value, currentTick)
  const original = exactTick(originalExpiry)
  if (candidate === null || original === null) return null
  return BigInt(candidate) < BigInt(original) ? candidate : null
}

function exactTick(value: string | number): string | null {
  if (typeof value === 'string') return /^-?(0|[1-9]\d*)$/.test(value) ? value : null
  return Number.isSafeInteger(value) ? String(value) : null
}

function formatExactTick(value: string | number): string {
  return exactTick(value) ?? '无法精确读取'
}
