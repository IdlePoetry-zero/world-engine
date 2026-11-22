import { type CultivationBreakthroughOption } from '../api/client'
import { Icon } from './Icon'

export function CultivationBreakthroughPanel({
  options,
  canPrepareAction,
  onPrepareAction,
}: {
  options: CultivationBreakthroughOption[]
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const validOptions = options.filter(isCultivationBreakthroughOption)
  const invalidCount = options.length - validOptions.length

  return <details className="shipment-panel promise-panel" aria-label="修炼突破">
    <summary className="shipment-panel-summary">
      <span><Icon name="spark" size={14} />修炼突破</span>

    </summary>
    {invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条突破资料无法读取，已隐藏。</p>}
    {validOptions.length === 0 && <p className="shipment-empty">当前投影没有可读取的突破策略。</p>}
    <div className="shipment-list" aria-live="polite">
      {validOptions.map((option, index) => <article className="shipment-card" key={`${option.policy_id}:${index}`}>
        <header className="shipment-card-heading">
          <span className={`shipment-status${option.available ? '' : ' shipment-status-spoiled'}`}>
            {option.available ? '可尝试' : '暂不可用'}
          </span>
          <small>{option.source_realm_name} → {option.target_realm_name}</small>
        </header>
        <dl className="shipment-parties">
          <div><dt>耗时</dt><dd>{option.duration_ticks} 世界秒</dd></div>
          <div><dt>灵气消耗</dt><dd>{option.qi_cost}</dd></div>
          <div><dt>功法熟练度</dt><dd>{option.practice_skill_level ?? "未登记"} / 要求 {option.minimum_skill_level}</dd></div>
          <div><dt>失败伤害</dt><dd>{option.failure_damage}</dd></div>
          <div><dt>成功概率</dt><dd>{option.chance_percent === null ? '未提供' : `${option.chance_percent}%`}</dd></div>
        </dl>
        {!option.available && <p className="promise-text" role="status">{option.blocked_reason?.trim() || '此策略当前不可用。'}</p>}
        <div className="promise-actions">
          <button
            type="button"
            className="button button-secondary button-small"
            onClick={() => onPrepareAction(`请按突破策略 ${option.policy_id} 开始一次突破尝试。`)}
            disabled={!option.available || !canPrepareAction}
            aria-label={`准备突破至${option.target_realm_name}境`}
            title={!option.available ? (option.blocked_reason?.trim() || '此策略当前不可用') : canPrepareAction ? '填入开始突破尝试的行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
          >准备开始尝试</button>
        </div>
      </article>)}
    </div>

  </details>
}

function isCultivationBreakthroughOption(value: unknown): value is CultivationBreakthroughOption {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const option = value as Partial<CultivationBreakthroughOption>
  return typeof option.policy_id === 'string' && option.policy_id.trim().length > 0
    && typeof option.source_realm_name === 'string'
    && typeof option.target_realm_name === 'string'
    && typeof option.duration_ticks === 'string'
    && typeof option.qi_cost === 'string'
    && typeof option.minimum_skill_level === 'string'
    && (option.practice_skill_level == null || typeof option.practice_skill_level === 'string')
    && typeof option.failure_damage === 'number' && Number.isSafeInteger(option.failure_damage)
    && typeof option.available === 'boolean'
    && (option.chance_percent === null || typeof option.chance_percent === 'number' && Number.isSafeInteger(option.chance_percent))
    && (option.blocked_reason === null || typeof option.blocked_reason === 'string')
}
