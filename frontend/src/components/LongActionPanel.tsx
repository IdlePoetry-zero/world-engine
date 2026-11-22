import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError, type ItineraryRouteOption, type JobDecision, type JobStatusRecord, type LongActionItineraryProgress, type LongActionRequest, type LongActionWorkProgress, type LongActionWorkPurchaseAffordability, type LongActionWorkPurchaseTarget, type LongActionWorkResume, type MarketOfferQuote, type OwnActiveTravel, type OwnStudySkill, type OwnWorkAssignment, type Settings, type TravelOption, type WorkShiftOffer, type WorldAdvanceRequest } from '../api/client'

import { Icon } from './Icon'

import type { Notice } from '../App'

import { errorMessage, FOREGROUND_STATUS_POLL_INTERVAL_MS, jobReasonLabel } from '../utils'



type Activity = LongActionRequest['activity']
type TravelPlanMode = 'single' | 'itinerary'

type Unit = 'second' | 'hour' | 'day' | 'year'

type ProgressRequest = { kind: 'long_action'; body: LongActionRequest } | { kind: 'world_advance'; body: WorldAdvanceRequest }



const OPEN_STATUSES = new Set(['queued', 'running', 'paused', 'waiting_input'])

const POLLED_STATUSES = new Set(['queued', 'running', 'waiting_input'])

function isNonresumableItineraryPause(job: JobStatusRecord): boolean {
  return job.job_type === 'simulation.long_action'
    && job.status === 'paused'
    && job.itinerary_requires_replan === true
}

function isBlockingOpenJob(job: JobStatusRecord): boolean {
  return OPEN_STATUSES.has(job.status) && !isNonresumableItineraryPause(job)
}

function isStructuralItineraryPause(job: JobStatusRecord | null): boolean {
  return job !== null
    && job.job_type === 'simulation.long_action'
    && job.status === 'paused'
    && STRUCTURAL_ITINERARY_REPLAN_REASONS.has(job.reason_code ?? '')
    && readItineraryProgress(job.itinerary_progress) !== null
}

const WORK_REASON_CODES = new Set([
  'work_balance_target_reached',
  'work_shift_boundary_required',
  'work_shift_start_missed',
  'work_assignment_unavailable',
  'work_shift_survival_care_required',
])

const STRUCTURAL_ITINERARY_REPLAN_REASONS = new Set([
  'route_unavailable',
  'origin_unavailable',
  'destination_unavailable',
  'route_map_invalid',
  'route_not_registered',
  'route_closed',
  'route_snapshot_changed',
])

const ITINERARY_PAUSE_GUIDANCE: Record<string, string> = {
  route_access_denied: '尝试下一段时，该路线已没有有效通行许可。取得许可后可继续原行程；如需改走其他路线，请先结束当前暂停任务。',
  route_unavailable: '尝试下一段时，原登记路线当前不可用。条件恢复、角色仍在该段起点且未超过原时限时，可尝试继续；也可提交替代行程，旧记录和已抵达回执会保留。',
  origin_unavailable: '原定下一段的出发地点当前不可用。恢复后可继续原行程；如要从其他地点出发，请重新规划。',
  destination_unavailable: '原定下一段的目的地当前不可用。条件恢复后可继续；如要改去其他地点，请重新规划。',
  route_map_invalid: '原定下一段的出发地点没有可用的登记路线图。请核对路线设置后继续，或重新规划。',
  route_not_registered: '原定下一段已不在出发地点的登记路线中。请核对路线设置后继续，或重新规划。',
  route_closed: '原定下一段路线目前已关闭。路线重新开放、角色仍在该段起点且未超过原时限时，可尝试继续；也可提交替代行程，旧记录和已抵达回执会保留。',
  route_snapshot_changed: '原定下一段的路线信息已变化，与行程授权时不一致。原路线信息恢复一致、角色仍在该段起点且未超过原时限时，可尝试继续；也可提交替代行程，旧记录和已抵达回执会保留。',
  destination_not_observed: '原定下一段的目的地尚未在当前角色投影中确认可见。请先更新可见地点资料，或重新规划可见路线。',
  actor_location_changed: '角色当前地点与原定下一段的起点不一致。请按角色当前实际所在地重新规划。',
  route_hazard: '途中遇到险情，当前路段未确认抵达目的地',
  travel_journey_interrupted: '当前路段已中断，未确认抵达目的地',
  deadline_passed: '授权时限已过，尚未完成的路段未获继续授权',
  remaining_route_cannot_fit_deadline: '剩余路线无法在原授权时限内完成',
  itinerary_auto_care_resource_unavailable: '途中自动照护缺少角色自有且可用的食物或饮水。补足所需补给后可继续原行程。',
  itinerary_auto_care_unsafe: '当前位置的条件不适合已授权的自动休息或补给；处理后再继续。',
  itinerary_auto_care_boundary_required: '自动休息或补给遇到需要玩家处理的事件边界；处理该边界后再继续。',
  itinerary_auto_care_deadline: '自动休息或补给会超出本次授权时限；请取消当前作业，再重新授权更长时间窗口。',
  itinerary_auto_care_deadline_exceeded: '自动休息或补给所需时间超出本次授权时限；请取消当前作业，再重新授权更长时间窗口。',
}

function itineraryPauseGuidance(reasonCode: string | null): string | null {
  if (!reasonCode) return null
  return ITINERARY_PAUSE_GUIDANCE[reasonCode] ?? jobReasonLabel(reasonCode)
}

const STUDY_SKILL_LABELS: Record<string, string> = {
  'skill.agriculture': '农业',
  'skill.care': '照护',
  'skill.cultivation_practice': '修习',
  'skill.investigation': '调查',
  'skill.local_knowledge': '地方知识',
  'skill.mediation': '调解',
  'skill.navigation': '导航',
  'skill.repair': '维修',
  'skill.research': '研究',
  'skill.social': '社交',
}



