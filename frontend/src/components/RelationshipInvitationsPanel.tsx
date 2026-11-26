import { useState } from 'react'
import { type CharacterEntity, type OwnMeetingReflection, type OwnRelationshipInvitation } from '../api/client'
import { Icon } from './Icon'
import { formatWorldRelativeTick } from '../worldTime'

const statusLabels: Record<string, string> = {
  pending: '等待回应',
  accepted: '双方已接受',
  declined: '已拒绝',
  withdrawn: '已撤回',
  ended: '已结束',
}

const outlookLabels = {
  closer: '更亲近',
  unchanged: '没有改变',
  uneasy: '感到不安',
} as const
type MeetingOutlook = keyof typeof outlookLabels

export function RelationshipInvitationsPanel({
  invitations: rawInvitations,
  reflections: rawReflections,
  entities,
  playerId,
  currentTick,
  daySeconds = 0,
  yearDays = 0,
  projectionAvailable,
  loading,
  viewError,
  canPrepareAction,
  onPrepareAction,
}: {
  invitations?: OwnRelationshipInvitation[] | null
  reflections?: OwnMeetingReflection[] | null
  entities: Record<string, CharacterEntity>
  playerId?: string
  currentTick: string | number
  daySeconds?: number
  yearDays?: number
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const [reflectionDrafts, setReflectionDrafts] = useState<Record<string, MeetingOutlook | ''>>({})
  const dataAvailable = Array.isArray(rawInvitations)
  const validRecords = dataAvailable ? rawInvitations.filter(isInvitation) : []
  const invitations = validRecords.filter((item) => item.proposer_id === playerId || item.invitee_id === playerId)
  const invalidCount = dataAvailable ? rawInvitations.length - validRecords.length : 0
  const currentTickValue = parseTick(currentTick)
  const calendarTime = (tick: string | number) => formatWorldRelativeTick(parseTick(tick), daySeconds, yearDays)
  const pendingCount = invitations.filter((item) => item.status === 'pending'
    && !isExpiredPendingInvitation(item, currentTickValue)).length
  const reflectionDataAvailable = Array.isArray(rawReflections)
  const validReflections = reflectionDataAvailable ? rawReflections.filter(isMeetingReflection) : []
  const ownReflections = validReflections.filter((item) => item.actor_id === playerId)
  const invalidReflectionCount = reflectionDataAvailable ? rawReflections.length - validReflections.length : 0

  return <details className="shipment-panel promise-panel" aria-label="我的关系邀约">
    <summary className="shipment-panel-summary">
      <span><Icon name="users" size={14} />关系邀约</span>
      <small>{loading ? '读取中' : !projectionAvailable ? '角色投影未载入' : !dataAvailable ? '邀约投影未提供' : `${invitations.length} 条邀约 · ${pendingCount} 条待回应${reflectionDataAvailable ? ` · ${ownReflections.length} 条私人感受` : ' · 私人感受投影未提供'}`}</small>
    </summary>
    <p className="shipment-empty">可在行动输入框直接提出邀约，并写明开始与结束世界秒；本面板没有单独的时间编辑器。</p>
    {loading && <p className="shipment-empty" role="status">正在读取关系邀约…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的关系邀约。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !dataAvailable && <p className="shipment-empty">当前投影未提供关系邀约记录。</p>}
    {!loading && !viewError && projectionAvailable && dataAvailable && invitations.length === 0 && <p className="shipment-empty">目前没有与你相关的关系邀约。</p>}
    {!loading && !viewError && projectionAvailable && invitations.length > 0 && <div className="shipment-list" aria-live="polite">
      {invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条邀约记录无法读取，已隐藏或排除。</p>}
      {invalidReflectionCount > 0 && <p className="shipment-empty" role="alert">有私人感受记录无法读取，已隐藏或排除。</p>}
      {invitations.map((invitation) => {
        const isProposer = invitation.proposer_id === playerId
        const counterpartId = isProposer ? invitation.invitee_id : invitation.proposer_id
        const counterpart = entities[counterpartId]?.name?.trim() || '对方资料未载入'
        const attendance = getParticipantAttendance(invitation.meeting_attendance, playerId, counterpartId)
        const ownAttendanceRecorded = attendance.some((record) => record.actorId === playerId)
        const counterpartAttendanceRecorded = attendance.some((record) => record.actorId === counterpartId)
        const bothParticipantsAttended = hasExactMeetingAttendance(
          invitation.meeting_attendance, playerId, counterpartId, invitation.meeting,
        )
        const expiredPending = isExpiredPendingInvitation(invitation, currentTickValue)
        const meetingInProgress = invitation.status === 'accepted'
          && invitation.meeting != null
          && isWithinMeeting(currentTick, invitation.meeting.start_tick, invitation.meeting.end_tick)
        const meetingEndTick = invitation.meeting ? parseTick(invitation.meeting.end_tick) : null
        const meetingEnded = invitation.status === 'accepted'
          && invitation.meeting != null
          && currentTickValue !== null
          && meetingEndTick !== null
          && currentTickValue >= meetingEndTick
        const status = expiredPending
          ? '已过接受期限'
          : meetingEnded ? '双方已接受 · 约定时段已结束' : statusLabels[invitation.status] ?? '状态暂不可识别'
        const reflection = ownReflections.find((item) => item.invitation_id === invitation.invitation_id)
        const reflectionDraft = reflectionDrafts[invitation.invitation_id] ?? ''
        const actionAllowed = canPrepareAction && Boolean(invitation.invitation_id)
        return <article className="shipment-card promise-card" key={invitation.invitation_id}>
          <header className="shipment-card-heading">
            <span className={`shipment-status ${expiredPending || invitation.status === 'declined' || invitation.status === 'withdrawn' || invitation.status === 'ended' ? 'shipment-status-spoiled' : ''}`}>{status}</span>
            <small>{isProposer ? '你提出的邀约' : '向你提出的邀约'}</small>
          </header>
          <dl className="shipment-parties">
            <div><dt>参与者</dt><dd>{counterpart}</dd></div>
            <div><dt>当前回应</dt><dd>{invitation.status === 'pending' && !expiredPending ? isProposer ? '等待对方回应' : '等待你回应' : status}</dd></div>
            <div><dt>提出 tick</dt><dd>{formatTick(invitation.proposed_tick)}</dd></div>
            <div><dt>更新 tick</dt><dd>{formatTick(invitation.updated_tick)}</dd></div>
            {invitation.meeting && <>
              {invitation.meeting_venue_name && <div><dt>会面地点</dt><dd>{invitation.meeting_venue_name}</dd></div>}
              <div><dt>开始</dt><dd><bdi title={`${formatTick(invitation.meeting.start_tick)} 世界秒`}>{calendarTime(invitation.meeting.start_tick)}</bdi></dd></div>
              <div><dt>结束</dt><dd><bdi title={`${formatTick(invitation.meeting.end_tick)} 世界秒`}>{calendarTime(invitation.meeting.end_tick)}</bdi></dd></div>
            </>}
            {invitation.meeting && <div><dt>已记录出席</dt><dd>{attendance.length
              ? attendance.map((record, index) => <span key={`${record.actorId}-${index}`}>
                {index > 0 && '；'}{record.actorId === playerId ? '你' : counterpart} · tick {record.attendedTick}
              </span>)
              : '尚无出席记录'}</dd></div>}
          </dl>
          {invitation.meeting && <p className="shipment-empty">已接受只表示同意约会安排，实际出席需另行登记。</p>}
          {reflection && <div className="meeting-reflection-record" aria-label="你的私人感受记录">
            <strong>你的私人感受</strong>
            <span>{outlookLabels[reflection.outlook]}</span>
            <small>记录 tick {formatTick(reflection.recorded_tick)}</small>
          </div>}
          {!reflection && invitation.status === 'accepted' && invitation.meeting && bothParticipantsAttended && !meetingEnded && <p className="shipment-empty">双方出席已记录；会面结束后可记录你的私人感受。</p>}
          {!reflection && invitation.status === 'accepted' && invitation.meeting && meetingEnded && !bothParticipantsAttended && <p className="shipment-empty">双方都登记出席后，才可记录私人感受。</p>}
          {!reflection && invitation.status === 'accepted' && invitation.meeting && meetingEnded && bothParticipantsAttended && reflectionDataAvailable && <div className="meeting-reflection-draft">
            <label>
              <span>会面后的私人感受</span>
              <select
                value={reflectionDraft}
                onChange={(event) => setReflectionDrafts((current) => ({ ...current, [invitation.invitation_id]: event.target.value as MeetingOutlook | '' }))}
                disabled={!actionAllowed}
              >
                <option value="">选择一项</option>
                {Object.entries(outlookLabels).map(([outlook, label]) => <option key={outlook} value={outlook}>{label}</option>)}
              </select>
            </label>
            <button
              type="button"
              className="button button-secondary button-small"
              onClick={() => {
                if (!reflectionDraft) return
                onPrepareAction(`请记录我对关系邀约 ${invitation.invitation_id} 会面后的私人感受，我的判断是“${outlookLabels[reflectionDraft]}”。`)
              }}
              disabled={!actionAllowed || !reflectionDraft}
              aria-label={`准备记录对 ${counterpart} 会面的私人感受`}
              title={actionAllowed ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
            >准备记录私人感受</button>
          </div>}
          {invitation.status === 'accepted' && invitation.meeting && meetingEnded && bothParticipantsAttended && <div className="promise-actions">
            <button
              type="button"
              className="button button-secondary button-small"
              onClick={() => onPrepareAction(`请让对方自行决定是否记录这次会面的私人感受，邀约 ID ${invitation.invitation_id}`)}
              disabled={!actionAllowed}
              aria-label={`请 ${counterpart} 自行决定是否记录会面的私人感受`}
              title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
            >请求对方记录私人感受</button>
          </div>}
          <p className="occupation-id">邀约 ID · {invitation.invitation_id}</p>
          {invitation.status === 'pending' && !expiredPending && !isProposer && <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确接受关系邀约 ${invitation.invitation_id}`)} disabled={!actionAllowed} aria-label={`把接受 ${counterpart} 关系邀约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备接受</button>
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确拒绝关系邀约 ${invitation.invitation_id}`)} disabled={!actionAllowed} aria-label={`把拒绝 ${counterpart} 关系邀约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备拒绝</button>
          </div>}
          {invitation.status === 'pending' && !expiredPending && isProposer && <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确撤回关系邀约 ${invitation.invitation_id}`)} disabled={!actionAllowed} aria-label={`把撤回对 ${counterpart} 关系邀约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备撤回</button>
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`请让对方自行回应我发出的这份待回复私人恋爱邀约 ${invitation.invitation_id}。`)} disabled={!actionAllowed} aria-label={`把请 ${counterpart} 自行回应关系邀约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；对方回应仍须由世界规则确认' : '清空输入框或等待当前请求完成后再操作'}>请求对方回应</button>
          </div>}
          {invitation.status === 'accepted' && <div className="promise-actions">
            {!meetingEnded && <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确结束与 ${counterpart} 的关系邀约 ${invitation.invitation_id}`)} disabled={!actionAllowed} aria-label={`把结束与 ${counterpart} 邀约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备结束邀约</button>}
            {meetingInProgress && !ownAttendanceRecorded && <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确赴约并登记我本人出席关系邀约 ${invitation.invitation_id}。`)} disabled={!actionAllowed} aria-label={`把登记本人出席 ${counterpart} 关系邀约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备登记本人出席</button>}
            {meetingInProgress && !counterpartAttendanceRecorded && <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`请让对方自行决定是否登记出席这份约会，邀约 ID ${invitation.invitation_id}。`)} disabled={!actionAllowed} aria-label={`把请求 ${counterpart} 自行决定是否登记赴约的行动填入输入框`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>请求对方决定赴约</button>}
          </div>}
        </article>
      })}
    </div>}
    {!loading && !viewError && projectionAvailable && dataAvailable && invitations.length > 0 && <p className="shipment-empty">按钮只会填入行动草稿；是否成立仍由你发送后按规则处理。</p>}
  </details>
}

function isMeetingReflection(value: unknown): value is OwnMeetingReflection {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const reflection = value as Partial<OwnMeetingReflection>
  return typeof reflection.invitation_id === 'string'
    && reflection.invitation_id.trim().length > 0
    && typeof reflection.actor_id === 'string'
    && reflection.actor_id.trim().length > 0
    && (reflection.outlook === 'closer' || reflection.outlook === 'unchanged' || reflection.outlook === 'uneasy')
    && isWorldSecond(reflection.recorded_tick)
}

function formatTick(value: string | number): string {
  if (typeof value === 'string') return value.trim() || '未提供'
  return Number.isSafeInteger(value) ? String(value) : '无法精确读取'
}

function isInvitation(value: unknown): value is OwnRelationshipInvitation {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const invitation = value as Partial<OwnRelationshipInvitation>
  return typeof invitation.invitation_id === 'string'
    && invitation.invitation_id.trim().length > 0
    && typeof invitation.proposer_id === 'string'
    && typeof invitation.invitee_id === 'string'
    && typeof invitation.status === 'string'
    && (typeof invitation.proposed_tick === 'string' || typeof invitation.proposed_tick === 'number')
    && (typeof invitation.updated_tick === 'string' || typeof invitation.updated_tick === 'number')
    && (typeof invitation.revision === 'string' || typeof invitation.revision === 'number')
    && isMeeting(invitation.meeting)
}

function isExpiredPendingInvitation(invitation: OwnRelationshipInvitation, currentTick: bigint | null): boolean {
  if (invitation.status !== 'pending' || !invitation.meeting || currentTick === null) return false
  const startTick = parseTick(invitation.meeting.start_tick)
  return startTick !== null && currentTick >= startTick
}

function isMeeting(value: unknown): value is NonNullable<OwnRelationshipInvitation['meeting']> | null | undefined {
  if (value === undefined || value === null) return true
  if (typeof value !== 'object' || Array.isArray(value)) return false
  const meeting = value as Record<string, unknown>
  if (!isWorldSecond(meeting.start_tick) || !isWorldSecond(meeting.end_tick)) return false
  const start = parseTick(meeting.start_tick)
  const end = parseTick(meeting.end_tick)
  return start !== null && end !== null && end > start
}

function isWorldSecond(value: unknown): value is string | number {
  if (typeof value === 'string') return /^-?\d+$/.test(value.trim())
  return typeof value === 'number' && Number.isSafeInteger(value)
}

function getParticipantAttendance(
  value: unknown,
  playerId: string | undefined,
  counterpartId: string,
): { actorId: string; attendedTick: string }[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return []
    const record = item as Record<string, unknown>
    const actorId = record.actor_id
    if (typeof actorId !== 'string' || (actorId !== playerId && actorId !== counterpartId)) return []
    const attendedTick = isWorldSecond(record.attended_tick) ? formatTick(record.attended_tick) : '无法精确读取'
    return [{ actorId, attendedTick }]
  })
}

function hasExactMeetingAttendance(
  value: unknown,
  playerId: string | undefined,
  counterpartId: string,
  meeting: OwnRelationshipInvitation['meeting'],
): boolean {
  if (!playerId || !Array.isArray(value) || value.length !== 2 || !meeting) return false
  const startTick = parseTick(meeting.start_tick)
  const endTick = parseTick(meeting.end_tick)
  if (startTick === null || endTick === null || endTick <= startTick) return false

  const participants = new Set([playerId, counterpartId])
  const attended = new Set<string>()
  for (const item of value) {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return false
    const record = item as Record<string, unknown>
    const actorId = record.actor_id
    if (typeof actorId !== 'string' || !participants.has(actorId) || attended.has(actorId)) return false
    if (!isWorldSecond(record.attended_tick)) return false
    const attendedTick = parseTick(record.attended_tick)
    if (attendedTick === null || attendedTick < startTick || attendedTick >= endTick) return false
    attended.add(actorId)
  }
  return attended.size === 2
}

function isWithinMeeting(currentTick: string | number, startTick: string | number, endTick: string | number): boolean {
  const current = parseTick(currentTick)
  const start = parseTick(startTick)
  const end = parseTick(endTick)
  return current !== null && start !== null && end !== null && current >= start && current < end
}

function parseTick(value: string | number): bigint | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) ? BigInt(value) : null
  const normalized = value.trim()
  if (!/^-?\d+$/.test(normalized)) return null
  try {
    return BigInt(normalized)
  } catch {
    return null
  }
}
