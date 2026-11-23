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

export function formatWorldRelativeTick(tick: bigint | null, daySeconds: number, yearDays: number): string {
  if (tick === null) return '无法精确读取世界时间'
  if (!isValidCalendarLength(daySeconds)) return `世界 tick ${formatTick(tick)}`

  const dayLength = BigInt(daySeconds)
  let dayIndex = tick / dayLength
  let secondOfDay = tick % dayLength
  if (secondOfDay < 0n) {
    secondOfDay += dayLength
    dayIndex -= 1n
  }
  const hours = String(secondOfDay / 3_600n).padStart(2, '0')
  const minutes = String((secondOfDay % 3_600n) / 60n).padStart(2, '0')
  const seconds = String(secondOfDay % 60n).padStart(2, '0')
  const clock = `${hours}:${minutes}:${seconds}`

  if (isValidCalendarLength(yearDays)) {
    const yearLength = BigInt(yearDays)
    let yearIndex = dayIndex / yearLength
    let dayOfYear = dayIndex % yearLength
    if (dayOfYear < 0n) {
      dayOfYear += yearLength
      yearIndex -= 1n
    }
    return `世界历第 ${formatTick(yearIndex + 1n)} 年第 ${formatTick(dayOfYear + 1n)} 日 ${clock}`
  }

  return `世界第 ${formatTick(dayIndex + 1n)} 日 ${clock}`
}
