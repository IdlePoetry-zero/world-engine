export function formatWorldDuration(value: bigint, daySeconds: number, yearDays: number): string {
  if (!isValidCalendarLength(daySeconds)) return `${formatTick(value)} tick`
  const dayLength = BigInt(daySeconds)
  const negative = value < 0n
  let remaining = negative ? -value : value
  const parts: string[] = []

  if (isValidCalendarLength(yearDays)) {
    const yearLength = dayLength * BigInt(yearDays)
    const years = remaining / yearLength
    remaining %= yearLength
    if (years > 0n) parts.push(`${formatTick(years)} 年`)
  }

  const days = remaining / dayLength
  remaining %= dayLength
  if (days > 0n) parts.push(`${formatTick(days)} 日`)

  const hours = remaining / 3_600n
  remaining %= 3_600n
  if (hours > 0n) parts.push(`${formatTick(hours)} 小时`)

  const minutes = remaining / 60n
  remaining %= 60n
  if (minutes > 0n) parts.push(`${formatTick(minutes)} 分钟`)
  if (remaining > 0n || parts.length === 0) parts.push(`${formatTick(remaining)} 秒`)

  return `${negative ? '−' : ''}${parts.join(' ')}`
}

export function isValidCalendarLength(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0
}

export function formatTick(value: bigint | null): string {
  return value === null ? '未知' : value.toLocaleString('zh-HK')
}