export function LongActionPanel({

  worldId,

  branchId,

  currentTick,

  stateVersion,

  daySeconds,
  yearDays,
  travelOptions,
  itineraryRouteOptions,
  ownActiveTravel,
  currentPlaceId,
  ownStudySkills,
  ownWorkAssignments,
  marketOffers,
  marketCurrency,
  actorId,
  chatLongActionJob,

  profile,

  profileLoading,

  profileError,

  disabled,

  onRefresh,

  onNotice,

}: {

  worldId: string

  branchId: string | null

  currentTick: string

  stateVersion: string

  daySeconds?: number
  yearDays?: number
  travelOptions?: TravelOption[] | null
  itineraryRouteOptions?: ItineraryRouteOption[] | null
  ownActiveTravel?: OwnActiveTravel | null
  currentPlaceId?: string | null
  ownStudySkills?: OwnStudySkill[] | null
  ownWorkAssignments?: OwnWorkAssignment[] | null
  marketOffers?: MarketOfferQuote[] | null
  marketCurrency?: string | null
  actorId?: string | null
  chatLongActionJob?: { jobId: string; worldId: string; branchId: string; actorId: string } | null

  profile: Pick<Settings, 'speed' | 'depth'> | null

  profileLoading: boolean

  profileError: string | null

  disabled: boolean

  onRefresh: () => void

  onNotice: (notice: Notice) => void

}) {

  const scopeKey = branchId ? `${worldId}:${branchId}` : ''
  const actorKey = actorId ?? ''
  const scopedChatJobId = chatLongActionJob?.worldId === worldId
    && chatLongActionJob.branchId === branchId
    && chatLongActionJob.actorId === actorId
    ? chatLongActionJob.jobId
    : null

  const [activitySelection, setActivitySelection] = useState<{ actorKey: string; activity: Activity } | null>(null)
  const [selectedRouteId, setSelectedRouteId] = useState('')
  const [travelPlanSelection, setTravelPlanSelection] = useState<{ key: string; mode: TravelPlanMode } | null>(null)
  const [travelItinerarySelection, setTravelItinerarySelection] = useState<{ key: string; routeIds: string[]; autoCare: boolean } | null>(null)
  const [studySkillSelection, setStudySkillSelection] = useState<{ actorKey: string; skillId: string } | null>(null)
  const [studySafetyThresholdDraft, setStudySafetyThresholdDraft] = useState<{ actorKey: string; value: string } | null>(null)
  const [workAssignmentSelection, setWorkAssignmentSelection] = useState<{ key: string; assignmentId: string } | null>(null)
  const [workScheduleDraft, setWorkScheduleDraft] = useState<{ key: string; firstShiftTick: string; repeatEveryDays: string; balanceTarget: string; purchaseTargetListingId: string; purchaseTargetQuantity: string } | null>(null)

  const [progressMode, setProgressMode] = useState<'character' | 'world'>('character')

  const [confirmedWorldAdvanceKey, setConfirmedWorldAdvanceKey] = useState('')

  const [duration, setDuration] = useState('1')
  const [additionalDays, setAdditionalDays] = useState('0')

  const [unit, setUnit] = useState<Unit>('hour')

  const [jobsLoad, setJobsLoad] = useState<{ key: string; attempt: number; items: JobStatusRecord[]; loading: boolean; error?: string } | null>(null)

  const [selectedJob, setSelectedJob] = useState<{ key: string; id: string; manual: boolean } | null>(null)

  const [listAttempt, setListAttempt] = useState(0)

  const [submission, setSubmission] = useState<{ key: string; request: ProgressRequest } | null>(null)

  const [submissionError, setSubmissionError] = useState<string | null>(null)

  const [submitting, setSubmitting] = useState(false)

  const [controlBusy, setControlBusy] = useState<string | null>(null)

  const [statusError, setStatusError] = useState<string | null>(null)

  const [decisionLoad, setDecisionLoad] = useState<{ jobId: string; attempt: number; items: JobDecision[]; loading: boolean; error?: string } | null>(null)

  const [decisionAttempt, setDecisionAttempt] = useState(0)

  const [decisionBusy, setDecisionBusy] = useState<string | null>(null)

  const [decisionActionError, setDecisionActionError] = useState<{ jobId: string; requestId: string; optionId: string; message: string } | null>(null)

  const refreshedCommitCursors = useRef(new Map<string, string>())

  const lastRefreshAt = useRef(0)

  const mounted = useRef(true)
  const latestScopeKey = useRef(scopeKey)



  const jobs = jobsLoad?.key === scopeKey ? jobsLoad.items : []

  const jobsLoading = Boolean(scopeKey && (!jobsLoad || jobsLoad.key !== scopeKey || jobsLoad.attempt !== listAttempt || jobsLoad.loading))

  const jobsError = jobsLoad?.key === scopeKey && jobsLoad.attempt === listAttempt ? jobsLoad.error : undefined

  const selectedJobId = selectedJob?.key === scopeKey ? selectedJob.id : null

  const currentJob = jobs.find((job) => job.job_id === selectedJobId) ?? null
  const currentJobProgress = currentJob ? jobTickProgress(currentJob) : null
  const currentJobSucceededBeforeDeadline = currentJob?.status === 'succeeded'
    && currentJobProgress !== null
    && currentJobProgress.committed < currentJobProgress.target
  const currentJobWindowUsage = currentJobProgress ? {
    used: currentJobProgress.committed <= currentJobProgress.started
      ? 0n
      : currentJobProgress.committed >= currentJobProgress.target
        ? currentJobProgress.target - currentJobProgress.started
        : currentJobProgress.committed - currentJobProgress.started,
    maximum: currentJobProgress.target - currentJobProgress.started,
  } : null
  const currentWorkProgress = currentJob?.job_type === 'simulation.long_action'
    ? readWorkProgress(currentJob.work_progress)
    : null
  const currentJobHasOwnWorkReadback = currentJob?.job_type === 'simulation.long_action'
    && Object.prototype.hasOwnProperty.call(currentJob, 'work_progress')
  const currentWorkPurchaseTarget = currentJob && currentJobHasOwnWorkReadback
    ? readWorkPurchaseTarget(currentJob.work_purchase_target)
    : null
  const currentWorkPurchaseAffordability = currentJob && currentJobHasOwnWorkReadback
    ? readWorkPurchaseAffordability(currentJob.work_purchase_affordability, currentWorkPurchaseTarget)
    : null
  const currentPurchaseTargetAffordable = Boolean(currentJob && currentJobHasOwnWorkReadback
    && currentJob.reason_code === 'work_purchase_target_affordable')
  const currentPurchaseTargetUnavailable = Boolean(currentJob && currentJobHasOwnWorkReadback
    && currentJob.status === 'paused'
    && currentJob.reason_code === 'work_purchase_target_unavailable')
  const currentWorkResume = currentJobHasOwnWorkReadback
    ? readWorkResume(currentJob?.work_resume)
    : null
  const currentItineraryProgress = currentJob?.job_type === 'simulation.long_action'
    ? readItineraryProgress(currentJob.itinerary_progress)
    : null
  const currentJobRequiresItineraryReplan = currentJob ? isNonresumableItineraryPause(currentJob) : false
  const currentJobCanOpenItineraryReplan = currentJobRequiresItineraryReplan || isStructuralItineraryPause(currentJob)
  const currentJobPauseGuidance = currentJob?.reason_code
    ? currentItineraryProgress
      ? itineraryPauseGuidance(currentJob.reason_code)
      : currentJob.reason_code === 'work_purchase_target_unavailable'
        ? '商品储蓄目标当前无法核验；工作已暂停'
        : jobReasonLabel(currentJob.reason_code)
    : null
  const currentJobStudySafetyThreshold = currentJob
    && isStudySafetyThreshold(currentJob.study_stop_safety_at_or_below)
    ? currentJob.study_stop_safety_at_or_below
    : null

  const currentJobId = currentJob?.job_id

  const currentJobStatus = currentJob?.status

  const currentDecisionLoad = decisionLoad && decisionLoad.jobId === currentJobId && decisionLoad.attempt === decisionAttempt ? decisionLoad : null

  const decisionsLoading = currentJobStatus === 'waiting_input' && (!currentDecisionLoad || currentDecisionLoad.loading)

  const decisionLoadError = currentDecisionLoad?.error

  const decisionItems = currentDecisionLoad?.items ?? []

  const currentDecisionAction = decisionActionError && decisionActionError.jobId === currentJobId ? decisionActionError : null

  const currentDecisionActionError = currentDecisionAction?.message ?? null

  const blockingJobs = jobs.filter(isBlockingOpenJob)
  const activeJob = blockingJobs[0] ?? null

  const activeJourney = ownActiveTravel?.status === 'in_transit' ? ownActiveTravel : null
  const availableStudySkills = useMemo(
    () => Array.isArray(ownStudySkills) ? ownStudySkills.filter(isOwnStudySkill) : [],
    [ownStudySkills],
  )
  const availableWorkAssignments = useMemo(
    () => Array.isArray(ownWorkAssignments) ? ownWorkAssignments.filter(isSchedulableWorkAssignment) : [],
    [ownWorkAssignments],
  )
  const activity = activitySelection?.actorKey === actorKey ? activitySelection.activity : 'wait'
  const selectedStudySkillId = studySkillSelection?.actorKey === actorKey ? studySkillSelection.skillId : ''
  const selectedStudySkill = availableStudySkills.find((skill) => skill.skill_id === selectedStudySkillId)
    ?? availableStudySkills[0]
    ?? null
  const studySafetyThresholdText = studySafetyThresholdDraft?.actorKey === actorKey
    ? studySafetyThresholdDraft.value
    : ''
  const studySafetyThreshold = parseStudySafetyThreshold(studySafetyThresholdText)
  const studySafetyThresholdValid = studySafetyThresholdText.trim() === '' || studySafetyThreshold !== null
  const localTravelOptions = Array.isArray(travelOptions)
    ? travelOptions.filter((option) => Boolean(option.route_id) && parsePositiveTravelTicks(option.travel_ticks) !== null)
    : []
  const selectedTravelRoute = localTravelOptions.find((option) => option.route_id === selectedRouteId) ?? localTravelOptions[0] ?? null
  const travelPlanKey = `${scopeKey}:${actorKey}:${currentPlaceId ?? ''}`
  const localItineraryRouteOptions = useMemo(() => {
    const validOptions = Array.isArray(itineraryRouteOptions) ? itineraryRouteOptions.filter(isItineraryRouteOption) : []
    const counts = new Map<string, number>()
    validOptions.forEach((option) => counts.set(option.route_id, (counts.get(option.route_id) ?? 0) + 1))
    return validOptions.filter((option) => counts.get(option.route_id) === 1)
  }, [itineraryRouteOptions])
  const hasItineraryStartOptions = Boolean(currentPlaceId)
    && localItineraryRouteOptions.some((option) => option.from_place_id === currentPlaceId)
  const selectedTravelMode: TravelPlanMode = !activeJourney && travelPlanSelection?.key === travelPlanKey
    ? travelPlanSelection.mode
    : 'single'
  const selectedItineraryRouteIds = travelItinerarySelection?.key === travelPlanKey
    ? travelItinerarySelection.routeIds
    : []
  const selectedItineraryAutoCare = travelItinerarySelection?.key === travelPlanKey
    ? travelItinerarySelection.autoCare
    : false
  const itineraryRouteById = new Map(localItineraryRouteOptions.map((option) => [option.route_id, option]))
  const selectedItineraryRoutes = selectedItineraryRouteIds.flatMap((routeId) => {
    const option = itineraryRouteById.get(routeId)
    return option ? [option] : []
  })
  const itineraryPathConnected = Boolean(currentPlaceId)
    && selectedItineraryRoutes.length > 0
    && selectedItineraryRoutes.length === selectedItineraryRouteIds.length
    && selectedItineraryRoutes.every((route, index) => route.from_place_id === (index === 0 ? currentPlaceId : selectedItineraryRoutes[index - 1].to_place_id))
  const itineraryPathValid = selectedItineraryRouteIds.length === 0
    ? Boolean(currentPlaceId)
    : itineraryPathConnected
  const itineraryNextOriginId = selectedItineraryRoutes.at(-1)?.to_place_id ?? currentPlaceId ?? ''
  const connectedItineraryOptions = !activeJourney && itineraryPathValid && itineraryNextOriginId
    ? localItineraryRouteOptions.filter((option) => option.from_place_id === itineraryNextOriginId && !selectedItineraryRouteIds.includes(option.route_id))
    : []
  const itineraryTotalTicks = itineraryPathConnected
    ? selectedItineraryRoutes.reduce<bigint | null>((total, option) => {
      const legTicks = parsePositiveTravelTicks(option.travel_ticks)
      return total === null || legTicks === null ? null : total + legTicks
    }, 0n)
    : null
  const validItineraryPlan = Boolean(currentPlaceId && itineraryPathConnected && selectedItineraryRouteIds.length > 0)
  const workDraftKey = `${scopeKey}:${actorKey}`
  const selectedWorkAssignmentId = workAssignmentSelection?.key === workDraftKey
    ? workAssignmentSelection.assignmentId
    : ''
  const selectedWorkAssignment = availableWorkAssignments.find((assignment) => assignment.assignment_id === selectedWorkAssignmentId)
    ?? availableWorkAssignments[0]
    ?? null
  const wageCurrency = selectedWorkAssignment?.work_shift.resource_id.trim() ?? ''
  const localPurchaseOffers = wageCurrency && marketCurrency === wageCurrency
    ? (marketOffers ?? []).filter(isPurchaseTargetOffer)
    : []
  const defaultFirstShiftTick = safeIntegerText(currentTick) === null ? '' : String(safeIntegerText(currentTick))
  const workScheduleValues = workScheduleDraft?.key === workDraftKey
    ? workScheduleDraft
    : { key: workDraftKey, firstShiftTick: defaultFirstShiftTick, repeatEveryDays: '', balanceTarget: '', purchaseTargetListingId: '', purchaseTargetQuantity: '' }
  const firstShiftTickNumber = safeIntegerText(workScheduleValues.firstShiftTick)
  const firstShiftTick = firstShiftTickNumber === null ? null : BigInt(firstShiftTickNumber)
  const repeatEveryDays = Number(workScheduleValues.repeatEveryDays)
  const balanceTarget = workScheduleValues.balanceTarget.trim()
  const balanceTargetValid = balanceTarget === '' || isExactNonNegativeDecimal(balanceTarget)
  const purchaseTargetListingId = workScheduleValues.purchaseTargetListingId.trim()
  const purchaseTargetQuantity = workScheduleValues.purchaseTargetQuantity.trim()
  const purchaseTargetFieldsPresent = purchaseTargetListingId !== '' || purchaseTargetQuantity !== ''
  const selectedPurchaseOffer = localPurchaseOffers.find((offer) => offer.listing_id === purchaseTargetListingId) ?? null
  const purchaseQuantityComparison = selectedPurchaseOffer !== null
    ? compareExactDecimals(purchaseTargetQuantity, selectedPurchaseOffer.minimum_quantity)
    : null
  const purchaseQuantityMeetsMinimum = purchaseQuantityComparison !== null && purchaseQuantityComparison >= 0
  const purchaseTargetError = !purchaseTargetFieldsPresent
    ? null
    : !selectedPurchaseOffer
      ? '请选择当前工资币种下仍可见的本地挂牌。'
      : !isExactPositiveDecimal(purchaseTargetQuantity)
        ? '请输入正的精确十进制商品数量。'
        : !purchaseQuantityMeetsMinimum
          ? `数量不可低于挂牌最低量 ${selectedPurchaseOffer.minimum_quantity}。`
          : null
  const conflictingWorkSavingsGoals = balanceTarget !== '' && purchaseTargetFieldsPresent
  const currentActivity: Activity = activeJourney
    ? 'travel'
    : (activity === 'travel' && localTravelOptions.length === 0 && !hasItineraryStartOptions)
      || (activity === 'study' && selectedStudySkill === null)
      || (activity === 'work' && selectedWorkAssignment === null) ? 'wait' : activity
  const explicitReplacementItinerary = progressMode === 'character'
    && currentActivity === 'travel'
    && selectedTravelMode === 'itinerary'
    && validItineraryPlan
  const structuralItineraryPauseCount = blockingJobs.filter(isStructuralItineraryPause).length
  const otherBlockingJobCount = blockingJobs.length - structuralItineraryPauseCount
  const canReplaceStructuralItineraryPause = structuralItineraryPauseCount > 0 && otherBlockingJobCount === 0
  const canSubmitReplacementItinerary = explicitReplacementItinerary && canReplaceStructuralItineraryPause
  const activeJobBlocksStart = activeJob !== null && !canSubmitReplacementItinerary
  const travelTicksMinimum = currentActivity === 'travel' && !activeJourney && selectedTravelRoute
    && selectedTravelMode === 'single'
      ? parsePositiveTravelTicks(selectedTravelRoute.travel_ticks)
    : null

  const hasWorldDayLength = Number.isSafeInteger(daySeconds) && (daySeconds ?? 0) > 0
  const dayLength = hasWorldDayLength ? daySeconds! : 0
  const hasWorldCalendar = hasWorldDayLength && Number.isSafeInteger(yearDays) && (yearDays ?? 0) > 0
  const yearLength = hasWorldCalendar ? yearDays! : 0
  const amount = Number(duration)
  const additionalDayAmount = Number(additionalDays)
  const amountMaximum = unit === 'year' ? 600 : unit === 'second' ? 31_536_000 : 3_650
  const amountValid = Number.isSafeInteger(amount)
    && (unit === 'year' ? amount >= 0 : amount >= 1)
    && amount <= amountMaximum
  const requestedCalendarDays = unit === 'year'
    ? amount * yearLength + additionalDayAmount
    : unit === 'day' ? amount : null
  const additionalDaysValid = unit !== 'year' || (
    Number.isSafeInteger(additionalDayAmount)
    && additionalDayAmount >= 0
    && additionalDayAmount < yearLength
    && requestedCalendarDays !== null
    && requestedCalendarDays <= 600 * yearLength
  )
  const durationSeconds = unit === 'year'
    ? (amount * yearLength + additionalDayAmount) * dayLength
    : amount * (unit === 'second' ? 1 : unit === 'hour' ? 3_600 : dayLength)
  const hasSafeDuration = Number.isSafeInteger(durationSeconds) && durationSeconds > 0
  const baseTick = parseTick(currentTick)
  const targetTickPreview = hasSafeDuration && baseTick !== null ? baseTick + BigInt(durationSeconds) : null
  const workFirstShiftValid = firstShiftTick !== null && baseTick !== null && targetTickPreview !== null
    && firstShiftTick >= baseTick
    && firstShiftTick + BigInt(selectedWorkAssignment?.work_shift.duration_seconds ?? 0) <= targetTickPreview
  const validWorkSchedule = Boolean(
    selectedWorkAssignment
    && Number.isSafeInteger(repeatEveryDays)
    && repeatEveryDays > 0
    && balanceTargetValid
    && purchaseTargetError === null
    && !conflictingWorkSavingsGoals
    && workFirstShiftValid,
  )
  const tickInRange = targetTickPreview !== null
    && targetTickPreview >= -(1n << 63n) && targetTickPreview <= (1n << 63n) - 1n
  const validDuration = amountValid && additionalDaysValid && hasSafeDuration && baseTick !== null && tickInRange
    && (unit === 'hour' || unit === 'second' || hasWorldDayLength)
    && (unit !== 'year' || hasWorldCalendar)
  const travelDurationEnough = progressMode !== 'character' || currentActivity !== 'travel'
    || Boolean(activeJourney)
    || (selectedTravelMode === 'itinerary'
      ? itineraryTotalTicks !== null && BigInt(hasSafeDuration ? durationSeconds : 0) >= itineraryTotalTicks
      : travelTicksMinimum !== null && BigInt(hasSafeDuration ? durationSeconds : 0) >= travelTicksMinimum)
  const itinerarySlackTicks = itineraryTotalTicks !== null && hasSafeDuration
    ? BigInt(durationSeconds) > itineraryTotalTicks
      ? BigInt(durationSeconds) - itineraryTotalTicks
      : 0n
    : null
  const worldAdvanceConfirmationKey = `${scopeKey}:${stateVersion}:${duration}:${unit}`

  const worldAdvanceConfirmed = confirmedWorldAdvanceKey === worldAdvanceConfirmationKey

  const canStart = Boolean(

    scopeKey && profile && !disabled && !profileLoading && !profileError

    && !jobsLoading && !jobsError && !activeJobBlocksStart && !submitting && !submission && validDuration && travelDurationEnough
    && (currentActivity !== 'travel' || Boolean(activeJourney) || (selectedTravelMode === 'itinerary' ? validItineraryPlan : Boolean(selectedTravelRoute)))
    && (currentActivity !== 'study' || Boolean(selectedStudySkill))
    && (currentActivity !== 'study' || studySafetyThresholdValid)
    && (currentActivity !== 'work' || validWorkSchedule),

  ) && (progressMode !== 'world' || worldAdvanceConfirmed)

  useEffect(() => {

    mounted.current = true

    return () => { mounted.current = false }

  }, [])

  useEffect(() => {
    latestScopeKey.current = scopeKey
  }, [scopeKey])



  useEffect(() => {

    if (!scopeKey || !branchId) return

    let active = true

    void api.listJobs(worldId, branchId).then(({ items }) => {

      if (!active) return

      const progressJobs = items.filter((job) => job.job_type === 'simulation.long_action' || job.job_type === 'simulation.world_advance')

      setStatusError(null)

      setJobsLoad({ key: scopeKey, attempt: listAttempt, items: progressJobs, loading: false })

      setSelectedJob((current) => {

        if (current?.key === scopeKey && current.manual && progressJobs.some((job) => job.job_id === current.id)) return current

        const active = progressJobs.find(isBlockingOpenJob)
        const chatJob = progressJobs.find((job) => job.job_id === scopedChatJobId && OPEN_STATUSES.has(job.status))
        const preferred = active ?? chatJob ?? progressJobs.find((job) => OPEN_STATUSES.has(job.status)) ?? progressJobs[0]

        return preferred ? { key: scopeKey, id: preferred.job_id, manual: false } : null

      })

    }).catch((reason) => {

      if (active) setJobsLoad({ key: scopeKey, attempt: listAttempt, items: [], loading: false, error: errorMessage(reason) })

    })

    return () => { active = false }

  }, [worldId, branchId, scopeKey, listAttempt, scopedChatJobId])



  const applyJobStatus = useCallback((job: JobStatusRecord) => {

    if (!mounted.current || scopeKey !== latestScopeKey.current) return

    setJobsLoad((current) => {

      if (current?.key !== scopeKey) return current

      const items = [job, ...current.items.filter((item) => item.job_id !== job.job_id)]

        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))

      return { ...current, attempt: listAttempt, items, loading: false, error: undefined }

    })

    setStatusError(null)

    const candidate = jobRefreshCursor(job, currentTick, stateVersion)
    if (candidate?.ahead) {
      const key = `${scopeKey}:${job.job_id}`
      const now = Date.now()

      if (job.status === 'running') {
        if (now - lastRefreshAt.current >= 4_000) {
          lastRefreshAt.current = now
          onRefresh()
        }
      } else if (refreshedCommitCursors.current.get(key) !== candidate.cursor) {
        refreshedCommitCursors.current.set(key, candidate.cursor)
        lastRefreshAt.current = now
        onRefresh()
      }
    }

  }, [currentTick, listAttempt, onRefresh, scopeKey, stateVersion])



  useEffect(() => {

    if (!currentJobId || !currentJobStatus || !POLLED_STATUSES.has(currentJobStatus)) return

    let active = true

    let inFlight = false

    const poll = () => {

      if (inFlight) return

      inFlight = true

      void api.jobStatus(currentJobId).then((job) => {

        if (active) applyJobStatus(job)

      }).catch((reason) => {

        if (active) setStatusError(errorMessage(reason))

      }).finally(() => { inFlight = false })

    }

    poll()

    const timer = window.setInterval(poll, FOREGROUND_STATUS_POLL_INTERVAL_MS)

    return () => { active = false; window.clearInterval(timer) }

  }, [applyJobStatus, currentJobId, currentJobStatus])



  useEffect(() => {

    if (!currentJobId || currentJobStatus !== 'waiting_input') return

    let active = true

    const jobId = currentJobId

    const attempt = decisionAttempt

    void api.jobDecisions(jobId).then(({ items }) => {

      if (active) setDecisionLoad({ jobId, attempt, items, loading: false })

    }).catch((reason) => {

      if (active) setDecisionLoad({ jobId, attempt, items: [], loading: false, error: errorMessage(reason) })

    })

    return () => { active = false }

  }, [currentJobId, currentJobStatus, decisionAttempt])



  const buildRequest = (): ProgressRequest | null => {

    if (!branchId || !profile || !validDuration) return null

    const common = {

      branch_id: branchId,

      expected_state_version: stateVersion,

      duration_seconds: durationSeconds,

      profile: { speed: profile.speed, depth: profile.depth },

    }

    if (progressMode === 'world') return { kind: 'world_advance', body: common }
    if (currentActivity === 'travel') {
      if (activeJourney) return { kind: 'long_action', body: { ...common, activity: 'travel', route_id: activeJourney.route_id } }
      if (selectedTravelMode === 'itinerary') {
        if (!validItineraryPlan) return null
        return {
          kind: 'long_action',
          body: {
            ...common,
            activity: 'travel',
            route_ids: selectedItineraryRouteIds,
            itinerary_auto_care: selectedItineraryAutoCare,
          },
        }
      }
      const routeId = selectedTravelRoute?.route_id
      if (!routeId) return null
      return { kind: 'long_action', body: { ...common, activity: 'travel', route_id: routeId } }
    }
    if (currentActivity === 'study') {
      if (!selectedStudySkill) return null
      if (!studySafetyThresholdValid) return null
      return { kind: 'long_action', body: {
        ...common,
        activity: 'study',
        skill_id: selectedStudySkill.skill_id,
        ...(studySafetyThreshold !== null ? { study_stop_safety_at_or_below: studySafetyThreshold } : {}),
      } }
    }
    if (currentActivity === 'work') {
      if (!selectedWorkAssignment || firstShiftTickNumber === null || !validWorkSchedule) return null
      return {
        kind: 'long_action',
        body: {
          ...common,
          activity: 'work',
          work_schedule: {
            assignment_id: selectedWorkAssignment.assignment_id,
            first_shift_tick: firstShiftTickNumber,
            repeat_every_days: repeatEveryDays,
            ...(selectedPurchaseOffer && purchaseTargetFieldsPresent
              ? { purchase_target: { listing_id: selectedPurchaseOffer.listing_id, quantity: purchaseTargetQuantity } }
              : balanceTarget ? { balance_target: balanceTarget } : {}),
          },
        },
      }
    }
    return { kind: 'long_action', body: { ...common, activity: currentActivity } }

  }



  const submit = async (request: ProgressRequest, idempotencyKey: string) => {

    if (submitting) return

    setSubmitting(true)

    setSubmissionError(null)

    try {

      const accepted = request.kind === 'world_advance'

        ? await api.startWorldAdvance(worldId, request.body, idempotencyKey)

        : await api.startLongAction(worldId, request.body, idempotencyKey)

      if (!mounted.current) return

      setSubmission(null)

      setSelectedJob({ key: scopeKey, id: accepted.job_id, manual: false })

      setListAttempt((attempt) => attempt + 1)

      onNotice({ kind: 'info', text: request.kind === 'world_advance' ? '全世界快进已提交；所有角色的变化只会显示在后端确认的进度中。' : '长时间行动已提交；世界只会显示后端确认的进度。' })

      try {

        const status = await api.jobStatus(accepted.job_id)

        applyJobStatus(status)

      } catch (reason) {

        if (mounted.current) setStatusError(errorMessage(reason))

      }

    } catch (reason) {

      if (!mounted.current) return

      setSubmissionError(errorMessage(reason))

      if (reason instanceof ApiError && reason.status < 500 && !reason.retryable) {

        setSubmission(null)

      } else {

        setSubmission({ key: idempotencyKey, request })

      }

    } finally {

      if (mounted.current) setSubmitting(false)

    }

  }



  const begin = () => {

    if (!canStart) return

    const request = buildRequest()

    if (!request) return

    void submit(request, crypto.randomUUID())

  }



  const retrySubmission = () => {

    if (submission && !submitting) void submit(submission.request, submission.key)

  }

  const openItineraryReplan = () => {
    setProgressMode('character')
    setActivitySelection({ actorKey, activity: 'travel' })
    setTravelPlanSelection({ key: travelPlanKey, mode: 'itinerary' })
    setTravelItinerarySelection({ key: travelPlanKey, routeIds: [], autoCare: false })
    window.requestAnimationFrame(() => {
      const controls = document.getElementById('long-action-controls')
      controls?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      controls?.focus({ preventScroll: true })
    })
  }



  const control = async (action: 'pause' | 'resume' | 'cancel') => {

    if (!currentJob || controlBusy || statusError) return

    setControlBusy(action)

    setStatusError(null)

    try {

      await api.controlJob(currentJob.job_id, action)

      if (!mounted.current) return

      const updated = await api.jobStatus(currentJob.job_id)

      applyJobStatus(updated)

    } catch (reason) {

      if (!mounted.current) return

      setStatusError(errorMessage(reason))

      // A worker can complete between polling and the control request; refresh

      // the authoritative status instead of showing the stale transition.

      try { applyJobStatus(await api.jobStatus(currentJob.job_id)) } catch { /* keep the control error visible */ }

    } finally {

      if (mounted.current) setControlBusy(null)

    }

  }



  const answerDecision = async (decision: JobDecision, optionId: string) => {

    if (!currentJobId || decisionBusy) return

    const jobId = currentJobId

    const busyKey = `${decision.request_id}:${optionId}`

    setDecisionBusy(busyKey)

    setDecisionActionError(null)

    try {

      const accepted = await api.answerJobDecision(jobId, decision.request_id, optionId)

      const acceptedReasonCode = (accepted as typeof accepted & { reason_code?: string | null }).reason_code ?? null

      if (!mounted.current) return

      setJobsLoad((current) => current?.key === scopeKey ? {

        ...current,

        items: current.items.map((job) => job.job_id === accepted.job_id

          ? { ...job, status: accepted.status, reason_code: acceptedReasonCode ?? job.reason_code, committed_through_tick: accepted.committed_through_tick ?? job.committed_through_tick }

          : job),

      } : current)

      setDecisionActionError(null)

      setDecisionAttempt((attempt) => attempt + 1)

      try {

        const updated = await api.jobStatus(jobId)

        applyJobStatus(updated)

        const reasonCode = updated.reason_code ?? acceptedReasonCode

        if (reasonCode === 'waypoint_opportunity_stale') {

          onNotice({ kind: 'info', text: jobReasonLabel('waypoint_opportunity_stale') })

        } else if (updated.status === 'paused') {

          onNotice({ kind: 'info', text: reasonCode
            ? `后端已暂停行动：${jobReasonLabel(reasonCode)}。`
            : '后端已返回行动暂停状态；请刷新以核对暂停原因。' })

        } else {

          const statusLabel: Record<string, string> = {
            queued: '排队中', running: '运行中', waiting_input: '等待输入',
            succeeded: '已完成', failed: '失败', cancelled: '已取消',
          }

          onNotice({ kind: 'info', text: `选择已提交；后端当前状态：${statusLabel[updated.status] ?? updated.status}。` })

        }

      } catch (reason) {

        if (mounted.current) {

          setStatusError(errorMessage(reason))

          if (acceptedReasonCode === 'waypoint_opportunity_stale') {

            onNotice({ kind: 'info', text: jobReasonLabel('waypoint_opportunity_stale') })

          } else if (accepted.status === 'paused') {

            onNotice({ kind: 'info', text: acceptedReasonCode
              ? `后端已暂停行动：${jobReasonLabel(acceptedReasonCode)}。`
              : '后端已返回行动暂停状态；读取暂停原因失败，暂不能确认选择是否应用。' })

          } else {

            onNotice({ kind: 'info', text: '选择已提交；读取后端最终状态失败，请稍后刷新行动状态。' })

          }

        }

      }

    } catch (reason) {

      if (mounted.current) {

        if (reason instanceof ApiError && reason.status < 500 && !reason.retryable) {

          setDecisionActionError(null)

          setDecisionAttempt((attempt) => attempt + 1)

        } else {

          setDecisionActionError({ jobId, requestId: decision.request_id, optionId, message: errorMessage(reason) })

        }

      }

    } finally {

      if (mounted.current) setDecisionBusy(null)

    }

  }



  const controlActions: ('pause' | 'resume' | 'cancel')[] = []

  if (currentJob && !statusError && !controlBusy) {

    if (currentJob.status === 'queued' || currentJob.status === 'running') controlActions.push('pause', 'cancel')

    if (currentJob.status === 'paused') {
      if (!currentJobRequiresItineraryReplan) controlActions.push('resume')
      controlActions.push('cancel')
    }

    if (currentJob.status === 'waiting_input') controlActions.push('cancel')

  }



  return (

    <section className="long-action-panel" aria-labelledby="long-action-title">

      <div className="long-action-overview">

        <div className="long-action-heading">

          <span className="long-action-mark"><Icon name="clock" size={17} /></span>

          <div>

            <div className="eyebrow"><span className="eyebrow-line" />世界时间</div>

            <h2 id="long-action-title">时间推进与长行动</h2>

            <p>选择角色行动或全世界快进；进度只按后端已提交的世界时钟显示。</p>

          </div>

        </div>

        <div className="long-action-progress" aria-live="polite">

          {currentJob ? <>

            <div className={`long-action-status status-${statusTone(currentJob.status)}`}>

              <span className="long-action-status-dot" />

              <strong>{longJobStatusLabel(currentJob.status)}</strong>

              <small>{currentJob.job_type === 'simulation.world_advance' ? '全世界快进' : '角色行动'} · {currentJob.job_id.slice(0, 8)}</small>

            </div>
            <div className="long-action-committed"><Icon name="timeline" size={14} /><span>已知提交进度</span><strong style={{ overflowWrap: 'anywhere' }}>{currentJob.committed_through_tick === null ? '暂无' : formatWorldTick(currentJob.committed_through_tick, dayLength, yearLength)}</strong></div>
            {currentJobProgress && currentJobSucceededBeforeDeadline && currentJobWindowUsage && <div className="long-action-target-progress" aria-live="off">
              <div className="long-action-target-line"><span>已用时间窗口</span><strong style={{ overflowWrap: 'anywhere' }} title={`已用 ${formatTick(currentJobWindowUsage.used)} tick / 授权 ${formatTick(currentJobWindowUsage.maximum)} tick`}>{formatWorldDuration(currentJobWindowUsage.used, dayLength, yearLength)} / {formatWorldDuration(currentJobWindowUsage.maximum, dayLength, yearLength)}</strong></div>
              <small style={{ overflowWrap: 'anywhere' }}>已用 {formatTick(currentJobWindowUsage.used)} tick / 授权 {formatTick(currentJobWindowUsage.maximum)} tick</small>
              <small>任务在授权期限前完成。</small>
            </div>}
            {currentJobProgress && !currentJobSucceededBeforeDeadline && <div className="long-action-target-progress" aria-live="off">
              <div className="long-action-target-line"><span>任务目标</span><strong style={{ overflowWrap: 'anywhere' }}>{formatWorldRelativeTick(currentJobProgress.committed, dayLength, yearLength)} / {formatWorldRelativeTick(currentJobProgress.target, dayLength, yearLength)}</strong><b>{currentJobProgress.percent.toFixed(2)}%</b></div>
              <progress max={100} value={currentJobProgress.percent} aria-label={`世界时钟提交进度 ${currentJobProgress.percent.toFixed(2)}%`} />
              <small style={{ overflowWrap: 'anywhere' }}>起点 {formatWorldRelativeTick(currentJobProgress.started, dayLength, yearLength)}；仅按后端已提交时钟计算。</small>
            </div>}
            {!currentJobProgress && currentJob.target_tick && <div className="long-action-target-line long-action-target-unavailable"><span>任务目标</span><strong style={{ overflowWrap: 'anywhere' }}>{formatWorldTick(currentJob.target_tick, dayLength, yearLength)}</strong><small style={{ overflowWrap: 'anywhere' }}>当前 API 记录未提供可计算进度的完整起点；目标 tick {currentJob.target_tick}。</small></div>}

            {currentWorkProgress && <div className="long-action-target-progress" aria-label="本人班次工资结算回执">
              <div className="long-action-target-line"><span>已结算班次</span><strong>{currentWorkProgress.shifts_paid.toLocaleString('zh-HK')} 班</strong></div>
              {Object.entries(currentWorkProgress.wages_by_resource).sort(([left], [right]) => left.localeCompare(right)).map(([resourceId, amount]) => <div className="long-action-target-line" key={resourceId}>
                <span>累计工资 · {resourceId}</span><strong>{amount}</strong>
              </div>)}
              {Object.keys(currentWorkProgress.wages_by_resource).length === 0 && <small>尚无已结算工资。</small>}
              <div className="long-action-target-line"><span>最近发薪时间</span><strong>{currentWorkProgress.last_paid_tick.trim() ? formatWorldTick(currentWorkProgress.last_paid_tick, dayLength, yearLength) : '尚无班次结算'}</strong></div>
              <div className="long-action-target-line"><span>最近结算后余额 · {currentWorkProgress.balance_resource_id}</span><strong>{currentWorkProgress.balance_after_last_pay}</strong></div>
              <small>班次数、工资及余额来自后端为本账号返回的结算回执。</small>
            </div>}
            {(currentWorkPurchaseTarget || currentWorkPurchaseAffordability || currentPurchaseTargetAffordable || currentPurchaseTargetUnavailable) && <div className="long-action-target-progress" aria-label="本人商品储蓄目标回执">
              {currentWorkPurchaseTarget && <>
                <div className="long-action-target-line"><span>目标商品</span><strong>{currentWorkPurchaseTarget.resource_id} × {currentWorkPurchaseTarget.quantity}</strong></div>
                <div className="long-action-target-line"><span>本地挂牌</span><strong style={{ overflowWrap: 'anywhere' }}>{currentWorkPurchaseTarget.listing_id}</strong></div>
                <div className="long-action-target-line"><span>工资币种</span><strong>{currentWorkPurchaseTarget.currency_resource_id}</strong></div>
              </>}
              {!currentWorkPurchaseTarget && currentWorkPurchaseAffordability && <>
                <div className="long-action-target-line"><span>核验挂牌</span><strong style={{ overflowWrap: 'anywhere' }}>{currentWorkPurchaseAffordability.listing_id}</strong></div>
                <div className="long-action-target-line"><span>目标数量</span><strong>{currentWorkPurchaseAffordability.quantity}</strong></div>
              </>}
              {currentWorkPurchaseAffordability && <>
                <div className="long-action-target-line"><span>完成 tick</span><strong style={{ overflowWrap: 'anywhere' }}>{formatWorldTick(currentWorkPurchaseAffordability.confirmed_tick, dayLength, yearLength)}</strong></div>
                <div className="long-action-target-line"><span>完成时商品总报价（最小货币单位）</span><strong>{formatExactInteger(currentWorkPurchaseAffordability.total_price_minor)} {currentWorkPurchaseAffordability.currency_resource_id}</strong></div>
                <div className="long-action-target-line"><span>完成时现金余额</span><strong>{currentWorkPurchaseAffordability.wage_balance} {currentWorkPurchaseAffordability.currency_resource_id}</strong></div>
                <small role="status">这是完成 tick 的历史报价核验，不代表现在的价格或库存；本作业没有购买商品。</small>
              </>}
              {currentPurchaseTargetAffordable && !currentWorkPurchaseAffordability && <small role="status">后端确认在完成时挂牌报价下已达到可负担条件，工作已完成；没有购买商品。</small>}
              {currentPurchaseTargetUnavailable && <small role="status">当前挂牌或报价无法按已授权目标核验，工作已暂停。确认挂牌恢复后可尝试继续；如需更换目标，请取消此作业并重新提交。</small>}
              {!currentPurchaseTargetAffordable && !currentPurchaseTargetUnavailable && <small>这是本人工作作业的商品储蓄目标；后端会按当前报价核验，不会替你购买。</small>}
            </div>}
            {currentWorkResume && <div className="long-action-target-progress" aria-label="本人重复工作恢复记录">
              <div className="long-action-target-line"><span>暂停期间错过班次</span><strong>{currentWorkResume.skipped_shifts.toLocaleString('zh-HK')} 班</strong></div>
              <div className="long-action-target-line"><span>恢复后首班起点</span><strong>{currentWorkResume.next_shift_tick === null ? '暂无下一班' : formatWorldTick(currentWorkResume.next_shift_tick, dayLength, yearLength)}</strong></div>
            </div>}
            {currentJobStudySafetyThreshold !== null && <div className="long-action-target-progress" aria-label="本人学习安全暂停阈值">
              <div className="long-action-target-line"><span>安全暂停阈值</span><strong>{currentJobStudySafetyThreshold}</strong></div>
              <small>角色个人安全值达到或低于此值时暂停学习。</small>
            </div>}
            {currentItineraryProgress && <div className="long-action-target-progress" aria-label="行程路段抵达进度">
              <div className="long-action-target-line"><span>{currentItineraryProgress.legs_arrived === currentItineraryProgress.legs_total ? '全部路段已抵达' : '已抵达路段'}</span><strong>{currentItineraryProgress.legs_arrived} / {currentItineraryProgress.legs_total}</strong></div>
              {currentJob.status === 'succeeded' && currentItineraryProgress.legs_arrived < currentItineraryProgress.legs_total && <small>作业已结束，但仍有授权路段尚未抵达。</small>}
              {currentJob.status === 'succeeded' && currentItineraryProgress.legs_arrived === currentItineraryProgress.legs_total && <small>已按授权路线抵达终点。</small>}
              <small>只显示后端确认的行程路段进度。</small>
            </div>}

            {currentJobRequiresItineraryReplan && <div className="long-action-boundary" role="status">
              <Icon name="info" size={14} />
              <span>
                这段连续行程需要重新规划，原任务无法继续。
                {currentJobPauseGuidance && <> 暂停原因：{currentJobPauseGuidance}。</>}
                请从角色当前实际所在地规划并提交新行程。
                {currentItineraryProgress && currentItineraryProgress.legs_arrived > 0
                  && <> 已确认抵达的 {currentItineraryProgress.legs_arrived} 段后端进度回执会保留。</>}
              </span>
              <button type="button" className="button button-secondary button-small" onClick={openItineraryReplan}>前往新行程规划</button>
            </div>}

            {currentJob.status === 'paused' && !currentJobRequiresItineraryReplan && <div className="long-action-boundary" role="status">

              <Icon name="info" size={14} />

              <span>
                {currentJobPauseGuidance && currentItineraryProgress
                  ? `连续行程暂停。${currentJobPauseGuidance}`
                  : currentJob.reason_code

                    ? `模拟在规则边界暂停（${currentJobPauseGuidance ?? jobReasonLabel(currentJob.reason_code)}）；该状态不表示行动已完成。`

                    : '行动已暂停；该状态不表示行动已完成。'}

                {currentJobHasOwnWorkReadback && <span>继续后将执行剩余授权班次；工资只在实际班次正式结算后计入。</span>}

                {currentItineraryProgress && currentItineraryProgress.legs_arrived > 0 && <span>已确认抵达的 {currentItineraryProgress.legs_arrived} 段后端进度回执已保留。</span>}

                {!currentJobRequiresItineraryReplan && currentJobCanOpenItineraryReplan && <span>路线条件恢复、角色仍在原定下一段起点且未超过原时限时，可尝试继续；如改道，可提交替代行程并保留旧记录与已抵达回执。</span>}
              </span>

              {currentJobCanOpenItineraryReplan && <button type="button" className="button button-secondary button-small" onClick={openItineraryReplan}>前往新行程规划</button>}

            </div>}

            {currentJob.status === 'waiting_input' && <p className="long-action-boundary" role="status"><Icon name="info" size={14} />模拟需要输入后才能继续；此界面不会替你做出裁定。</p>}

            {currentJob.status === 'waiting_input' && <div className="long-action-decisions" aria-label="待处理的玩家选择">

              <strong>需要你的选择</strong>

              {decisionsLoading ? <p role="status" aria-live="polite"><Icon name="spinner" size={13} />正在读取后端提供的选项…</p> : decisionLoadError ? <div className="long-action-decision-error" role="alert"><span>{decisionLoadError}</span><button type="button" className="text-button" onClick={() => setDecisionAttempt((attempt) => attempt + 1)}>重试读取</button></div> : decisionItems.length ? decisionItems.map((decision) => <div className="long-action-decision" key={decision.request_id}>

                <h3>{decision.title}</h3>

                {decision.options.length ? <div className="long-action-decision-options">{decision.options.map((option) => {

                  const busyKey = `${decision.request_id}:${option.option_id}`

                  const unresolvedChoice = currentDecisionAction?.requestId === decision.request_id

                  return <button type="button" className="long-action-decision-option" key={option.option_id} onClick={() => void answerDecision(decision, option.option_id)} disabled={Boolean(decisionBusy) || Boolean(unresolvedChoice && currentDecisionAction?.optionId !== option.option_id)}>

                    <span><strong>{option.label}</strong><small>{option.effect_summary}</small></span>

                    {decisionBusy === busyKey ? <Icon name="spinner" size={14} /> : <Icon name="arrow-right" size={14} />}

                  </button>

                })}</div> : <p role="status">后端尚未返回可选项。</p>}

              </div>) : <p role="status">当前没有由玩家处理的待决选项。此任务仍处于等待状态。</p>}

              {currentDecisionActionError && <p className="long-action-decision-error" role="alert">提交选择失败：{currentDecisionActionError}。可重试相同选项，以保持请求一致。</p>}

              <small>仅显示后端返回、由玩家处理的待决选项。</small>

            </div>}

            {currentJob.reason_code && currentJob.status !== 'paused' && (currentJob.status !== 'succeeded' || WORK_REASON_CODES.has(currentJob.reason_code)) && <p className="long-action-reason">原因：{jobReasonLabel(currentJob.reason_code)}</p>}

          </> : <div className="long-action-no-job">

            <span className="long-action-status-dot" />

            <span>{jobsLoading ? '正在读取本分支的行动记录…' : jobsError ? '行动记录不可用' : '本分支尚无长时间行动'}</span>

            <strong style={{ overflowWrap: 'anywhere' }} title={`tick ${currentTick}`}>{jobsLoading ? '读取中' : jobsError ? '离线' : formatWorldTick(currentTick, dayLength, yearLength)}</strong>

          </div>}

          {statusError && <p className="long-action-error" role="alert">状态同步失败：{statusError}</p>}

        </div>

      </div>



      <div className="long-action-controls" id="long-action-controls" tabIndex={-1}>

        <div className="progress-mode-switch" role="group" aria-label="选择时间推进方式">

          <button type="button" className={progressMode === 'character' ? 'selected' : ''} aria-pressed={progressMode === 'character'} onClick={() => setProgressMode('character')} disabled={Boolean(submission) || submitting}>角色行动</button>

          <button type="button" className={progressMode === 'world' ? 'selected' : ''} aria-pressed={progressMode === 'world'} onClick={() => { setProgressMode('world'); setConfirmedWorldAdvanceKey('') }} disabled={Boolean(submission) || submitting}>全世界快进</button>

        </div>

        {progressMode === 'world' && <div className="world-advance-warning" role="note">

          <strong>全世界快进会结算所有角色</strong>

          <p>这段时间会同时经过所有人物，包括玩家角色；他们可能衰老、消耗资源或死亡。只有世界所有者可以提交，结果按后端已登记规则逐步结算。</p>

          <label><input type="checkbox" checked={worldAdvanceConfirmed} onChange={(event) => setConfirmedWorldAdvanceKey(event.target.checked ? worldAdvanceConfirmationKey : '')} disabled={Boolean(submission) || submitting} /><span>我已了解所有人物（包括玩家角色）都会经历这段时间及其后果。</span></label>

        </div>}

        {progressMode === 'character' && <label className="long-action-field">

          <span>活动</span>

          <select value={currentActivity} onChange={(event) => setActivitySelection({ actorKey, activity: event.target.value as Activity })} disabled={Boolean(submission) || submitting || Boolean(activeJourney)}>

            <option value="wait">等待</option>

            <option value="rest">休息</option>

            {availableStudySkills.length > 0 && <option value="study">学习</option>}

            {(activeJourney || localTravelOptions.length > 0 || hasItineraryStartOptions) && <option value="travel">旅行</option>}

            {availableWorkAssignments.length > 0 && <option value="work">按既有岗位排班工作</option>}

          </select>

        </label>}

        {progressMode === 'character' && currentActivity === 'study' && selectedStudySkill && <label className="long-action-field">

          <span>技能</span>

          <select
            value={selectedStudySkill.skill_id}
            onChange={(event) => setStudySkillSelection({ actorKey, skillId: event.target.value })}
            disabled={Boolean(submission) || submitting}
          >
            {availableStudySkills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>
              {studySkillOptionLabel(skill)}
            </option>)}
          </select>

        </label>}

        {progressMode === 'character' && currentActivity === 'study' && <label className="long-action-field">
          <span>安全暂停阈值（可选）</span>
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            inputMode="numeric"
            aria-label="学习安全暂停阈值"
            aria-describedby="study-safety-threshold-help"
            aria-invalid={!studySafetyThresholdValid}
            value={studySafetyThresholdText}
            onChange={(event) => setStudySafetyThresholdDraft({ actorKey, value: event.target.value })}
            disabled={Boolean(submission) || submitting}
            placeholder="留空则不设置"
          />
          <small id="study-safety-threshold-help">学习期间，角色本人的安全值达到或低于此阈值时暂停。范围为 0–100；留空不添加这个停止条件。</small>
          {!studySafetyThresholdValid && <small className="long-action-error" role="alert">请输入 0 到 100 之间的整数。</small>}
        </label>}

        {progressMode === 'character' && currentActivity === 'travel' && !activeJourney && (localTravelOptions.length > 0 || hasItineraryStartOptions) && <label className="long-action-field">
          <span>行程方式</span>
          <select
            value={selectedTravelMode}
            onChange={(event) => setTravelPlanSelection({ key: travelPlanKey, mode: event.target.value as TravelPlanMode })}
            disabled={Boolean(submission) || submitting}
          >
            <option value="single" disabled={localTravelOptions.length === 0}>单段路线</option>
            <option value="itinerary" disabled={!hasItineraryStartOptions}>连续行程（可见路段）</option>
          </select>
        </label>}

        {progressMode === 'character' && currentActivity === 'travel' && (activeJourney || selectedTravelMode === 'single') && (activeJourney || localTravelOptions.length > 0) && <label className="long-action-field">

          <span>路线</span>

          <select value={activeJourney?.route_id ?? selectedTravelRoute?.route_id ?? ''} onChange={(event) => setSelectedRouteId(event.target.value)} disabled={Boolean(submission) || submitting || Boolean(activeJourney)}>

            {activeJourney
              ? <option value={activeJourney.route_id}>继续当前旅程 · {activeJourney.to_place_id.slice(0, 8)}</option>
              : localTravelOptions.map((option) => <option key={option.route_id} value={option.route_id}>{option.destination_name} · {formatTickDurationDetail(parsePositiveTravelTicks(option.travel_ticks), dayLength, yearLength)}</option>)}

          </select>

        </label>}

        {progressMode === 'character' && currentActivity === 'travel' && selectedTravelMode === 'itinerary' && <>
          <label className="long-action-field">
            <span>追加下一段已知路线</span>
            <select
              value=""
              onChange={(event) => {
                const routeId = event.target.value
                if (!connectedItineraryOptions.some((option) => option.route_id === routeId)) return
                setTravelItinerarySelection({
                  key: travelPlanKey,
                  routeIds: [...selectedItineraryRouteIds, routeId],
                  autoCare: selectedItineraryAutoCare,
                })
              }}
              disabled={Boolean(submission) || submitting || connectedItineraryOptions.length === 0}
            >
              <option value="">{connectedItineraryOptions.length ? '选择从当前行程终点出发的路线' : '没有可见的连续下一段'}</option>
              {connectedItineraryOptions.map((option) => <option key={option.route_id} value={option.route_id}>
                {option.from_place_name} → {option.to_place_name} · {formatTickDurationDetail(parsePositiveTravelTicks(option.travel_ticks), dayLength, yearLength)}
              </option>)}
            </select>
            <small>{selectedItineraryRoutes.length === 0 ? '首段必须从玩家当前所在地点出发。' : `下一段必须从 ${selectedItineraryRoutes[selectedItineraryRoutes.length - 1].to_place_name} 出发。`}</small>
          </label>

          <div className="long-action-target-progress" aria-label="按顺序规划的已知行程">
            <div className="long-action-target-line"><span>行程预览</span><strong>{selectedItineraryRoutes.length} 段</strong></div>
            {selectedItineraryRoutes.map((route, index) => <div className="long-action-target-line" key={`${route.route_id}-${index}`}>
              <span>{index + 1}. {route.from_place_name} → {route.to_place_name}</span>
              <strong>{formatTickDurationDetail(parsePositiveTravelTicks(route.travel_ticks), dayLength, yearLength)}</strong>
            </div>)}
            {selectedItineraryRoutes.length === 0 && <small>尚未添加路线。只会列出当前投影提供且与行程连续的候选路线；不会补造路线。</small>}
            <div className="long-action-target-line"><span>计划纯路线移动时间</span><strong>{itineraryTotalTicks === null ? '待添加路线' : formatTickDurationDetail(itineraryTotalTicks, dayLength, yearLength)}</strong></div>
            <div className="long-action-target-line"><span>玩家授权时间窗口</span><strong style={{ overflowWrap: 'anywhere' }} title={hasSafeDuration ? `${durationSeconds.toLocaleString('zh-HK')} 秒` : undefined}>{hasSafeDuration ? formatSafeWorldDuration(durationSeconds, dayLength, yearLength) : '时长无效'}</strong></div>
            {itineraryTotalTicks !== null && <div className="long-action-target-line"><span>可用于休息、粮水与停留的余量</span><strong>{itinerarySlackTicks === null ? '待窗口有效后计算' : formatTickDurationDetail(itinerarySlackTicks, dayLength, yearLength)}</strong></div>}
            {itinerarySlackTicks !== null && <small>余量按授权窗口减纯路线移动时间计算；不估算自动自理或途中停留的实际耗时。</small>}
            <div className="long-action-actions">
              <button type="button" className="button button-secondary button-small" onClick={() => setTravelItinerarySelection({ key: travelPlanKey, routeIds: selectedItineraryRouteIds.slice(0, -1), autoCare: selectedItineraryAutoCare })} disabled={Boolean(submission) || submitting || selectedItineraryRouteIds.length === 0}>移除最后一段</button>
              <button type="button" className="button button-secondary button-small" onClick={() => setTravelItinerarySelection({ key: travelPlanKey, routeIds: [], autoCare: selectedItineraryAutoCare })} disabled={Boolean(submission) || submitting || selectedItineraryRouteIds.length === 0}>清空行程</button>
            </div>
          </div>

          <div className="world-advance-warning" role="note">
            <strong>自动休息与补给（可选）</strong>
            <label>
              <input
                type="checkbox"
                checked={selectedItineraryAutoCare}
                onChange={(event) => setTravelItinerarySelection({ key: travelPlanKey, routeIds: selectedItineraryRouteIds, autoCare: event.target.checked })}
                disabled={Boolean(submission) || submitting}
              />
              <span>允许旅途中自动休息，并消耗角色当前可用且拥有的食物和水。</span>
            </label>
            <p>此选项不允许购买补给、改道或作出其他承诺；这类情况仍需你输入。休息或补给会延迟实际抵达，下面的最低时长只加总路线旅行 tick。</p>
            {selectedItineraryAutoCare && validDuration && travelDurationEnough && itinerarySlackTicks === 0n && <small role="note">当前可用余量为 0。自动休息或补给需要路线移动之外的时间，建议增加授权窗口；只要窗口覆盖纯路线移动时间，余量为零本身不会阻止提交。</small>}
          </div>
        </>}

        {progressMode === 'character' && currentActivity === 'work' && selectedWorkAssignment && <>

          <label className="long-action-field">

            <span>任职岗位</span>

            <select
              value={selectedWorkAssignment.assignment_id}
              onChange={(event) => setWorkAssignmentSelection({ key: workDraftKey, assignmentId: event.target.value })}
              disabled={Boolean(submission) || submitting}
            >
              {availableWorkAssignments.map((assignment) => <option key={assignment.assignment_id} value={assignment.assignment_id}>
                {assignment.title} · {assignment.work_shift.wage} {assignment.work_shift.resource_id}
              </option>)}
            </select>

            <small>仅使用你当前持有、且后端投影提供已登记班次条件的任职。</small>

          </label>

          <div className="long-action-duration-summary" role="note" aria-label="已登记的班次报酬">
            <strong>已登记班次条件</strong>
              <span title={`每班 ${selectedWorkAssignment.work_shift.duration_seconds.toLocaleString()} 秒`}>每班 {formatWorldDuration(BigInt(selectedWorkAssignment.work_shift.duration_seconds), dayLength, yearLength)}</span>
            <span>报酬 {selectedWorkAssignment.work_shift.wage} {selectedWorkAssignment.work_shift.resource_id}</span>
            {!selectedWorkAssignment.work_shift.can_work_shift && <small>当前投影无法确认现在即可完成一班；岗位、地点和资源等条件仍由后端在结算时核验。</small>}
          </div>

          <label className="long-action-field">
            <span>首班开始（世界 tick）</span>
            <input
              type="text"
              inputMode="numeric"
              aria-label="首班开始世界 tick"
              value={workScheduleValues.firstShiftTick}
              onChange={(event) => setWorkScheduleDraft({ ...workScheduleValues, firstShiftTick: event.target.value })}
              disabled={Boolean(submission) || submitting}
              placeholder={defaultFirstShiftTick || '输入安全整数 tick'}
            />
            <small>{firstShiftTick === null
              ? `输入不早于当前世界 tick ${currentTick} 的安全整数。${hasWorldCalendar ? '下方会显示对应的世界历时间。' : ''}`
              : `${formatWorldRelativeTick(firstShiftTick, dayLength, yearLength)}；默认值为当前世界时刻。`}</small>
            {firstShiftTickNumber !== null && baseTick !== null && firstShiftTick !== null && firstShiftTick < baseTick && <small className="long-action-error" role="alert">首班不能早于当前世界时刻。</small>}
            {firstShiftTickNumber !== null && !workFirstShiftValid && firstShiftTick !== null && baseTick !== null && firstShiftTick >= baseTick && <small className="long-action-error" role="alert">首班及完整班次必须在本次工作期限结束前完成。</small>}
          </label>

          <label className="long-action-field">
            <span>重复间隔（世界日）</span>
            <input
              type="number"
              min={1}
              max={Number.MAX_SAFE_INTEGER}
              step={1}
              aria-label="重复间隔世界日"
              value={workScheduleValues.repeatEveryDays}
              placeholder="输入正整数天数"
              onChange={(event) => setWorkScheduleDraft({ ...workScheduleValues, repeatEveryDays: event.target.value })}
              disabled={Boolean(submission) || submitting}
            />
            <small>按世界日历间隔重复，不会把其他日期自动改成工作日。</small>
            {!Number.isSafeInteger(repeatEveryDays) || repeatEveryDays <= 0 ? <small className="long-action-error" role="alert">请输入正安全整数天数。</small> : null}
          </label>

          <label className="long-action-field">
            <span>余额目标（可选）</span>
            <input
              type="text"
              inputMode="decimal"
              aria-label={`余额目标，货币 ${selectedWorkAssignment.work_shift.resource_id}`}
              value={workScheduleValues.balanceTarget}
              onChange={(event) => setWorkScheduleDraft({ ...workScheduleValues, balanceTarget: event.target.value })}
              disabled={Boolean(submission) || submitting}
              placeholder={`以 ${selectedWorkAssignment.work_shift.resource_id} 计`}
            />
            <small>使用精确十进制金额和此班次报酬货币；留空则只按期限停止。目标只约束停止条件。</small>
            {!balanceTargetValid && <small className="long-action-error" role="alert">请输入不带指数的非负精确十进制金额，例如 125.50。</small>}
          </label>

          <label className="long-action-field">
            <span>商品储蓄目标（可选）</span>
            <select
              aria-label="商品储蓄目标挂牌"
              value={purchaseTargetListingId}
              onChange={(event) => setWorkScheduleDraft({ ...workScheduleValues, purchaseTargetListingId: event.target.value })}
              disabled={Boolean(submission) || submitting}
            >
              <option value="">不设置商品储蓄目标</option>
              {purchaseTargetListingId && !selectedPurchaseOffer && <option value={purchaseTargetListingId}>所选挂牌当前不可用</option>}
              {localPurchaseOffers.map((offer) => <option key={offer.listing_id} value={offer.listing_id}>
                {offer.resource_id} · 最低 {offer.minimum_quantity} · 挂牌 {offer.listing_id.slice(0, 8)}
              </option>)}
            </select>
            {localPurchaseOffers.length > 0
              ? <small>仅可选本地投影可见、并以该岗位登记工资币种 {wageCurrency} 挂牌的商品。服务端会在提交时重新核验挂牌与报价。</small>
              : <small>{!marketOffers?.length
                ? '当前投影没有可用的本地挂牌；仍可按期限或工资余额目标停止工作。'
                : !marketCurrency
                  ? '当前投影未提供可核对的挂牌币种；商品储蓄目标暂不可用。'
                  : `当前没有以该岗位工资币种 ${wageCurrency} 挂牌的可见商品；仍可按期限或工资余额目标停止工作。`}</small>}
          </label>

          <label className="long-action-field">
            <span>目标商品数量</span>
            <input
              type="text"
              inputMode="decimal"
              aria-label="目标商品精确数量"
              value={workScheduleValues.purchaseTargetQuantity}
              onChange={(event) => setWorkScheduleDraft({ ...workScheduleValues, purchaseTargetQuantity: event.target.value })}
              disabled={Boolean(submission) || submitting}
              placeholder={selectedPurchaseOffer ? `正数，最低 ${selectedPurchaseOffer.minimum_quantity}` : '选择挂牌后填写'}
            />
            {purchaseTargetError && <small className="long-action-error" role="alert">{purchaseTargetError}</small>}
          </label>

          {conflictingWorkSavingsGoals && <p className="long-action-error" role="alert">余额目标与商品储蓄目标只能选择一项，请清除其中一项。</p>}

          <div className="world-advance-warning" role="note">
            <strong>提交后授权范围</strong>
            <p>按「{selectedWorkAssignment.title}」已登记班次每 {workScheduleValues.repeatEveryDays || '未设置'} 个世界日重复，首班为 {firstShiftTick === null ? '未设置' : formatWorldRelativeTick(firstShiftTick, dayLength, yearLength)}{balanceTarget ? `；余额达到 ${balanceTarget} ${selectedWorkAssignment.work_shift.resource_id} 时停止` : ''}{selectedPurchaseOffer && purchaseTargetQuantity ? `；以挂牌 ${selectedPurchaseOffer.listing_id.slice(0, 8)} 的商品 ${selectedPurchaseOffer.resource_id} × ${purchaseTargetQuantity} 为储蓄目标` : ''}。工资只以正式结算计入余额。</p>
            <p>商品储蓄目标只由服务端按当前报价核验可负担时停止，不会替你购买；挂牌不可用时会暂停。未提交前不会安排工作或发起购买。</p>
          </div>

        </>}

        {activeJourney && progressMode === 'character' && <div className="long-action-boundary" role="status">

          <Icon name="info" size={14} />

          <span style={{ overflowWrap: 'anywhere' }}>旅程进行中：出发 {formatJourneyTick(activeJourney.departed_tick, dayLength, yearLength)}，预计抵达 {formatJourneyTick(activeJourney.arrival_tick, dayLength, yearLength)}。</span>

        </div>}

        <label className="long-action-field long-action-duration">

          <span>{progressMode === 'character' && currentActivity === 'travel' ? '行动时间窗口' : progressMode === 'character' && currentActivity === 'study' ? '学习时间窗口' : currentActivity === 'work' ? '工作计划期限（从现在起）' : '持续时间'}</span>

          <div className={`long-action-duration-inputs ${unit === 'year' ? 'long-action-year-inputs' : ''}`}>
            <input aria-label={unit === 'year' ? '日历年数量' : '持续时间数量'} type="number" min={unit === 'year' ? 0 : 1} max={amountMaximum} step={1} value={duration} onChange={(event) => setDuration(event.target.value)} disabled={Boolean(submission) || submitting} />
            {unit === 'year' && <input aria-label="附加日历日" type="number" min={0} max={Math.max(0, yearLength - 1)} step={1} value={additionalDays} onChange={(event) => setAdditionalDays(event.target.value)} disabled={Boolean(submission) || submitting || !hasWorldCalendar} />}
            <select aria-label="持续时间单位" value={unit} onChange={(event) => setUnit(event.target.value as Unit)} disabled={Boolean(submission) || submitting}>
              <option value="second">秒</option>
              <option value="hour">小时</option>
              <option value="day" disabled={!hasWorldDayLength}>日历日{!hasWorldDayLength ? '（世界时长未提供）' : ''}</option>
              <option value="year" disabled={!hasWorldCalendar}>日历年{!hasWorldCalendar ? '（世界日历未提供）' : ''}</option>
            </select>
          </div>
          {unit === 'year' && <small className="long-action-year-hint">按本世界每年 {yearLength || '—'} 日换算；最多 600 年。{progressMode === 'character' ? '这是该角色的行动时间窗口。' : ''}</small>}

        </label>

        <div className="long-action-duration-summary">
          <span>{progressMode === 'world' ? '全世界将推进' : progressMode === 'character' && currentActivity === 'travel' ? '旅程时间窗口' : currentActivity === 'work' ? '例行工作授权期限' : '将提交'}</span>
          <strong title={validDuration ? `${durationSeconds.toLocaleString()} 秒` : undefined}>{validDuration
            ? currentActivity === 'travel' && !activeJourney && selectedTravelMode === 'itinerary' && !validItineraryPlan
              ? '请添加一段或多段连续的可见路线'
              : currentActivity === 'travel' && !activeJourney && selectedTravelMode === 'itinerary' && !travelDurationEnough && itineraryTotalTicks !== null
                ? `行程至少需要 ${formatTickDurationDetail(itineraryTotalTicks, dayLength, yearLength)}`
                : currentActivity === 'travel' && !activeJourney && selectedTravelMode === 'single' && !selectedTravelRoute
                  ? '没有单段路线；请改选连续行程'
                  : currentActivity === 'travel' && !travelDurationEnough && travelTicksMinimum !== null
                    ? `路线至少需要 ${formatTickDurationDetail(travelTicksMinimum, dayLength, yearLength)}`
                    : formatWorldDuration(BigInt(durationSeconds), dayLength, yearLength)
            : durationError({ unit, amount, additionalDayAmount, yearLength, hasWorldCalendar, hasWorldDayLength, baseTick, targetTickPreview, tickInRange })}</strong>
          {progressMode === 'character' && currentActivity === 'work' && validDuration && targetTickPreview !== null && <small>最晚至 {formatWorldRelativeTick(targetTickPreview, dayLength, yearLength)}；只有正式结算的班次工资计入余额。</small>}
          {progressMode === 'character' && currentActivity === 'travel' && activeJourney && <small>预计抵达 {formatJourneyTick(activeJourney.arrival_tick, dayLength, yearLength)}；本次按所选时间窗口继续推进。</small>}
          {progressMode === 'character' && currentActivity === 'travel' && !activeJourney && travelTicksMinimum !== null && <small>路线需至少 {formatTickDurationDetail(travelTicksMinimum, dayLength, yearLength)}；当前窗口为 {formatSafeWorldDuration(durationSeconds, dayLength, yearLength)}（{durationSeconds.toLocaleString()} 秒）。</small>}
          {progressMode === 'character' && currentActivity === 'travel' && !activeJourney && selectedTravelMode === 'itinerary' && itineraryTotalTicks !== null && <small>计划纯路线移动时间 {formatTickDurationDetail(itineraryTotalTicks, dayLength, yearLength)}；玩家授权窗口 {formatSafeWorldDuration(durationSeconds, dayLength, yearLength)}（{durationSeconds.toLocaleString('zh-HK')} 秒）；路线外可用余量（休息、粮水与停留）{itinerarySlackTicks === null ? '待窗口有效后计算' : formatTickDurationDetail(itinerarySlackTicks, dayLength, yearLength)}。余量不代表预计的自理或停留耗时。</small>}
          {progressMode === 'world' && validDuration && targetTickPreview !== null && tickInRange && <small>预计目标 {formatWorldRelativeTick(targetTickPreview, dayLength, yearLength)}</small>}
        </div>

        <button className="button button-primary long-action-start" onClick={begin} disabled={!canStart}>

          {submitting ? <><Icon name="spinner" size={14} />正在提交</> : <><Icon name="arrow-right" size={14} />{progressMode === 'world' ? '开始全世界快进' : activeJourney ? '继续旅程' : currentActivity === 'travel' ? '开始旅程' : currentActivity === 'study' ? '开始学习' : currentActivity === 'work' ? '提交重复工作授权' : '开始行动'}</>}

        </button>

      </div>



      <div className="long-action-footer">

        <span><Icon name="shield" size={12} />速度与推演深度分别使用当前运行设置，不会因时长自动降低。</span>

        {profileLoading ? <span className="long-action-profile">读取速度 / 深度…</span> : profile ? <span className="long-action-profile">{profile.speed} · {profile.depth}</span> : <span className="long-action-profile error">{profileError ?? '运行设置不可用'}</span>}

      </div>



      <div className="long-action-history">

        <label htmlFor="long-action-job">本分支行动</label>

        {jobsLoading ? <span className="long-action-list-note">正在读取…</span> : jobsError ? <div className="long-action-list-error"><span>{jobsError}</span><button className="text-button" onClick={() => setListAttempt((attempt) => attempt + 1)}>重试</button></div> : jobs.length ? <select id="long-action-job" value={currentJob?.job_id ?? ''} onChange={(event) => setSelectedJob(event.target.value ? { key: scopeKey, id: event.target.value, manual: true } : null)} aria-label="选择本分支时间推进任务">

          {jobs.map((job) => <option key={job.job_id} value={job.job_id}>{job.job_type === 'simulation.world_advance' ? '全世界快进' : '角色行动'} · {longJobStatusLabel(job.status)} · {job.job_id.slice(0, 8)} · {formatUpdatedAt(job.updated_at)}</option>)}

        </select> : <span className="long-action-list-note">暂无记录</span>}

        <div className="long-action-actions" aria-label="行动控制">

          {controlActions.map((action) => <button key={action} className={`button ${action === 'cancel' ? 'button-danger' : 'button-secondary'} button-small`} onClick={() => void control(action)} disabled={Boolean(controlBusy)}>

            {controlBusy === action ? <Icon name="spinner" size={13} /> : <Icon name={action === 'pause' ? 'clock' : action === 'resume' ? 'arrow-right' : 'close'} size={13} />}

            {action === 'pause' ? '暂停' : action === 'resume' ? resumeLabel(currentJob?.reason_code) : '取消'}

          </button>)}

          <button className="icon-button long-action-refresh" title="重新读取行动状态" aria-label="重新读取行动状态" onClick={() => setListAttempt((attempt) => attempt + 1)} disabled={jobsLoading}><Icon name="refresh" size={14} /></button>

        </div>

      </div>



      {submissionError && <div className="long-action-submit-error" role="alert">

        <span>{submissionError}</span>

        {submission && <button className="button button-secondary button-small" onClick={retrySubmission} disabled={submitting}>使用相同请求安全重试</button>}

      </div>}

      {activeJob && !submission && !canSubmitReplacementItinerary && <p className="long-action-lock-note">
        {otherBlockingJobCount > 0 && structuralItineraryPauseCount > 0
          ? `本分支另有 ${otherBlockingJobCount} 项未结束行动阻止提交；请先完成、暂停、取消或回应其他任务。${structuralItineraryPauseCount} 项路线状态暂停行程会继续保留。`
          : canReplaceStructuralItineraryPause
            ? `选择一条完整且连续的替代路线后，可提交新行程并保留 ${structuralItineraryPauseCount} 项旧暂停记录及已抵达回执。`
            : '本分支已有未结束的长时间行动；请先处理上方任务，再提交新的计划。'}
      </p>}
      {canSubmitReplacementItinerary && <p className="long-action-lock-note">提交替代行程会建立独立任务；旧记录保持暂停。只有原路线条件恢复、角色回到原定下一段起点且未超过原时限时，旧任务才可继续；已抵达路段回执保留。</p>}

      {!profile && !profileLoading && <p className="long-action-lock-note">读取运行设置失败时不会提交行动。</p>}

    </section>

  )

}



