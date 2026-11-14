import { ApiError } from './api/client'

export function initials(value: string) { return value.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => Array.from(part)[0]?.toUpperCase() ?? '').join('') || 'WE' }
export function worldStatusLabel(status: string) { return ({ active: '运行中', ready: '可用', paused: '已暂停', archived: '已归档', draft: '草稿' } as Record<string, string>)[status] ?? status }
export function toError(value: unknown): Error { return value instanceof Error ? value : new Error('连接失败，请检查本地服务。') }
export function errorMessage(value: unknown): string { return value instanceof ApiError ? `${value.message}${value.correlation_id ? ` · ${value.correlation_id}` : ''}` : value instanceof Error ? value.message : '请求失败，请检查本地服务后重试。' }

