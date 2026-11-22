import { useState } from 'react'
import { type ShipmentReliefOption } from '../api/client'
import { Icon } from './Icon'

type ExactDecimal = { coefficient: bigint; scale: number }

export function ShipmentReliefPanel({ options: rawOptions, canPrepareAction, onPrepareAction }: {
  options?: ShipmentReliefOption[] | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const [shipmentId, setShipmentId] = useState('')
  const [organizationId, setOrganizationId] = useState('')
  const [recipientId, setRecipientId] = useState('')
  const [quantityText, setQuantityText] = useState('')
  const options = Array.isArray(rawOptions) ? rawOptions.filter(isShipmentReliefOption) : []

  if (options.length === 0) return null

  const selectedOption = options.find((option) => option.shipment_id === shipmentId) ?? options[0]
  const organizations = selectedOption.organizations
  const recipients = selectedOption.recipients
  const selectedOrganization = organizations.find((organization) => organization.organization_id === organizationId)
    ?? organizations[0]
  const selectedRecipient = recipients.find((recipient) => recipient.recipient_id === recipientId)
    ?? recipients[0]
  const enteredQuantity = parsePositiveDecimal(quantityText)
  const availableQuantity = parsePositiveDecimal(selectedOption.available_quantity)
  const withinAvailable = enteredQuantity !== null && availableQuantity !== null
    && compareDecimals(enteredQuantity, availableQuantity) <= 0
  const quantityMessage = quantityText.trim().length === 0
    ? `可用 ${selectedOption.available_quantity} 单位；请输入正数，最多 6 位小数。`
    : enteredQuantity === null
      ? `可用 ${selectedOption.available_quantity} 单位；请输入大于 0 的十进制数量，最多 6 位小数。`
      : !withinAvailable
        ? `可用 ${selectedOption.available_quantity} 单位；分发数量不能超过可用量。`
        : `可用 ${selectedOption.available_quantity} 单位；数量已按十进制精确比较。`

  const prepare = () => {
    if (!withinAvailable || !selectedOrganization || !selectedRecipient) return
    const quantity = quantityText.trim()
    onPrepareAction(
      `我明确代表组织 ${selectedOrganization.name}（组织 ID ${selectedOrganization.organization_id}），从已到货的货运 ${selectedOption.shipment_id} 发放 ${quantity} 单位粮食给 ${selectedRecipient.name}（人物 ID ${selectedRecipient.recipient_id}）。`,
    )
  }

  const changeShipment = (nextShipmentId: string) => {
    const nextOption = options.find((option) => option.shipment_id === nextShipmentId)
    setShipmentId(nextShipmentId)
    setOrganizationId(nextOption?.organizations[0]?.organization_id ?? '')
    setRecipientId(nextOption?.recipients[0]?.recipient_id ?? '')
    setQuantityText('')
  }

  return <details className="shipment-panel promise-panel" aria-label="发放到货粮食">
    <summary className="shipment-panel-summary">
      <span><Icon name="box" size={14} />发放到货粮食</span>
      <small>{options.length} 笔可用货运</small>
    </summary>
    <div className="shipment-list" aria-live="polite">
      <article className="shipment-card">
        <label className="safe-conduct-expiry">
          <span>选择运单</span>
          <select
            aria-label="选择可分发的运单"
            value={selectedOption.shipment_id}
            onChange={(event) => changeShipment(event.target.value)}
          >
            {options.map((option, index) => <option key={`${option.shipment_id}:${index}`} value={option.shipment_id}>
              {option.depot_name} · {option.resource_id}
            </option>)}
          </select>
        </label>
        <label className="safe-conduct-expiry">
          <span>代表组织</span>
          <select
            aria-label="选择代表组织"
            value={selectedOrganization.organization_id}
            onChange={(event) => setOrganizationId(event.target.value)}
          >
            {organizations.map((organization) => <option key={organization.organization_id} value={organization.organization_id}>
              {organization.name}
            </option>)}
          </select>
        </label>
        <label className="safe-conduct-expiry">
          <span>接收居民</span>
          <select
            aria-label="选择接收救济物资的居民"
            value={selectedRecipient.recipient_id}
            onChange={(event) => setRecipientId(event.target.value)}
          >
            {recipients.map((recipient) => <option key={recipient.recipient_id} value={recipient.recipient_id}>
              {recipient.name}
            </option>)}
          </select>
        </label>
        <label className="safe-conduct-expiry">
          <span>分发数量</span>
          <input
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={quantityText}
            onChange={(event) => setQuantityText(event.target.value)}
            aria-label="分发数量"
            placeholder="例如 2.5"
            aria-invalid={quantityText.trim().length > 0 && !withinAvailable}
          />
          <small role={quantityText.trim().length > 0 && !withinAvailable ? 'alert' : undefined}>{quantityMessage}</small>
        </label>
        <div className="promise-actions">
          <button
            type="button"
            className="button button-secondary button-small"
            onClick={prepare}
            disabled={!canPrepareAction || !withinAvailable}
            title={canPrepareAction ? '只填入行动草稿；检查并编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
          >准备分发</button>
        </div>
      </article>
    </div>
  </details>
}

function isShipmentReliefOption(value: unknown): value is ShipmentReliefOption {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const option = value as Partial<ShipmentReliefOption>
  return typeof option.shipment_id === 'string' && option.shipment_id.trim().length > 0
    && typeof option.depot_name === 'string' && option.depot_name.trim().length > 0
    && typeof option.resource_id === 'string' && option.resource_id.trim().length > 0
    && typeof option.available_quantity === 'string'
    && parsePositiveDecimal(option.available_quantity) !== null
    && Array.isArray(option.organizations) && option.organizations.length > 0
    && option.organizations.every(isOrganizationOption)
    && Array.isArray(option.recipients) && option.recipients.length > 0
    && option.recipients.every(isRecipientOption)
}

function isOrganizationOption(value: unknown): value is ShipmentReliefOption['organizations'][number] {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const option = value as Partial<ShipmentReliefOption['organizations'][number]>
  return typeof option.organization_id === 'string' && option.organization_id.trim().length > 0
    && typeof option.name === 'string' && option.name.trim().length > 0
}

function isRecipientOption(value: unknown): value is ShipmentReliefOption['recipients'][number] {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const option = value as Partial<ShipmentReliefOption['recipients'][number]>
  return typeof option.recipient_id === 'string' && option.recipient_id.trim().length > 0
    && typeof option.name === 'string' && option.name.trim().length > 0
}

function parsePositiveDecimal(value: string): ExactDecimal | null {
  const normalized = value.trim()
  const match = /^(\d+)(?:\.(\d{1,6}))?$/.exec(normalized)
  if (!match) return null
  if (match[1].replace(/^0+/, '').length > 30) return null
  const fraction = match[2] ?? ''
  let coefficient: bigint
  try {
    coefficient = BigInt(`${match[1]}${fraction}`)
  } catch {
    return null
  }
  return coefficient > 0n ? { coefficient, scale: fraction.length } : null
}

function compareDecimals(left: ExactDecimal, right: ExactDecimal): number {
  const scale = Math.max(left.scale, right.scale)
  const leftValue = left.coefficient * 10n ** BigInt(scale - left.scale)
  const rightValue = right.coefficient * 10n ** BigInt(scale - right.scale)
  return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0
}