function isOwnStudySkill(value: unknown): value is OwnStudySkill {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const skill = value as Partial<OwnStudySkill>
  return typeof skill.skill_id === 'string'
    && /^[a-zA-Z][a-zA-Z0-9_.-]*$/.test(skill.skill_id)
    && (typeof skill.level === 'number' ? Number.isFinite(skill.level)
      : typeof skill.level === 'string' && skill.level.trim() !== '' && Number.isFinite(Number(skill.level)))
}

function readWorkProgress(value: unknown): LongActionWorkProgress | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const progress = value as Partial<LongActionWorkProgress>
  const wages = progress.wages_by_resource
  if (!Number.isSafeInteger(progress.shifts_paid) || (progress.shifts_paid ?? -1) < 0
    || typeof progress.last_paid_tick !== 'string'
    || typeof progress.balance_resource_id !== 'string' || !progress.balance_resource_id.trim()
    || typeof progress.balance_after_last_pay !== 'string'
    || typeof wages !== 'object' || wages === null || Array.isArray(wages)) return null
  if (!Object.entries(wages).every(([resourceId, amount]) => resourceId.trim() !== '' && typeof amount === 'string')) return null
  return progress as LongActionWorkProgress
}

function readWorkPurchaseTarget(value: unknown): LongActionWorkPurchaseTarget | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const target = value as Partial<LongActionWorkPurchaseTarget>
  return typeof target.listing_id === 'string' && target.listing_id.trim() !== ''
    && typeof target.resource_id === 'string' && target.resource_id.trim() !== ''
    && typeof target.currency_resource_id === 'string' && target.currency_resource_id.trim() !== ''
    && typeof target.quantity === 'string' && isExactPositiveDecimal(target.quantity)
    ? target as LongActionWorkPurchaseTarget
    : null
}

