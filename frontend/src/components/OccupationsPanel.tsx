import { type LocalWorkPosting, type OwnWorkAssignment, type WorkShiftOffer } from '../api/client'
import { Icon } from './Icon'

export function OccupationsPanel({
  postings: rawPostings, assignments: rawAssignments, projectionAvailable,
  loading, viewError, canPrepareAction, onPrepareAction,
}: {
  postings?: LocalWorkPosting[] | null
  assignments?: OwnWorkAssignment[] | null
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const postingDataAvailable = Array.isArray(rawPostings)
  const assignmentDataAvailable = Array.isArray(rawAssignments)
  const postings = postingDataAvailable ? rawPostings.filter(isLocalWorkPosting) : []
  const assignments = assignmentDataAvailable ? rawAssignments.filter(isOwnWorkAssignment) : []
  const invalidPostingCount = postingDataAvailable ? rawPostings.length - postings.length : 0
  const invalidAssignmentCount = assignmentDataAvailable ? rawAssignments.length - assignments.length : 0
  const activeAssignments = assignments.filter(isActiveAssignment)
  const endedAssignments = assignments.filter((assignment) => !isActiveAssignment(assignment))
  const summary = loading
    ? '读取中'
    : !projectionAvailable
      ? '角色投影未载入'
      : !postingDataAvailable && !assignmentDataAvailable
        ? '职业数据未提供'
        : `${postings.length} 个本地岗位 · ${assignments.length} 条任职记录`

  return <details className="shipment-panel occupation-panel" aria-label="工作岗位与任职记录">
    <summary className="shipment-panel-summary">
      <span><Icon name="activity" size={14} />职业</span>
      <small>{summary}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取职业投影…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的职业信息。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && <div className="occupation-content">
      <section className="occupation-section" aria-labelledby="occupation-postings-title">
        <header className="shipment-card-heading occupation-heading"><strong id="occupation-postings-title">本地空缺岗位</strong><small>{postingDataAvailable ? `${postings.length} 个` : '投影未提供'}</small></header>
        {invalidPostingCount > 0 && <p className="shipment-empty" role="alert">有 {invalidPostingCount} 个岗位字段无法读取，未将其显示为没有空缺。</p>}
        {!postingDataAvailable
          ? <p className="shipment-empty">当前投影未提供本地岗位数据，不能据此判断是否没有空缺。</p>
          : postings.length === 0
            ? <p className="shipment-empty">当前地点没有可显示的空缺岗位。</p>
            : <div className="shipment-list occupation-list" aria-live="polite">
              {postings.map((posting, index) => {
                const skill = posting.required_skill
                const requirement = skill && typeof skill.skill_id === 'string' && skill.skill_id
                  ? `所需技能：${skill.skill_id} · 等级 ${formatExactNumber(skill.minimum_level)}`
                  : skill === null || skill === undefined
                    ? '未提供指定技能条件'
                    : '技能条件无法读取'
                return <article className="shipment-card" key={posting.posting_id || `posting-${index}`}>
                  <header className="shipment-card-heading">
                    <span className="shipment-status">当前空缺</span>
                    <small>{posting.title || '岗位名称未提供'}</small>
                  </header>
                  <p className="occupation-requirement">{requirement}</p>
                  {posting.posting_id && <p className="occupation-id">岗位 ID · {posting.posting_id}</p>}
                  <button
                    type="button"
                    className="button button-secondary button-small occupation-action"
                    onClick={() => onPrepareAction(`我明确要入职岗位 ${posting.posting_id}`)}
                    disabled={!canPrepareAction || !posting.posting_id}
                    aria-label={`把申请岗位 ${posting.title || posting.posting_id} 的行动填入输入框`}
                    title={canPrepareAction ? '填入行动草稿；发送后由规则引擎校验条件' : '清空输入框或等待当前请求完成后再操作'}
                  >准备入职</button>
                </article>
              })}
            </div>}
      </section>

      <section className="occupation-section" aria-labelledby="occupation-history-title">
        <header className="shipment-card-heading occupation-heading"><strong id="occupation-history-title">我的任职记录</strong><small>{assignmentDataAvailable ? `${activeAssignments.length} 项在任 · ${endedAssignments.length} 项过往` : '投影未提供'}</small></header>
        {invalidAssignmentCount > 0 && <p className="shipment-empty" role="alert">有 {invalidAssignmentCount} 条任职记录无法读取，已从列表中隐藏。</p>}
        {!assignmentDataAvailable
          ? <p className="shipment-empty">当前投影未提供任职记录数据，不能据此判断履历为空。</p>
          : assignments.length === 0
            ? <p className="shipment-empty">当前投影没有你的任职记录。</p>
            : <div className="shipment-list occupation-list" aria-live="polite">
              {assignments.map((assignment, index) => {
                const active = isActiveAssignment(assignment)
                return <article className="shipment-card" key={assignment.assignment_id || `assignment-${index}`}>
                  <header className="shipment-card-heading">
                    <span className={`shipment-status ${active ? '' : 'shipment-status-spoiled'}`}>{active ? '在任' : '已结束'}</span>
                    <small>{assignment.title || '岗位名称未提供'}</small>
                  </header>
                  <dl className="shipment-parties occupation-ticks">
                    <div><dt>开始 tick</dt><dd>{formatTick(assignment.started_tick)}</dd></div>
                    {!active && <div><dt>结束 tick</dt><dd>{formatTick(assignment.ended_tick)}</dd></div>}
                  </dl>
                  {assignment.assignment_id && <p className="occupation-id">任职 ID · {assignment.assignment_id}</p>}
                  {active && isWorkShiftOffer(assignment.work_shift) && <div className="occupation-shift" aria-label="已登记班次条件">
                    <dl className="shipment-parties occupation-ticks">
                      <div><dt>班次时长</dt><dd>{formatDuration(assignment.work_shift.duration_seconds)}</dd></div>
                      <div><dt>报酬</dt><dd>{assignment.work_shift.wage} · {assignment.work_shift.resource_id}</dd></div>
                    </dl>
                    <button
                      type="button"
                      className="button button-secondary button-small occupation-action"
                      onClick={() => onPrepareAction(`我明确要求完成一班工作，任职 ID 是 ${assignment.assignment_id}`)}
                      disabled={!canPrepareAction || !assignment.work_shift.can_work_shift || !assignment.assignment_id}
                      aria-label={`把完成 ${assignment.title || '当前岗位'} 一班工作的行动填入输入框`}
                      title={!assignment.work_shift.can_work_shift
                        ? '当前条件未满足，不能准备开工'
                        : canPrepareAction
                          ? '填入行动草稿；发送后由规则引擎结算班次与报酬'
                          : '清空输入框或等待当前请求完成后再操作'}
                    >准备完成一班</button>
                    {!assignment.work_shift.can_work_shift && <p className="occupation-shift-note" role="status">当前条件未满足，暂不能开始班次。</p>}
                  </div>}
                  {active && assignment.work_shift !== undefined && assignment.work_shift !== null && !isWorkShiftOffer(assignment.work_shift) && <p className="occupation-shift-note" role="alert">班次条件无法读取，已停用开工草稿。</p>}
                  {active && assignment.assignment_id && <button
                    type="button"
                    className="button button-secondary button-small occupation-action"
                    onClick={() => onPrepareAction(`我明确要从岗位 ${assignment.assignment_id} 辞职`)}
                    disabled={!canPrepareAction}
                    aria-label={`把从 ${assignment.title || '当前岗位'} 辞职的行动填入输入框`}
                    title={canPrepareAction ? '填入行动草稿；发送后由规则引擎校验在任状态' : '清空输入框或等待当前请求完成后再操作'}
                  >准备辞职</button>}
                </article>
              })}
            </div>}
      </section>
    </div>}
  </details>
}

