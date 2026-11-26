import { ApiError, type ConversationItem } from '../api/client'

export const MESSAGE_RECEIPT_AUTO_POLL_LIMIT = 8

export function shouldContinueMessageReceiptPolling(status: string, pollsUsed: number) {
  return status === 'running' && pollsUsed < MESSAGE_RECEIPT_AUTO_POLL_LIMIT
}

export function conversationErrorCode(item: ConversationItem) {
  const error = item.result?.error
  if (typeof error === 'string') return error
  if (error && typeof error === 'object' && typeof error.code === 'string') return error.code
  return undefined
}

export function canRetryFailedMessageFromReceipt(item: ConversationItem) {
  if (item.status !== 'failed') return false
  const code = conversationErrorCode(item)
  return code === 'PROCESS_INTERRUPTED' || code === 'STORAGE_BUSY'
}

export function canEditFailedMessage(delivery: string | undefined, hasUncertainRequest: boolean) {
  return delivery === 'failed' && !hasUncertainRequest
}

export function isBranchBusyConflict(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409 && error.code === 'BRANCH_BUSY'
}