function readWorkPurchaseAffordability(
  value: unknown,
  target: LongActionWorkPurchaseTarget | null,
): LongActionWorkPurchaseAffordability | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const proof = value as Partial<LongActionWorkPurchaseAffordability>
  if (typeof proof.listing_id !== 'string' || !proof.listing_id.trim()
    || typeof proof.quantity !== 'string'
    || typeof proof.currency_resource_id !== 'string' || !proof.currency_resource_id.trim()
    || typeof proof.total_price_minor !== 'string'
    || typeof proof.wage_balance !== 'string'
    || typeof proof.confirmed_tick !== 'string' || parseTick(proof.confirmed_tick) === null) return null
  const quantity = parseExactDecimal(proof.quantity)
  const totalPrice = parseExactDecimal(proof.total_price_minor)
  const wageBalance = parseExactDecimal(proof.wage_balance)
  const affordabilityComparison = compareExactDecimals(proof.wage_balance, proof.total_price_minor)
  if (quantity === null || quantity.coefficient <= 0n
    || totalPrice === null || totalPrice.coefficient < 0n || totalPrice.scale !== 0
    || wageBalance === null || affordabilityComparison === null || affordabilityComparison < 0) return null
  if (target !== null && (proof.listing_id !== target.listing_id
    || proof.currency_resource_id !== target.currency_resource_id
    || compareExactDecimals(proof.quantity, target.quantity) !== 0)) return null
  return proof as LongActionWorkPurchaseAffordability
}

