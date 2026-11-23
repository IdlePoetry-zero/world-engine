import { ApiError } from './api/client'

export const FOREGROUND_STATUS_POLL_INTERVAL_MS = 1_500

export function initials(value: string) { return value.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => Array.from(part)[0]?.toUpperCase() ?? '').join('') || 'WE' }
export function worldStatusLabel(status: string) { return ({ active: '运行中', ready: '可用', paused: '已暂停', archived: '已归档', draft: '草稿' } as Record<string, string>)[status] ?? status }
export function toError(value: unknown): Error { return value instanceof Error ? value : new Error('连接失败，请检查本地服务。') }
export function errorMessage(value: unknown): string { return value instanceof ApiError ? `${value.message}${value.correlation_id ? ` · ${value.correlation_id}` : ''}` : value instanceof Error ? value.message : '请求失败，请检查本地服务后重试。' }

export function jobReasonLabel(reason: string): string {

  const labels: Record<string, string> = {

    stop_condition_already_satisfied: '停止条件已满足',

    stop_condition_reached: '已到达停止条件',

    action_step_complete: '行动规则到达一个结算边界',

    discovered: '搜查发现了新线索，可在本角色的调查记录中查看',
    nothing_hidden_found: '搜查已结束，暂未发现新线索',
    search_failed: '搜查已结束，暂未发现新线索',
    exploration_site_unavailable: '角色已离开原处或原地点无法继续搜查',
    explorer_unavailable: '角色当前无法继续搜查',

    plan_constraint_do_not_eat: '计划要求停止进食活动',

    plan_constraint_do_not_rest: '计划要求停止休息活动',

    death_committed_succession_pending: '死亡后的继承结算待处理',

    actor_lifespan_expired: '角色寿命已尽；模拟已终止',

    actor_incapacitated: '角色因伤失去行动能力；长行动已暂停',

    died_of_deprivation: '角色因匮乏死亡；模拟已终止',

    background_deprivation_pending: '非行动人物已到生存匮乏边界，等待规则处理',

    background_deprivation_boundary: '行动已到非行动人物生存匮乏边界',

    scheduled_event_overdue_reconciliation_required: '存在逾期计划事件；模拟已暂停，需人工复核对账',

    accepted_model_decision_requires_rule_resolution: '模型裁定仍需规则确认',

    MODEL_DECISION_UNAVAILABLE: '当前模型无法完成裁定，可检查模型连接后继续',

    insufficient_storage: '存储空间不足，进度已保留。腾出空间后可继续原作业',

    STALE_START_VERSION: '行动暂停期间世界已被修改',

    BRANCH_CLOCK_CHANGED: '行动暂停期间世界时间已变化',
    insufficient_resource: '所持物资不足',
    inventory_insufficient: '背包内可用物资不足',
    resource_not_suitable: '指定物资无法用于该项补给',
    need_satisfied: '该项需求已满足',
    healing_unavailable: '当前没有可以恢复的生命值',

    work_balance_target_reached: '余额目标已达成；重复工作已停止',

    work_shift_boundary_required: '已遇到班次中的重要结算边界；当前工作计划已暂停',
    work_attendance_started: '本班已开始，工资将在班次完成后结算',
    work_shift_interrupted: '本班中断，未发放本班工资',
    work_shift_not_paid: '本班未能结薪，请核对任职状态和雇主余额',
    work_slot_already_in_progress: '这个岗位已有未结算的班次',

    work_shift_start_missed: '错过了预定班次开始时间；工作计划已停止',

    work_assignment_unavailable: '原任职已不可用；工作计划已停止',

    work_shift_survival_care_required: '角色需要先处理生存照护；工作计划已停止',

    travel_itinerary_completed: '已抵达授权行程终点',

    waypoint_opportunity: '角色已实际到达途经地点，并遇到已登记的可选停留机会；可在此选择互动，或继续原行程',

    waypoint_opportunity_stop: '已在实际途经地点选择停留；可在原授权时限内处理此处事项后继续行程',

    waypoint_opportunity_stale: '世界状态已变化；本次选择未应用，行程已暂停。恢复时会重新核验地点与机会，再询问选择',

    itinerary_auto_care_resource_unavailable: '角色没有可用且自有的食物或水；行程已暂停',

    itinerary_auto_care_unsafe: '当前条件不适合自动休息或补给；行程已暂停',

    itinerary_auto_care_boundary_required: '行程遇到需要处理的事件边界；自动照护已暂停',

    itinerary_auto_care_deadline: '已到达授权时间期限；行程已停止',

    itinerary_auto_care_deadline_exceeded: '自动休息或补给会超出授权时间期限；行程已停止',

    route_access_denied: '当前缺少此路线要求的通行许可，取得许可后可继续，也可以改走其他路线',

  }

  if (labels[reason]) return labels[reason]

  if (reason.endsWith('_has_no_registered_rule')) return '世界尚未登记该活动规则'

  if (reason.endsWith('_exceeds_requested_window')) return '规则步骤超出所选时间窗口'

  return `模拟边界 ${reason}`

}