function isActiveAssignment(assignment: OwnWorkAssignment): boolean {
  return assignment.ended_tick === undefined || assignment.ended_tick === null
}

function formatTick(value: string | number | null | undefined): string {
  if (typeof value === 'string') return value.trim() || '未提供'
  if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value)
  return '无法精确读取'
}

function formatExactNumber(value: string | number | undefined): string {
  if (typeof value === 'string') return value
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '未提供'
}

function formatDuration(value: number): string {
  return Number.isSafeInteger(value) && value > 0 ? `${value.toLocaleString('zh-HK')} 秒` : '时长无法读取'
}

function isWorkShiftOffer(value: unknown): value is WorkShiftOffer {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const shift = value as Partial<WorkShiftOffer>
  return typeof shift.duration_seconds === 'number'
    && Number.isSafeInteger(shift.duration_seconds)
    && shift.duration_seconds > 0
    && typeof shift.resource_id === 'string'
    && shift.resource_id.trim().length > 0
    && typeof shift.wage === 'string'
    && shift.wage.trim().length > 0
    && typeof shift.can_work_shift === 'boolean'
}

function isLocalWorkPosting(value: unknown): value is LocalWorkPosting {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const posting = value as Partial<LocalWorkPosting>
  return typeof posting.posting_id === 'string'
    && typeof posting.title === 'string'
    && (typeof posting.revision === 'string' || typeof posting.revision === 'number')
}

function isOwnWorkAssignment(value: unknown): value is OwnWorkAssignment {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const assignment = value as Partial<OwnWorkAssignment>
  return typeof assignment.assignment_id === 'string'
    && typeof assignment.posting_id === 'string'
    && typeof assignment.title === 'string'
    && (typeof assignment.started_tick === 'string' || typeof assignment.started_tick === 'number')
    && (typeof assignment.revision === 'string' || typeof assignment.revision === 'number')
    && (assignment.ended_tick === undefined || assignment.ended_tick === null || typeof assignment.ended_tick === 'string' || typeof assignment.ended_tick === 'number')
}