function readWorkResume(value: unknown): LongActionWorkResume | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const resume = value as Partial<LongActionWorkResume>
  if (!Number.isSafeInteger(resume.skipped_shifts) || (resume.skipped_shifts ?? -1) < 0) return null
  if (resume.next_shift_tick !== null
    && (typeof resume.next_shift_tick !== 'string' || !/^-?\d+$/.test(resume.next_shift_tick))) return null
  return resume as LongActionWorkResume
}

function readItineraryProgress(value: unknown): LongActionItineraryProgress | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const progress = value as Partial<LongActionItineraryProgress>
  if (!Number.isSafeInteger(progress.legs_total) || (progress.legs_total ?? 0) < 1
    || !Number.isSafeInteger(progress.legs_arrived) || (progress.legs_arrived ?? -1) < 0
    || (progress.legs_arrived ?? 0) > (progress.legs_total ?? -1)) return null
  return progress as LongActionItineraryProgress
}

function isItineraryRouteOption(value: unknown): value is ItineraryRouteOption {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const route = value as Partial<ItineraryRouteOption>
  return typeof route.route_id === 'string' && route.route_id.trim() !== ''
    && typeof route.from_place_id === 'string' && route.from_place_id.trim() !== ''
    && typeof route.from_place_name === 'string' && route.from_place_name.trim() !== ''
    && typeof route.to_place_id === 'string' && route.to_place_id.trim() !== ''
    && typeof route.to_place_name === 'string' && route.to_place_name.trim() !== ''
    && typeof route.travel_ticks === 'string'
    && parsePositiveTravelTicks(route.travel_ticks) !== null
}

