import { type CharacterEntity, type CombatTruceOptions, type OwnCombatTruce } from '../api/client'
import { Icon } from './Icon'

export function CombatTrucePanel({
  options: rawOptions,
  truces: rawTruces,
  entities,
  playerId,
  projectionAvailable,
  loading,
  viewError,
  canPrepareAction,
  onPrepareAction,
}: {
  options?: CombatTruceOptions | null
  truces?: OwnCombatTruce[] | null
  entities: Record<string, CharacterEntity>
  playerId?: string
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const optionProvided = rawOptions !== undefined && rawOptions !== null
  const option = isCombatTruceOptions(rawOptions) ? rawOptions : null
  const truceDataAvailable = Array.isArray(rawTruces)
  const validTruces = truceDataAvailable ? rawTruces.filter(isOwnCombatTruce) : []
  const invalidCount = (optionProvided && !option ? 1 : 0)
    + (truceDataAvailable ? rawTruces.length - validTruces.length : 0)
  const history = validTruces
    .filter((truce) => truce.offer_id !== option?.offer_id)
    .slice()
    .reverse()
  const opponentName = option
    ? option.opponent_name?.trim() || entities[option.opponent_id]?.name?.trim() || `对手 ${shortId(option.opponent_id)}`
    : ''
  const summary = option
    ? option.offer_id
      ? option.proposed_by_self ? '你已提出停战，等待回应' : '对方已提出停战，待你回应'
      : '当前交战中，尚无停战提议'
    : truceDataAvailable
      ? `${validTruces.length} 条历史停战记录 · 当前无可回应提议`
      : '当前没有可交涉的停战提议'

  return <details className="shipment-panel promise-panel" aria-label="交战停战">
    <summary className="shipment-panel-summary">
      <span><Icon name="shield" size={14} />交战停战</span>
      <small>{loading ? '读取中' : !projectionAvailable ? '角色投影未载入' : summary}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取停战状态…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的停战状态。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !option && !truceDataAvailable && <p className="shipment-empty">当前投影未提供停战选项或历史记录。</p>}
    {!loading && !viewError && projectionAvailable && option && <article className="shipment-card">
      <header className="shipment-card-heading">
        <span className="shipment-status">{option.offer_id
          ? option.proposed_by_self ? '等待对方回应' : '等待你回应'
          : '交战进行中'}</span>
        <small>对手 · {opponentName}</small>
      </header>
      {option.offer_id === null
        ? <>
          <p className="shipment-empty">当前遭遇尚无停战提议；你可以明确提出，由对方自行决定是否接受。</p>
          <div className="promise-actions">
            <button
              type="button"
              className="button button-secondary button-small"
              onClick={() => onPrepareAction(`我明确提出停战，针对当前遭遇最后事件 ID ${option.expected_last_event_id}。`)}
              disabled={!canPrepareAction}
              aria-label={`准备向 ${opponentName} 提出停战`}
              title={canPrepareAction ? '填入停战行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
            >准备提出停战</button>
          </div>
        </>
        : option.proposed_by_self
          ? <>
            <p className="shipment-empty">你已提出停战；是否接受由对方自行决定。</p>
            {option.can_request_response && <div className="promise-actions">
              <button
                type="button"
                className="button button-secondary button-small"
                onClick={() => onPrepareAction(`请 NPC 对方自行决定是否回应停战提议 ${option.offer_id}，不指定接受或拒绝。`)}
                disabled={!canPrepareAction}
                aria-label={`请求 ${opponentName} 自行回应停战提议`}
                title={canPrepareAction ? '只请求 NPC 自主决定，不替对方选择接受或拒绝' : '清空输入框或等待当前请求完成后再操作'}
              >请求 NPC 自主回应</button>
            </div>}
          </>
          : <>
            <p className="shipment-empty">{opponentName} 已提出停战，请明确选择是否接受。</p>
            <div className="promise-actions">
              <button
                type="button"
                className="button button-secondary button-small"
                onClick={() => onPrepareAction(`我明确接受停战提议 ${option.offer_id}。`)}
                disabled={!canPrepareAction}
                title={canPrepareAction ? '填入接受停战的行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
              >准备接受</button>
              <button
                type="button"
                className="button button-secondary button-small"
                onClick={() => onPrepareAction(`我明确拒绝停战提议 ${option.offer_id}。`)}
                disabled={!canPrepareAction}
                title={canPrepareAction ? '填入拒绝停战的行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
              >准备拒绝</button>
            </div>
          </>}
    </article>}
    {!loading && !viewError && projectionAvailable && invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条停战资料无法读取，已隐藏。</p>}
    {!loading && !viewError && projectionAvailable && truceDataAvailable && history.length > 0 && <section aria-label="停战历史记录">
      <header className="shipment-card-heading safe-conduct-section-heading">
        <strong>停战记录</strong>
        <small>{history.length} 条历史记录</small>
      </header>
      <div className="shipment-list" aria-live="polite">
        {history.map((truce) => {
          const proposedBySelf = truce.proposer_id === playerId
          const counterpartId = proposedBySelf ? truce.recipient_id : truce.proposer_id
          const counterpartName = entities[counterpartId]?.name?.trim() || `对方 ${shortId(counterpartId)}`
          const direction = proposedBySelf ? `你向 ${counterpartName} 提议` : `${counterpartName} 向你提议`
          const status = truce.status === 'accepted'
            ? '双方已同意停战'
            : truce.status === 'declined'
              ? proposedBySelf ? '对方已拒绝' : '你已拒绝'
              : '未回应（当前已无此提议）'
          return <article className="shipment-card" key={truce.offer_id}>
            <header className="shipment-card-heading">
              <span className={`shipment-status ${truce.status === 'declined' ? 'shipment-status-spoiled' : ''}`}>{status}</span>
              <small>{direction}</small>
            </header>
            <dl className="shipment-parties">
              <div><dt>提出 tick</dt><dd>{formatTick(truce.offered_tick)}</dd></div>
              {truce.responded_tick !== null && <div><dt>回应 tick</dt><dd>{formatTick(truce.responded_tick)}</dd></div>}
            </dl>
            <p className="occupation-id">停战提议 ID · {truce.offer_id}</p>
          </article>
        })}
      </div>
    </section>}
    {!loading && !viewError && projectionAvailable && !truceDataAvailable && option && <p className="shipment-empty">当前投影未提供历史停战记录。</p>}
    {!loading && !viewError && projectionAvailable && !option && truceDataAvailable && validTruces.length === 0 && invalidCount === 0 && <p className="shipment-empty">当前没有正在交涉的停战提议或历史记录。</p>}
    {!loading && !viewError && projectionAvailable && option && <p className="shipment-empty">所有按钮只会填入你选定的行动草稿；不会替对方决定回应。</p>}
  </details>
}

function isCombatTruceOptions(value: unknown): value is CombatTruceOptions {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const options = value as Partial<CombatTruceOptions>
  return typeof options.opponent_id === 'string' && options.opponent_id.trim().length > 0
    && (options.opponent_name === undefined || options.opponent_name === null || typeof options.opponent_name === 'string')
    && typeof options.expected_last_event_id === 'string' && options.expected_last_event_id.trim().length > 0
    && (options.offer_id === null || typeof options.offer_id === 'string' && options.offer_id.trim().length > 0)
    && typeof options.proposed_by_self === 'boolean'
    && typeof options.can_request_response === 'boolean'
}

function isOwnCombatTruce(value: unknown): value is OwnCombatTruce {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const truce = value as Partial<OwnCombatTruce>
  return typeof truce.offer_id === 'string' && truce.offer_id.trim().length > 0
    && typeof truce.proposer_id === 'string' && truce.proposer_id.trim().length > 0
    && typeof truce.recipient_id === 'string' && truce.recipient_id.trim().length > 0
    && (truce.status === 'pending' || truce.status === 'accepted' || truce.status === 'declined')
    && isWorldTick(truce.offered_tick)
    && (truce.responded_tick === null || isWorldTick(truce.responded_tick))
}

function isWorldTick(value: unknown): value is string | number {
  if (typeof value === 'string') return /^-?\d+$/.test(value.trim())
  return typeof value === 'number' && Number.isSafeInteger(value)
}

function formatTick(value: string | number): string {
  if (typeof value === 'string') return /^-?\d+$/.test(value.trim()) ? value.trim() : '无法精确读取'
  return Number.isSafeInteger(value) ? String(value) : '无法精确读取'
}

function shortId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value
}