function isSchedulableWorkAssignment(value: unknown): value is OwnWorkAssignment & { work_shift: WorkShiftOffer } {
  if (!isOwnWorkAssignment(value) || (value.ended_tick !== undefined && value.ended_tick !== null)) return false
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.assignment_id)) return false
  return isWorkShiftOffer(value.work_shift)
    && isExactNonNegativeDecimal(value.work_shift.wage)
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

function isWorkShiftOffer(value: unknown): value is WorkShiftOffer {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const shift = value as Partial<WorkShiftOffer>
  return typeof shift.duration_seconds === 'number'
    && Number.isSafeInteger(shift.duration_seconds)
    && shift.duration_seconds > 0
    && typeof shift.resource_id === 'string'
    && shift.resource_id.trim().length > 0
    && typeof shift.wage === 'string'
    && typeof shift.can_work_shift === 'boolean'
}

function isExactNonNegativeDecimal(value: string): boolean {
  return /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value.trim())
}

function isExactPositiveDecimal(value: string): boolean {
  const decimal = parseExactDecimal(value)
  return decimal !== null && decimal.coefficient > 0n
}

function parseExactDecimal(value: string): { coefficient: bigint; scale: number } | null {
  const normalized = value.trim()
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(normalized)) return null
  const [whole, originalFraction = ''] = normalized.split('.')
  const fraction = originalFraction.replace(/0+$/, '')
  if (whole.length > 30 || fraction.length > 6) return null
  try {
    return { coefficient: BigInt(`${whole}${fraction}`), scale: fraction.length }
  } catch {
    return null
  }
}

function compareExactDecimals(left: string, right: string): number | null {
  const leftDecimal = parseExactDecimal(left)
  const rightDecimal = parseExactDecimal(right)
  if (leftDecimal === null || rightDecimal === null) return null
  const scale = Math.max(leftDecimal.scale, rightDecimal.scale)
  const leftValue = leftDecimal.coefficient * (10n ** BigInt(scale - leftDecimal.scale))
  const rightValue = rightDecimal.coefficient * (10n ** BigInt(scale - rightDecimal.scale))
  return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0
}

function isPurchaseTargetOffer(value: unknown): value is MarketOfferQuote {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const offer = value as Partial<MarketOfferQuote>
  return typeof offer.listing_id === 'string' && offer.listing_id.trim() !== ''
    && typeof offer.resource_id === 'string' && offer.resource_id.trim() !== ''
    && typeof offer.unit_price_minor === 'string'
    && typeof offer.minimum_quantity === 'string' && isExactPositiveDecimal(offer.minimum_quantity)
}

function safeIntegerText(value: string): number | null {
  const normalized = value.trim()
  if (!/^-?(?:0|[1-9]\d*)$/.test(normalized)) return null
  const parsed = Number(normalized)
  return Number.isSafeInteger(parsed) ? parsed : null
}

function formatWorldRelativeTick(tick: bigint | null, daySeconds: number, yearDays: number): string {
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
    return `世界历第 ${formatTick(yearIndex + 1n)} 年第 ${formatTick(dayOfYear + 1n)} 日 ${clock}（tick ${formatTick(tick)}）`
  }

  return `世界第 ${formatTick(dayIndex + 1n)} 日 ${clock}（tick ${formatTick(tick)}）`
}

function formatWorldDuration(value: bigint, daySeconds: number, yearDays: number): string {
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

function formatSafeWorldDuration(seconds: number, daySeconds: number, yearDays: number): string {
  return Number.isSafeInteger(seconds) && seconds >= 0
    ? formatWorldDuration(BigInt(seconds), daySeconds, yearDays)
    : '时间未知'
}

function formatTickDurationDetail(value: bigint | null, daySeconds: number, yearDays: number): string {
  if (value === null) return '未知'
  if (!isValidCalendarLength(daySeconds)) return `${formatTick(value)} tick`
  return `${formatWorldDuration(value, daySeconds, yearDays)}（${formatTick(value)} tick）`
}

function isValidCalendarLength(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0
}

function studySkillOptionLabel(skill: OwnStudySkill): string {
  const name = STUDY_SKILL_LABELS[skill.skill_id] ?? skill.skill_id
  const level = typeof skill.level === 'number' ? skill.level : Number(skill.level)
  const formattedLevel = Number.isFinite(level)
    ? new Intl.NumberFormat('zh-HK', { maximumFractionDigits: 6 }).format(level)
    : String(skill.level)
  return `${name} · 等级 ${formattedLevel}`
}

function parseTick(value: string | null | undefined): bigint | null {
  if (typeof value !== 'string' || !/^-?\d+$/.test(value)) return null
  try { return BigInt(value) } catch { return null }
}

function parseStateVersion(value: string | null | undefined): bigint | null {
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return null
  try { return BigInt(value) } catch { return null }
}

function jobRefreshCursor(
  job: JobStatusRecord,
  currentTick: string,
  stateVersion: string,
): { cursor: string; ahead: boolean } | null {
  const committedVersion = parseStateVersion(job.committed_state_version)
  const viewVersion = parseStateVersion(stateVersion)
  if (committedVersion !== null && viewVersion !== null) {
    return { cursor: `version:${committedVersion}`, ahead: committedVersion > viewVersion }
  }

  const committedTick = parseTick(job.committed_through_tick)
  const viewTick = parseTick(currentTick)
  if (committedTick !== null && viewTick !== null) {
    return { cursor: `tick:${committedTick}`, ahead: committedTick > viewTick }
  }

  if (typeof job.committed_through_tick === 'string') {
    return {
      cursor: `tick:${job.committed_through_tick}`,
      ahead: job.committed_through_tick !== currentTick,
    }
  }

  return null
}

function formatWorldTick(value: string | null | undefined, daySeconds: number, yearDays: number): string {
  const tick = parseTick(value)
  if (tick !== null) return formatWorldRelativeTick(tick, daySeconds, yearDays)
  return value ? `无法精确读取世界时间（tick ${value}）` : '无法精确读取世界时间'
}

function formatTick(value: bigint | null): string {
  return value === null ? '未知' : value.toLocaleString('zh-HK')
}

function formatExactInteger(value: string): string {
  const decimal = parseExactDecimal(value)
  return decimal !== null && decimal.scale === 0 ? formatTick(decimal.coefficient) : '无法精确读取'
}

function parsePositiveTravelTicks(value: string | number): bigint | null {
  if (typeof value === 'string' && /^[1-9]\d*$/.test(value)) {
    try { return BigInt(value) } catch { return null }
  }
  if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) return BigInt(value)
  return null
}

function formatJourneyTick(value: string | number, daySeconds: number, yearDays: number): string {
  let tick: bigint
  if (typeof value === 'string' && /^-?\d+$/.test(value)) {
    try { tick = BigInt(value) } catch { return '未知' }
  } else if (typeof value === 'number' && Number.isSafeInteger(value)) {
    tick = BigInt(value)
  } else {
    return '超出精确显示范围'
  }
  return formatWorldRelativeTick(tick, daySeconds, yearDays)
}

function jobTickProgress(job: JobStatusRecord): { started: bigint; target: bigint; committed: bigint; percent: number } | null {
  const started = parseTick(job.started_tick)
  const target = parseTick(job.target_tick)
  const committed = parseTick(job.committed_through_tick)
  if (started === null || target === null || committed === null || target <= started) return null
  const total = target - started
  const elapsed = committed <= started ? 0n : committed >= target ? total : committed - started
  return { started, target, committed, percent: Number((elapsed * 10_000n) / total) / 100 }
}

function parseStudySafetyThreshold(value: string): number | null {
  const text = value.trim()
  if (!/^\d+$/.test(text)) return null
  const parsed = Number(text)
  return Number.isSafeInteger(parsed) && parsed >= 0 && parsed <= 100 ? parsed : null
}

function isStudySafetyThreshold(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 100
}

function durationError(input: {
  unit: Unit
  amount: number
  additionalDayAmount: number
  yearLength: number
  hasWorldCalendar: boolean
  hasWorldDayLength: boolean
  baseTick: bigint | null
  targetTickPreview: bigint | null
  tickInRange: boolean
}): string {
  if (input.unit === 'year' && !input.hasWorldCalendar) return '当前世界未提供有效日历配置'
  if (input.unit === 'day' && !input.hasWorldDayLength) return '当前世界未提供日长'
  if (!Number.isSafeInteger(input.amount) || input.amount < (input.unit === 'year' ? 0 : 1)) return '请输入有效时长'
  if (input.unit === 'year' && input.amount > 600) return '日历年最多为 600 年'
  if (input.unit === 'year' && (!Number.isSafeInteger(input.additionalDayAmount) || input.additionalDayAmount < 0 || input.additionalDayAmount >= input.yearLength)) return '附加日历日必须小于一年'
  if (input.unit === 'year' && input.amount * input.yearLength + input.additionalDayAmount > 600 * input.yearLength) return '日历年最多为 600 年'
  if (input.baseTick === null) return '无法读取当前世界时钟'
  if (input.targetTickPreview !== null && !input.tickInRange) return '目标 tick 超出有符号 64 位范围'
  return '请输入有效时长'
}

function longJobStatusLabel(status: string): string {

  return ({ queued: '排队中', running: '模拟中', waiting_input: '等待输入', paused: '已暂停', succeeded: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] ?? '状态未知'

}



function statusTone(status: string): 'active' | 'attention' | 'done' | 'error' | 'quiet' {

  if (status === 'queued' || status === 'running') return 'active'

  if (status === 'paused' || status === 'waiting_input') return 'attention'

  if (status === 'succeeded') return 'done'

  if (status === 'failed') return 'error'

  return 'quiet'

}







function resumeLabel(reason: string | null | undefined): string {

  if (reason === 'background_deprivation_pending' || reason === 'background_deprivation_boundary') return '重试推进'

  return '继续'

}



function formatUpdatedAt(value: string): string {

  const parsed = new Date(value)

  return Number.isNaN(parsed.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-HK', { hour: '2-digit', minute: '2-digit' }).format(parsed)

}
