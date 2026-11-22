import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, RefObject } from 'react'
import { api, ApiError, type Branch, type CharacterEntity, type Conversation, type ConversationItem, type GameMessage, type MarketOfferQuote, type MessageRequest, type NarrativeRewrites, type NarrativeRewriteStyle, type OwnLeadershipBallot, type OwnMembership, type OwnPromise, type OwnSafeConductTreaty, type OwnShipment, type PlayerContinuationOption, type PlayerContinuationOptions, type SafeConductProposalOption, type Settings, type World, type WorldView } from '../api/client'
import { InlineState, PageEmpty } from '../components/States'
import { Icon } from '../components/Icon'
import { ChallengesPanel } from '../components/ChallengesPanel'
import { ContestsPanel } from '../components/ContestsPanel'
import { InvestigationJournal } from '../components/InvestigationJournal'
import { InstitutionalCreditPanel } from '../components/InstitutionalCreditPanel'
import { LeadershipBallotsPanel } from '../components/LeadershipBallotsPanel'
import { LongActionPanel } from '../components/LongActionPanel'
import { OccupationsPanel } from '../components/OccupationsPanel'
import { PlaySceneCard, type PlaySceneStage } from '../components/PlaySceneCard'
import { RelationshipInvitationsPanel } from '../components/RelationshipInvitationsPanel'
import { RelationshipPartnershipsPanel } from '../components/RelationshipPartnershipsPanel'
import { SafeConductPanel } from '../components/SafeConductPanel'
import { ShipmentReliefPanel } from '../components/ShipmentReliefPanel'
import type { Notice, Page } from '../App'
import { errorMessage, FOREGROUND_STATUS_POLL_INTERVAL_MS, jobReasonLabel } from '../utils'

const CombatTrucePanel = lazy(() => import('../components/CombatTrucePanel').then((module) => ({ default: module.CombatTrucePanel })))
const CultivationBreakthroughPanel = lazy(() => import('../components/CultivationBreakthroughPanel').then((module) => ({ default: module.CultivationBreakthroughPanel })))

type Mode = 'auto' | 'ask_gm' | 'roleplay' | 'action' | 'author'
const modeLabels: Record<Mode, string> = { auto: '自动', ask_gm: '问 GM', roleplay: '角色说话', action: '行动', author: '作者指令' }
const modeHints: Record<Mode, string> = { auto: '让 GM 判断是询问、对话还是行动。', ask_gm: '查询当前角色可知的信息，不推进世界时间。', roleplay: '以角色身份说话并进入世界交流。', action: '明确提交一项世界行动。', author: '作者权限下的设定编辑或干预。' }
const CONTINUATION_SEARCH_THRESHOLD = 20
const rewriteStyles = ['concise', 'literary'] as const
const rewriteStyleLabels: Record<NarrativeRewriteStyle, string> = { concise: '简洁', literary: '文学' }
const rewritableMessageKinds = new Set(['query', 'hypothetical', 'action', 'dialogue'])
type PlayChatJob = {
  commandId?: string
  jobId?: string
  status: string
  worldId: string
  branchId: string
  actorId: string
  refreshLongActionList: boolean
}
type OwnPrivateGiftView = {
  gift_id: string
  giver_id: string
  recipient_id: string
  place_id: string
  resource_id: string
  quantity: string
  offered_tick: string | number
  expires_at_tick: string | number
  revision: number
  status: 'pending' | 'accepted' | 'declined' | 'withdrawn'
  resolved_tick: string | number | null
  can_accept: boolean
  can_decline: boolean
  can_withdraw: boolean
  can_request_reply: boolean
}

export function PlayPage({ world, view, viewError, branches, branchId, loading, sceneStage, onSelectWorld, onOpenHistory, onBranchChange, onNotice, onRefresh }: { world: World | null; view: WorldView | null; viewError: string | null; branches: Branch[]; branchId: string | null; loading: boolean; sceneStage?: PlaySceneStage; onSelectWorld: () => void; onOpenHistory: () => void; onBranchChange: (id: string) => void; onNotice: (notice: Notice) => void; onRefresh: () => void }) {
  const [mode, setMode] = useState<Mode>('auto')
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<GameMessage[]>([])
  const [conversationLoad, setConversationLoad] = useState<{ key: string; conversation?: Conversation; error?: string } | null>(null)
  const [conversationAttempt, setConversationAttempt] = useState(0)
  const [sending, setSending] = useState(false)
  const [failedRequest, setFailedRequest] = useState<MessageRequest | null>(null)
  const [job, setJob] = useState<PlayChatJob | null>(null)
  const [profile, setProfile] = useState<Pick<Settings, 'speed' | 'depth'> | null>(null)
  const [profileLoading, setProfileLoading] = useState(true)
  const [profileError, setProfileError] = useState<string | null>(null)
  const [continuationLoad, setContinuationLoad] = useState<{ key: string; status: 'ready' | 'error' | 'hidden'; options?: PlayerContinuationOptions; error?: string } | null>(null)
  const [continuationAttempt, setContinuationAttempt] = useState(0)
  const [selectedContinuation, setSelectedContinuation] = useState<{ key: string; actorId: string } | null>(null)
  const [continuationSubmitting, setContinuationSubmitting] = useState(false)
  const [continuationSubmitState, setContinuationSubmitState] = useState<{ key: string; error: string; failedRequest: { key: string; body: { branch_id: string; previous_player_id: string; new_player_id: string; expected_state_version: string } } } | null>(null)
  const worldId = world?.world_id
  const conversationRequestKey = `${worldId ?? ''}:${branchId ?? ''}:${conversationAttempt}`
  const conversationState = conversationLoad?.key === conversationRequestKey ? conversationLoad : null
  const conversation = conversationState?.conversation ?? null
  const conversationSessionId = conversation?.session_id
  const conversationLoading = Boolean(worldId && branchId && !conversationState)
  const conversationError = conversationState?.error ?? null
  const state = view && (!('mode' in view) || view.mode === 'player') ? view.state : undefined
  const playerId = state?.player_id
  const sendScopeRevision = useRef(0)
  const composer = useRef<HTMLTextAreaElement>(null)
  const story = useRef<HTMLDivElement>(null)
  const followStory = useRef(true)
  const journeyTools = useRef<HTMLDetailsElement>(null)
  const journeyStatus = useRef<HTMLDivElement>(null)
  const traveling = state?.own_active_travel?.status === 'in_transit'
  const chatLongActionJob = job?.refreshLongActionList && job.jobId
    && job.worldId === worldId && job.branchId === branchId && job.actorId === playerId
    ? { jobId: job.jobId, worldId: job.worldId, branchId: job.branchId, actorId: job.actorId }
    : null
  const people = useMemo(() => Object.values(state?.entities ?? {}).filter((entity) => entity.kind === 'character' || entity.kind === 'person') as CharacterEntity[], [state?.entities])
  const shipments = state?.own_shipments ?? []
  const shipmentReliefOptions = state?.shipment_relief_options
  const localWorkPostings = state?.local_work_postings
  const ownWorkAssignments = state?.own_work_assignments
  const rawPromises = state?.own_promises
  const promiseDataAvailable = Array.isArray(rawPromises)
  const promises = promiseDataAvailable ? rawPromises.filter(isOwnPromise) : []
  const rawPrivateGifts = (state as unknown as { own_private_gifts?: unknown } | undefined)?.own_private_gifts
  const relationshipInvitations = state?.own_relationship_invitations
  const ownMeetingReflections = state?.own_meeting_reflections
  const relationshipPartnerships = state?.own_relationship_partnerships
  const partnershipProposalTargetIds = state?.partnership_proposal_target_ids
  const relationshipDisclosureOptions = state?.relationship_disclosure_options
  const ownRelationshipDisclosures = state?.own_relationship_disclosures
  const ownMemberships = state?.own_memberships
  const ownLeadershipBallots = state?.own_leadership_ballots
  const safeConductProposalOptions = state?.safe_conduct_proposal_options
  const ownSafeConductTreaties = state?.own_safe_conduct_treaties
  const combatTruceOptions = state?.combat_truce_options
  const ownCombatTruces = state?.own_combat_truces
  const cultivationBreakthroughOptions = state?.cultivation_breakthrough_options
  const rawMarketOffers = state?.market_offers
  const marketOffers = Array.isArray(rawMarketOffers?.offers) ? rawMarketOffers.offers.filter(isMarketOfferQuote) : []
  const marketCurrency = typeof rawMarketOffers?.currency_resource_id === 'string' ? rawMarketOffers.currency_resource_id : null
  const player = people.find((person) => person.id === playerId)
  const playerDead = player?.status === 'dead'
  const characterCanAct = player?.status === 'active'
  const continuationRequestKey = `${worldId ?? ''}:${branchId ?? ''}:${playerId ?? ''}:${continuationAttempt}`
  const continuationState = continuationLoad?.key === continuationRequestKey ? continuationLoad : null
  const continuationOptions = continuationState?.status === 'ready' ? continuationState.options ?? null : null
  const selectedContinuationId = selectedContinuation?.key === continuationRequestKey ? selectedContinuation.actorId : ''
  const continuationSubmitError = continuationSubmitState?.key === continuationRequestKey ? continuationSubmitState.error : null
  const failedContinuationRequest = continuationSubmitState?.key === continuationRequestKey ? continuationSubmitState.failedRequest : null
  const modeForActor = characterCanAct ? mode : 'ask_gm'
  const playerPlaceId = typeof player?.location_id === 'string' ? player.location_id : null
  const composerReady = Boolean(branchId && playerId && conversation?.actor_id === playerId && profile && !conversationLoading && !conversationError && !loading && !viewError && !sending && !failedRequest)
  const canPrepareQuery = composerReady && input.length === 0
  const canPrepareAction = canPrepareQuery && characterCanAct
  const canPrepareSceneDraft = modeForActor !== 'author'

  useLayoutEffect(() => {
    const transcript = story.current
    if (transcript && followStory.current) transcript.scrollTop = transcript.scrollHeight
  }, [messages.length, sending, conversationSessionId])

  useLayoutEffect(() => {
    return () => { sendScopeRevision.current += 1 }
  }, [worldId, branchId, playerId, conversationAttempt, conversationSessionId])

  useEffect(() => {
    let active = true
    // The selected world/branch key keeps old projection data out of the current view.
    if (!worldId || !branchId) return () => { active = false }
    void api.conversation(worldId, branchId)
      .then((result) => {
        if (!active) return
        setConversationLoad({ key: conversationRequestKey, conversation: result })
        setMessages(messagesFromConversation(result.items))
      })
      .catch((reason) => { if (active) setConversationLoad({ key: conversationRequestKey, error: errorMessage(reason) }) })
    return () => { active = false }
  }, [worldId, branchId, conversationRequestKey])

  useEffect(() => {
    if (!worldId || !branchId || !playerId || !playerDead) {
      return
    }
    let active = true
    const key = continuationRequestKey
    void api.playerContinuationOptions(worldId, branchId)
      .then((options) => {
        if (active) setContinuationLoad({ key, status: 'ready', options })
      })
      .catch((reason) => {
        if (!active) return
        if (reason instanceof ApiError && (reason.status === 403 || reason.status === 404)) {
          // The endpoint is owner-scoped. Hide continuation controls for other
          // accounts rather than exposing a prompt based on the dead actor.
          setContinuationLoad({ key, status: 'hidden' })
        } else {
          setContinuationLoad({ key, status: 'error', error: errorMessage(reason) })
        }
      })
    return () => { active = false }
  }, [worldId, branchId, playerId, playerDead, continuationRequestKey])

  useEffect(() => {
    let active = true
    void api.getSettings().then((settings) => { if (active) setProfile({ speed: settings.speed, depth: settings.depth }) }).catch((reason) => { if (active) setProfileError(errorMessage(reason)) }).finally(() => { if (active) setProfileLoading(false) })
    return () => { active = false }
  }, [world?.world_id])
  useEffect(() => {
    if (job?.jobId || !job?.commandId || !['queued', 'running', 'waiting_input'].includes(job.status)) return
    const commandId = job.commandId
    let active = true
    let inFlight = false
    const timer = window.setInterval(() => {
      if (inFlight) return
      inFlight = true
      void api.command(commandId).then((result) => {
        if (!active) return
        setJob((current) => current ? { ...current, status: result.status } : current)
        const commandResult = result.result
        if (result.status === 'succeeded' && commandResult) {
          const content = typeof commandResult === 'string' ? commandResult : commandResult.content
          if (content) setMessages((current) => [...current, {
            id: `cmd-${commandId}`, role: 'gm', content, source: 'api',
            kind: typeof commandResult === 'string' ? undefined : commandResult.kind,
            worldEffect: typeof commandResult === 'string' ? undefined : commandResult.world_effect,
            elapsedTicks: typeof commandResult === 'string' ? undefined : commandResult.elapsed_ticks,
            references: typeof commandResult === 'string' ? undefined : commandResult.references,
          }])
          if (result.committed_through_tick) { onNotice({ kind: 'success', text: `行动已完成 · 世界时间 ${result.committed_through_tick}` }); onRefresh() }
        }
        if (['failed', 'cancelled'].includes(result.status)) onNotice({ kind: 'error', text: result.error?.message ?? `任务${result.status === 'failed' ? '失败' : '已取消'}` })
      }).catch((reason) => { if (active) { setJob((current) => current ? { ...current, status: 'unavailable' } : current); onNotice({ kind: 'error', text: errorMessage(reason) }) } })
        .finally(() => { inFlight = false })
    }, FOREGROUND_STATUS_POLL_INTERVAL_MS)
    return () => { active = false; window.clearInterval(timer) }
  }, [job?.commandId, job?.jobId, job?.status, onNotice, onRefresh])

  useEffect(() => {
    if (!job?.jobId || job.worldId !== worldId || job.branchId !== branchId || job.actorId !== playerId
      || !['queued', 'running', 'waiting_input', 'paused'].includes(job.status)) return
    const jobId = job.jobId
    let active = true
    let pending = false
    const poll = async () => {
      if (pending) return
      pending = true
      try {
        const result = await api.jobStatus(jobId)
        if (!active || result.status === job.status) return
        setJob((current) => current?.jobId === jobId ? { ...current, status: result.status } : current)
        if (result.status === 'succeeded') {
          onNotice({ kind: 'success', text: '后台行动已结算' })
        } else if (['failed', 'cancelled', 'paused'].includes(result.status)) {
          onNotice({ kind: 'error', text: result.reason_code ? jobReasonLabel(result.reason_code) : jobStatusLabel(result.status) })
        }
        if (['paused', 'waiting_input', 'succeeded', 'failed', 'cancelled'].includes(result.status)) onRefresh()
      } catch (reason) {
        if (!active) return
        setJob((current) => current?.jobId === jobId ? { ...current, status: 'unavailable' } : current)
        onNotice({ kind: 'error', text: errorMessage(reason) })
      } finally {
        pending = false
      }
    }
    void poll()
    const timer = window.setInterval(() => { void poll() }, FOREGROUND_STATUS_POLL_INTERVAL_MS)
    return () => { active = false; window.clearInterval(timer) }
  }, [job, worldId, branchId, playerId, onNotice, onRefresh])

  const executeRequest = async (request: MessageRequest, retry = false) => {
    const sendScope = sendScopeRevision.current
    setSending(true)
    try {
      const result = await api.sendMessage(request.world_id, request)
      if (sendScope !== sendScopeRevision.current) return
      let rewriteSource = narrativeRewriteSource(request.request_id, result.kind)
      if (retry && result.content && rewriteSource) {
        // A receipt recovered on retry can succeed even if its conversation row never completed.
        const persisted = await api.conversation(request.world_id, request.branch_id).catch(() => null)
        if (sendScope !== sendScopeRevision.current) return
        const item = persisted?.session_id === request.session_id && persisted.actor_id === request.actor_id
          ? persisted.items.find((entry) => entry.request_id === request.request_id)
          : undefined
        rewriteSource = item?.status === 'succeeded' && item.result?.content === result.content
          ? narrativeRewriteSource(item.request_id, item.result.kind, item.result.rewrites)
          : undefined
      }
      setMessages((current) => current.map((message) => message.id === `local-${request.request_id}` ? { ...message, delivery: 'accepted' } : message))
      setMessages((current) => result.content && !current.some((message) => message.id === result.message_id) ? [...current, {
        id: result.message_id, role: 'gm', content: result.content, source: 'api', kind: result.kind,
        worldEffect: result.world_effect, elapsedTicks: result.elapsed_ticks, references: result.references, rewriteSource,
      }] : current)
      if (result.job_id || result.command_id) {
        setJob({
          commandId: result.command_id,
          jobId: result.job_id,
          status: 'queued',
          worldId: request.world_id,
          branchId: request.branch_id,
          actorId: request.actor_id,
          refreshLongActionList: Boolean(result.job_id && result.kind === 'action' && result.world_effect === false),
        })
        if (!result.content) setMessages((current) => current.some((message) => message.id === `pending-${result.message_id}`) ? current : [...current, { id: `pending-${result.message_id}`, role: 'system', content: '请求已提交，等待本地引擎处理。', source: 'api' }])
      }
      setFailedRequest(null)
      if (result.world_effect) onRefresh()
    } catch (reason) {
      const invalidDialogueReply = reason instanceof ApiError && reason.code === 'INVALID_DIALOGUE_REPLY'
      if (invalidDialogueReply) {
        setFailedRequest(null)
        if (sendScope !== sendScopeRevision.current) return
        setInput((current) => current || request.text)
      } else {
        setFailedRequest(!(reason instanceof ApiError) || reason.retryable ? request : null)
      }
      const content = invalidDialogueReply ? `${errorMessage(reason)} 模型调用可能产生费用。` : errorMessage(reason)
      setMessages((current) => [...current.filter((message) => message.id !== `error-${request.request_id}`), { id: `error-${request.request_id}`, role: 'error', content, source: 'api' }])
      setMessages((current) => current.map((message) => message.id === `local-${request.request_id}` ? { ...message, delivery: 'failed' } : message))
    } finally { setSending(false) }
  }
  const send = () => {
    if (!world || !branchId || !conversation || !profile || !input.trim() || !composerReady) return
    const text = input.trim()
    const request: MessageRequest = {
      request_id: crypto.randomUUID(), world_id: world.world_id, branch_id: branchId,
      session_id: conversation.session_id, actor_id: conversation.actor_id,
      expected_state_version: view?.world.state_version ?? world.state_version,
      mode: modeForActor, text, profile,
    }
    setMessages((current) => [...current, { id: `local-${request.request_id}`, role: 'player', content: text, source: 'local', delivery: 'pending' }])
    setInput('')
    void executeRequest(request)
  }
  const submitContinuation = async (request: NonNullable<typeof failedContinuationRequest> | null = null) => {
    if (!worldId || !playerId || !branchId || !continuationOptions || !playerDead || continuationSubmitting) return
    const selected = continuationOptions.items.find((item) => item.actor_id === selectedContinuationId)
    if (!selected) return
    const submission = request ?? {
      key: crypto.randomUUID(),
      body: {
        branch_id: branchId,
        previous_player_id: continuationOptions.previous_player_id,
        new_player_id: selected.actor_id,
        expected_state_version: continuationOptions.state_version,
      },
    }
    setContinuationSubmitting(true)
    setContinuationSubmitState({ key: continuationRequestKey, error: '', failedRequest: submission })
    try {
      const result = await api.continueAfterDeath(worldId, submission.body, submission.key)
      if (result.branch_id !== branchId || result.player_id !== selected.actor_id) {
        throw new Error('服务器返回的分支或角色与本次选择不一致。')
      }
      setContinuationLoad({ key: continuationRequestKey, status: 'hidden' })
      setContinuationSubmitState(null)
      setSelectedContinuation(null)
      setInput('')
      setMode('ask_gm')
      setConversationAttempt((attempt) => attempt + 1)
      onNotice({ kind: 'success', text: `已切换为「${selected.name}」继续当前分支。` })
      onRefresh()
    } catch (reason) {
      setContinuationSubmitState({
        key: continuationRequestKey,
        error: errorMessage(reason),
        failedRequest: submission,
      })
    } finally {
      setContinuationSubmitting(false)
    }
  }
  const retryFailedRequest = () => { if (failedRequest && !sending) void executeRequest(failedRequest, true) }
  const prepareDraft = (text: string, draftMode: 'ask_gm' | 'roleplay' | 'action') => {
    if (draftMode === 'ask_gm' ? !canPrepareQuery : !canPrepareAction) return
    setMode(draftMode)
    setInput(text)
    composer.current?.focus()
  }
  const prepareActionDraft = (text: string) => prepareDraft(text, 'action')
  const prepareGmQueryDraft = (text: string) => prepareDraft(text, 'ask_gm')
  const openJourneyStatus = () => {
    const tools = journeyTools.current
    const status = journeyStatus.current
    if (!tools || !status) return
    tools.open = true
    status.focus({ preventScroll: true })
    status.scrollIntoView({ block: 'start' })
  }
  const onInputKey = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() } }

  if (!world) return <PageEmpty eyebrow="进入世界" title="先选择一个世界。" body="从世界库打开现有存档，或创建一个新的世界。当前没有可展示的虚构进度。" action={<button className="button button-primary" onClick={onSelectWorld}>前往世界库 <Icon name="arrow-right" size={16} /></button>} />
  return <section className="play-page scene-play page-enter">
    <div className="play-topline">
      <h1>{world.title}</h1>
      <div className="play-header-actions">
        <select aria-label="当前分支" className="branch-select" value={branchId ?? ''} onChange={(event) => onBranchChange(event.target.value)} disabled={!branches.length}>
          {branches.length === 0 ? <option value="">尚无分支</option> : branches.map((branch) => <option key={branch.branch_id} value={branch.branch_id}>{branch.label || branch.branch_id.slice(0, 8)}</option>)}
        </select>
        <button type="button" className="button button-secondary button-small" onClick={onOpenHistory}><Icon name="timeline" size={15} />历史</button>
      </div>
    </div>
    <div className="play-statusbar">
      <span className="statusbar-dot" /><span>本地世界</span><span className="status-divider" />
      <span>{branchId ? '分支 ' + branchId.slice(0, 8) : '分支待创建'}</span>
      <span className="status-divider" /><span>{view?.branch.current_tick ?? world.current_tick} tick</span>
      {view?.world.world_mode === 'story' && <span className="status-version">有主线世界</span>}
      <span className="statusbar-spacer" /><span className="status-version">版本 {view?.world.state_version ?? world.state_version}</span>
    </div>
    <div className="scene-play-grid">
      <section className="conversation-panel scene-story" aria-labelledby="play-story-title">
        <div className="conversation-head">
          <div><span className="conversation-mark"><Icon name="spark" size={16} /></span><h2 id="play-story-title">故事与对话</h2></div>
          <div className="scene-story-actions">
            <button type="button" className="scene-journey-link" data-traveling={traveling} aria-controls="play-journey-tools" onClick={openJourneyStatus}>
              <Icon name="clock" size={15} />{traveling ? '行程进度' : '长行动'}
            </button>
            <button type="button" className="icon-button quiet-button" aria-label="复制对话" title="复制对话" onClick={() => { void navigator.clipboard?.writeText(messages.map((message) => message.role + ': ' + message.content).join('\n\n')); onNotice({ kind: 'info', text: '已复制当前对话。' }) }} disabled={!messages.length}><Icon name="copy" size={16} /></button>
          </div>
        </div>
        {Boolean(state?.story_milestones?.length) && <details className="scene-story-chronicle">
          <summary>已发生的剧情</summary>
          <ul>{state?.story_milestones?.map((milestone) => <li key={milestone.record_id}>
            <strong>{milestone.title}</strong><p>{milestone.summary}</p>
          </li>)}</ul>
        </details>}
        {worldId && branchId && playerId && view && Boolean(state?.local_challenges?.length) &&
          <ChallengesPanel key={`challenges:${branchId}:${playerId}`} worldId={worldId} branchId={branchId} actorId={playerId}
            version={view.world.state_version} options={state?.local_challenges ?? []} onRefresh={onRefresh} />}
        {worldId && branchId && playerId && view && Boolean(state?.local_contests?.length) &&
          <ContestsPanel key={`contests:${branchId}:${playerId}`} worldId={worldId} branchId={branchId} actorId={playerId}
            version={view.world.state_version} options={state?.local_contests ?? []} onRefresh={onRefresh} />}
        {playerDead && continuationState && (continuationState.status === 'ready' || continuationState.status === 'error') && (
          <div className="scene-continuation">
            <PlayerContinuationPanel
              currentPlayerId={playerId}
              loadError={continuationState.status === 'error' ? continuationState.error ?? '无法读取继续游玩的选项。' : null}
              options={continuationOptions}
              selectedId={selectedContinuationId}
              submitting={continuationSubmitting}
              submitError={continuationSubmitError}
              retrying={Boolean(failedContinuationRequest)}
              onSelect={(actorId) => setSelectedContinuation({ key: continuationRequestKey, actorId })}
              onContinue={() => { void submitContinuation() }}
              onRetryContinue={() => { void submitContinuation(failedContinuationRequest) }}
              onRetryOptions={() => setContinuationAttempt((attempt) => attempt + 1)}
            />
          </div>
        )}
        <div
          className="conversation-body"
          aria-label="故事记录"
          aria-live="polite"
          tabIndex={0}
          ref={story}
          onScroll={(event) => {
            const transcript = event.currentTarget
            followStory.current = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 48
          }}
        >
          {loading && <div className="conversation-blank"><span className="blank-symbol"><Icon name="spinner" size={22} /></span><strong>正在读取世界状态</strong><span>正在载入当前分支及角色的可见信息。</span></div>}
          {!loading && viewError && <InlineState icon="cloud-off" title="无法读取此分支" description={viewError} action={<button className="button button-secondary button-small" onClick={onRefresh}>重新连接</button>} />}
          {!loading && conversationLoading && <div className="conversation-blank"><span className="blank-symbol"><Icon name="spinner" size={22} /></span><strong>正在读取持久对话</strong><span>正在载入当前分支的会话与历史消息。</span></div>}
          {!loading && !conversationLoading && conversationError && <InlineState icon="cloud-off" title="无法读取对话记录" description={conversationError} action={<button className="button button-secondary button-small" onClick={() => setConversationAttempt((attempt) => attempt + 1)}>重新读取</button>} />}
          {!loading && !viewError && !conversationLoading && !conversationError && messages.length === 0 && <div className="conversation-blank"><span className="blank-symbol"><Icon name="message" size={22} /></span><strong>{playerDead ? '旧旅程留在这里。' : '这一页，留给即将发生的事。'}</strong><span>{playerDead ? '你仍可向 GM 查询这个分支的历史；行动和角色对话已停用。' : '输入一句话开始。尚未发送的内容不会推进世界时间。'}</span><div className="starter-prompts"><button type="button" disabled={!canPrepareQuery || !canPrepareSceneDraft} onClick={() => prepareGmQueryDraft('我现在身处哪里？')}>我现在身处哪里？</button>{characterCanAct && <button type="button" disabled={!canPrepareAction || !canPrepareSceneDraft} onClick={() => prepareActionDraft('看看周围有什么线索。')}>看看周围有什么线索</button>}</div></div>}
          {messages.map((message) => (
            <article key={message.id} className={`message-row message-${message.role}`}>
              <div className="message-avatar">
                {message.role === 'player' ? <Icon name="user" size={15} /> : message.role === 'gm' ? <Icon name="spark" size={15} /> : <Icon name="alert" size={15} />}
              </div>
              <div className="message-content">
                <div className="message-meta">
                  <strong>{message.role === 'player' ? '你' : message.role === 'gm' ? 'GM' : '系统提示'}</strong>
                  {message.kind && <span>{messageKindLabel(message.kind)}</span>}
                  {message.worldEffect && <small className="message-world-effect">已写入世界</small>}
                  {message.elapsedTicks === '0' && <small className="message-no-time">时间未推进</small>}
                  {message.delivery === 'pending' && <small>等待确认</small>}
                  {message.delivery === 'accepted' && <small>已提交</small>}
                  {message.delivery === 'failed' && <small>未送达</small>}
                </div>
                {message.rewriteSource && worldId && conversation && conversation.actor_id === playerId ? (
                  <NarrativeRewriteText
                    key={`${conversationRequestKey}:${conversation.session_id}:${playerId}:${message.rewriteSource.requestId}`}
                    worldId={worldId}
                    source={message.rewriteSource}
                    original={message.content}
                    scopeRevision={sendScopeRevision}
                  />
                ) : <p>{message.content}</p>}
                <MessageReferences references={message.references} entities={state?.entities ?? {}} promises={promises} marketOffers={marketOffers} />
                {message.role === 'error' && failedRequest?.request_id && message.id === `error-${failedRequest.request_id}` && (
                  <button className="retry-send" onClick={retryFailedRequest} disabled={sending}><Icon name="refresh" size={13} />使用相同请求重试</button>
                )}
              </div>
            </article>
          ))}
          {sending && <div className="typing-indicator"><span /><span /><span /><em>正在发送到本地引擎</em></div>}
          {job && <div className="job-strip"><span className="job-pulse" /><span>后台任务 {job.jobId?.slice(0, 8) ?? job.commandId?.slice(0, 8) ?? ''}</span><strong>{jobStatusLabel(job.status)}</strong></div>}
        </div>
        <div className="composer-wrap">
          <div className="mode-switcher" role="group" aria-label="输入模式">
            {(['auto', 'ask_gm', 'roleplay', 'action', 'author'] as Mode[]).map((value) => (
              <button type="button" key={value} className={'mode-option' + (modeForActor === value ? ' selected' : '')} onClick={() => setMode(value)} title={!characterCanAct && value !== 'ask_gm' ? '当前角色只能向 GM 查询。' : modeHints[value]} aria-pressed={modeForActor === value} disabled={!characterCanAct && value !== 'ask_gm'}>{modeLabels[value]}</button>
            ))}
          </div>
          <div className="composer">
            <textarea
              ref={composer}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={onInputKey}
              aria-label="发送给 GM"
              placeholder={modeForActor === 'ask_gm' ? '想向 GM 了解什么？' : modeForActor === 'author' ? '输入作者指令…' : '描述你想说的话或采取的行动…'}
              rows={2}
              disabled={!composerReady}
            />
            <div className="composer-footer">
              <span><kbd>Enter</kbd> 发送 <span className="kbd-divider">·</span> <kbd>Shift + Enter</kbd> 换行</span>
              <button type="button" className="send-button" onClick={send} disabled={!input.trim() || !composerReady} aria-label="发送消息">
                {sending ? <Icon name="spinner" size={17} /> : <Icon name="arrow-up" size={17} />}
              </button>
            </div>
          </div>
          <div className="composer-note"><Icon name="shield" size={12} /><span>{failedRequest ? '发送状态未确认；请重试同一请求，以避免重复执行。' : conversationLoading ? '正在从后端恢复会话。' : conversationError ? '对话状态不可用；重新读取前不会发送请求。' : playerDead ? '旧角色已去世：你仍可使用 GM 查询，行动与角色对话不可用。' : '查询与假设不会推进世界时间；行动结果以提交后的状态为准。'}</span><span className="profile-chip">{profileLoading ? '读取配置…' : profile ? `${profile.speed} · ${profile.depth}` : `发送已停用 · ${profileError ?? '无法读取运行配置'}`}</span></div>
        </div>
      </section>
      <PlaySceneCard
        state={state}
        loading={loading}
        viewError={Boolean(viewError)}
        stage={sceneStage}
        canPrepareAction={canPrepareSceneDraft && canPrepareAction}
        canPrepareQuery={canPrepareSceneDraft && canPrepareQuery}
        onPrepareDraft={({ mode: draftMode, text }) => { if (canPrepareSceneDraft) prepareDraft(text, draftMode) }}
      />
    </div>
    <details id="play-journey-tools" className="scene-tools" ref={journeyTools}>
      <summary><Icon name="layers" size={18} /><strong>旅程工具</strong><small>长行动、调查、物资与关系</small><Icon name="chevron" size={15} /></summary>
      <div className="scene-tools-content">
        <section className="scene-tool-group" aria-labelledby="scene-tools-journey">
          <h3 id="scene-tools-journey">行动与调查</h3>
          {worldId && <div className="scene-journey-status" role="group" aria-labelledby="long-action-title" tabIndex={-1} ref={journeyStatus}>
            <LongActionPanel worldId={worldId} branchId={branchId} currentTick={view?.branch.current_tick ?? world.current_tick} stateVersion={view?.branch.state_version ?? world.state_version} daySeconds={view?.state.calendar?.day_seconds} yearDays={view?.state.calendar?.year_days} travelOptions={state?.travel_options} itineraryRouteOptions={state?.itinerary_route_options} currentPlaceId={playerPlaceId} ownActiveTravel={state?.own_active_travel} ownStudySkills={state?.own_study_skills} ownWorkAssignments={state?.own_work_assignments} marketOffers={marketOffers} marketCurrency={marketCurrency} actorId={playerId} chatLongActionJob={chatLongActionJob} profile={profile} profileLoading={profileLoading} profileError={profileError} disabled={loading || Boolean(viewError) || !view || !playerId || !characterCanAct} onRefresh={onRefresh} onNotice={onNotice} />
          </div>}
          <InvestigationJournal
            key={`${worldId ?? ''}:${branchId ?? ''}:${playerId ?? ''}`}
            anchors={state?.available_case_anchors ?? []}
            caseFiles={state?.case_files ?? []}
            repairOffers={state?.own_accusation_repair_offers}
            playerId={playerId}
            knowledge={state?.knowledge ?? []}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={Boolean(viewError)}
            inputHasText={Boolean(input.trim())}
            busy={sending || Boolean(failedRequest) || !characterCanAct}
            authorMode={modeForActor === 'author'}
            onPrepareAction={prepareActionDraft}
          />
          {cultivationBreakthroughOptions && cultivationBreakthroughOptions.length > 0 && <Suspense fallback={null}>
            <CultivationBreakthroughPanel
              options={cultivationBreakthroughOptions}
              canPrepareAction={canPrepareAction}
              onPrepareAction={prepareActionDraft}
            />
          </Suspense>}
        </section>
        <section className="scene-tool-group" aria-labelledby="scene-tools-economy">
          <h3 id="scene-tools-economy">生计与物资</h3>
          <OccupationsPanel
            postings={localWorkPostings}
            assignments={ownWorkAssignments}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          <ShipmentsPanel
            shipments={shipments}
            entities={state?.entities ?? {}}
            playerName={player?.name ?? '你'}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onReceive={(shipmentId) => prepareActionDraft(`请接收运单 ${shipmentId}`)}
          />
          <ShipmentReliefPanel
            options={shipmentReliefOptions}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          {marketOffers.length > 0 && <MarketQuotesPanel
            offers={marketOffers}
            currencyResourceId={marketCurrency}
            canPrepareQuery={canPrepareQuery}
            onAsk={(text) => prepareGmQueryDraft(text)}
          />}
          <details className="shipment-panel">
            <summary className="shipment-panel-summary"><span><Icon name="box" size={14} />我的资源</span><small>{Object.keys(state?.resources ?? {}).length} 项</small></summary>
            {state && Object.keys(state.resources ?? {}).length ? (
              <div className="shipment-list resource-list">{Object.entries(state.resources).map(([accountId, resource]) => <div className="resource-row" key={accountId}><span>{resource.resource_id}</span><strong>{resource.balance}</strong></div>)}</div>
            ) : <p className="shipment-empty">尚无可显示的资源账目。</p>}
          </details>
        </section>
        <section className="scene-tool-group" aria-labelledby="scene-tools-relations">
          <h3 id="scene-tools-relations">关系与治理</h3>
          <RelationshipInvitationsPanel
            invitations={relationshipInvitations}
            reflections={ownMeetingReflections}
            entities={state?.entities ?? {}}
            playerId={playerId}
            currentTick={view?.branch.current_tick ?? world.current_tick}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          <PrivateGiftsPanel
            gifts={rawPrivateGifts}
            entities={state?.entities ?? {}}
            playerId={playerId}
            currentTick={view?.branch.current_tick ?? world.current_tick}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          <RelationshipPartnershipsPanel
            partnerships={relationshipPartnerships}
            proposalTargetIds={partnershipProposalTargetIds}
            disclosureOptions={relationshipDisclosureOptions}
            receivedDisclosures={ownRelationshipDisclosures}
            entities={state?.entities ?? {}}
            playerId={playerId}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          <PromisesPanel
            promises={promises}
            entities={state?.entities ?? {}}
            promiseDataAvailable={promiseDataAvailable}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          <LeadershipBallotsPanel
            memberships={ownMemberships as OwnMembership[] | null | undefined}
            ballots={ownLeadershipBallots as OwnLeadershipBallot[] | null | undefined}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          <SafeConductPanel
            proposalOptions={safeConductProposalOptions as SafeConductProposalOption[] | null | undefined}
            treaties={ownSafeConductTreaties as OwnSafeConductTreaty[] | null | undefined}
            entities={state?.entities ?? {}}
            currentTick={view?.branch.current_tick ?? world.current_tick}
            projectionAvailable={Boolean(state && playerId)}
            loading={loading}
            viewError={viewError}
            canPrepareAction={canPrepareAction}
            onPrepareAction={prepareActionDraft}
          />
          {(combatTruceOptions || (ownCombatTruces?.length ?? 0) > 0) && <Suspense fallback={null}>
            <CombatTrucePanel
              options={combatTruceOptions}
              truces={ownCombatTruces}
              entities={state?.entities ?? {}}
              playerId={playerId}
              projectionAvailable={Boolean(state && playerId)}
              loading={loading}
              viewError={viewError}
              canPrepareAction={canPrepareAction}
              onPrepareAction={prepareActionDraft}
            />
          </Suspense>}
          <InstitutionalCreditPanel
            key={`${worldId ?? ''}:${branchId ?? ''}:${playerId ?? ''}`}
            credit={state?.institutional_credit}
            finance={state?.institutional_finance}
            projectionLoading={loading}
            canPrepareAction={canPrepareAction}
            canPrepareQuery={canPrepareQuery}
            onPrepareAction={prepareActionDraft}
            onPrepareQuery={prepareGmQueryDraft}
          />
        </section>
        <div className="scene-tool-links">
          <button type="button" className="button button-secondary button-small" onClick={() => window.dispatchEvent(new CustomEvent<Page>('world-engine:navigate', { detail: 'world' }))}><Icon name="book" size={14} />设定档案</button>
          <button type="button" className="button button-secondary button-small" onClick={() => window.dispatchEvent(new CustomEvent<Page>('world-engine:navigate', { detail: 'operations' }))}><Icon name="pulse" size={14} />模型与运行</button>
        </div>
      </div>
    </details>
  </section>
}

function PlayerContinuationPanel({ currentPlayerId, loadError, options, selectedId, submitting, submitError, retrying, onSelect, onContinue, onRetryContinue, onRetryOptions }: {
  currentPlayerId?: string
  loadError: string | null
  options: PlayerContinuationOptions | null
  selectedId: string
  submitting: boolean
  submitError: string | null
  retrying: boolean
  onSelect: (actorId: string) => void
  onContinue: () => void
  onRetryContinue: () => void
  onRetryOptions: () => void
}) {
  const [searchText, setSearchText] = useState('')
  const mismatchedOwner = Boolean(options && options.previous_player_id !== currentPlayerId)
  const candidates = mismatchedOwner ? [] : options?.items ?? []
  const selected = candidates.find((item) => item.actor_id === selectedId)
  const searchNeedle = searchText.trim().toLocaleLowerCase()
  const filteredCandidates = candidates.filter((candidate) =>
    !searchNeedle || `${candidate.name} ${candidate.role}`.toLocaleLowerCase().includes(searchNeedle),
  )
  const selectedIsFilteredOut = Boolean(selected && !filteredCandidates.some((candidate) => candidate.actor_id === selected.actor_id))
  return <section className="shipment-panel" aria-labelledby="continuation-title">
    <div className="shipment-panel-summary">
      <span id="continuation-title"><Icon name="user" size={14} />继续当前分支</span>
      <small>旧角色已去世</small>
    </div>
    <div className="shipment-list" aria-live="polite">
      {loadError && <InlineState
        icon="cloud-off"
        title="无法读取继续游玩的选项"
        description={loadError}
        action={<button className="button button-secondary button-small" type="button" onClick={onRetryOptions}>重新读取</button>}
      />}
      {!loadError && mismatchedOwner && <InlineState
        icon="cloud-off"
        title="分支状态已变化"
        description="当前继续选项对应另一位旧角色，请重新读取分支状态。"
        action={<button className="button button-secondary button-small" type="button" onClick={onRetryOptions}>重新读取</button>}
      />}
      {!loadError && !mismatchedOwner && options && candidates.length === 0 && <p className="shipment-empty" role="status">
        目前没有可接手的在世人物。旧角色的档案、物品和关系仍留在这个分支。
      </p>}
      {!loadError && !mismatchedOwner && candidates.length > 0 && <article className="shipment-card meeting-reflection-draft">
        <p className="promise-text">选择一名在世人物继续游玩。这里只会切换当前扮演角色；旧角色的事实、物品和关系保持原样。</p>
        {candidates.length > CONTINUATION_SEARCH_THRESHOLD && <div className="continuation-search" role="search" aria-label="搜索可接手人物">
          <label htmlFor="continuation-actor-search">
            <span>按姓名或角色筛选 · {filteredCandidates.length}/{candidates.length} 人</span>
            <input
              id="continuation-actor-search"
              type="search"
              aria-label="按姓名或角色搜索继续游玩人物"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="输入姓名或角色"
              disabled={submitting || retrying}
            />
          </label>
          {searchText && <button
            className="text-button"
            type="button"
            onClick={() => setSearchText('')}
            disabled={submitting || retrying}
            aria-label="清除人物搜索"
          >清除</button>}
        </div>}
        {candidates.length > CONTINUATION_SEARCH_THRESHOLD && searchNeedle && filteredCandidates.length === 0 && <p className="continuation-search-empty" role="status" aria-live="polite">
          没有符合“{searchText.trim()}”的在世人物。清除搜索后可查看全部人物。
        </p>}
        <label>
          <span>继续游玩的角色</span>
          <select
            className="branch-select"
            aria-label="选择继续游玩的角色"
            value={selectedId}
            onChange={(event) => onSelect(event.target.value)}
            disabled={submitting || retrying}
          >
            <option value="">选择一名在世人物</option>
            {selectedIsFilteredOut && selected && <option value={selected.actor_id}>
              当前选择 · {selected.name || '未命名人物'}{selected.role ? ` · ${selected.role}` : ''}
            </option>}
            {filteredCandidates.map((candidate: PlayerContinuationOption) => <option key={candidate.actor_id} value={candidate.actor_id}>
              {candidate.name || '未命名人物'}{candidate.role ? ` · ${candidate.role}` : ''}
            </option>)}
          </select>
        </label>
        {submitError && <p className="shipment-empty" role="alert">{submitError}</p>}
        <button
          className="button button-primary button-small"
          type="button"
          onClick={retrying ? onRetryContinue : onContinue}
          disabled={!selected || submitting}
          aria-label={retrying ? '使用相同请求重试角色切换' : `以${selected?.name ?? '所选角色'}继续当前分支`}
        >{submitting ? <><Icon name="spinner" size={14} />正在切换角色…</> : retrying ? '使用相同请求重试' : selected ? `以${selected.name || '所选角色'}继续` : '先选择一名角色'}</button>
      </article>}
    </div>
  </section>
}

function PromisesPanel({ promises, entities, promiseDataAvailable, projectionAvailable, loading, viewError, canPrepareAction, onPrepareAction }: {
  promises: OwnPromise[]
  entities: Record<string, CharacterEntity>
  promiseDataAvailable: boolean
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const activeCount = promises.filter((promise) => promise.status === 'active').length
  return <details className="shipment-panel promise-panel" aria-label="本人参与的承诺">
    <summary className="shipment-panel-summary">
      <span><Icon name="message" size={14} />我的承诺</span>
      <small>{loading ? '读取中' : !projectionAvailable ? '角色投影未载入' : !promiseDataAvailable ? '旧版投影未提供' : promises.length ? `${promises.length} 项 · ${activeCount} 项待处理` : '目前没有关联承诺'}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取承诺记录…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的承诺记录。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !promiseDataAvailable && <p className="shipment-empty">当前版本的世界投影未提供承诺记录。</p>}
    {!loading && !viewError && projectionAvailable && promiseDataAvailable && promises.length === 0 && <p className="shipment-empty">目前没有与你相关的承诺记录。</p>}
    {!loading && !viewError && projectionAvailable && promises.length > 0 && <div className="shipment-list" aria-live="polite">
      {promises.map((promise, index) => {
        const promiseId = typeof promise.promise_id === 'string' && promise.promise_id.trim() ? promise.promise_id : null
        const role = promise.role === 'promisor' || promise.role === 'promisee' ? promise.role : null
        const active = promise.status === 'active'
        const counterpart = promise.counterparty_id
          ? entities[promise.counterparty_id]?.name ?? '对方资料未载入'
          : '对方资料未提供'
        const text = typeof promise.text === 'string' && promise.text.trim() ? promise.text : '承诺内容暂不可用。'
        const resourceId = typeof promise.terms?.resource_id === 'string' ? promise.terms.resource_id : null
        const quantity = promise.terms?.quantity
        const hasTerms = promise.terms?.kind === 'return_item' && resourceId !== null && quantity !== undefined && quantity !== null
        const deadline = promise.deadline_tick === undefined || promise.deadline_tick === null
          ? '期限未提供'
          : `截止 tick ${String(promise.deadline_tick)}`
        const resolvedTick = promise.resolved_tick === undefined || promise.resolved_tick === null
          ? null
          : String(promise.resolved_tick)
        const roleLabel = role === 'promisor' ? '你作出的承诺' : role === 'promisee' ? '对你作出的承诺' : '参与方未提供'
        const statusLabel = promise.status === 'breached' && promise.late_returned === true
          ? '已逾期 · 已补还'
          : promiseStatusLabel(promise.status)
        return <article className="shipment-card promise-card" key={promiseId ?? `promise-${index}`}>
          <header className="shipment-card-heading">
            <span className={`shipment-status ${promise.status === 'breached' ? 'shipment-status-spoiled' : ''}`}>{statusLabel}</span>
            <small>{roleLabel}</small>
          </header>
          <p className="promise-text">{text}</p>
          <dl className="shipment-parties">
            <div><dt>对方</dt><dd>{counterpart}</dd></div>
            <div><dt>{active ? '履行期限' : '期限'}</dt><dd>{deadline}</dd></div>
          </dl>
          {hasTerms && <div className="shipment-cargo" aria-label="承诺物资与数量">
            <div className="shipment-cargo-row"><span>{resourceId}</span><strong>{String(quantity)}</strong></div>
          </div>}
          {!active && resolvedTick !== null && <p className="shipment-remaining">结清 tick {resolvedTick}</p>}
          {active && promiseId && role === 'promisor' && <div className="promise-actions">
            <button
              className="button button-secondary button-small"
              onClick={() => onPrepareAction(`请履行承诺 ${promiseId}`)}
              disabled={!canPrepareAction}
              aria-label="把履行承诺的行动填入输入框"
              title={canPrepareAction ? '填入行动草稿；发送后由规则引擎处理' : '清空输入框或等待当前请求完成后再操作'}
            >准备履行</button>
          </div>}
          {active && promiseId && role === 'promisee' && <div className="promise-actions">
            <button
              className="button button-secondary button-small"
              onClick={() => onPrepareAction(`请解除承诺 ${promiseId}`)}
              disabled={!canPrepareAction}
              aria-label="把解除承诺的行动填入输入框"
              title={canPrepareAction ? '填入行动草稿；发送后由规则引擎处理' : '清空输入框或等待当前请求完成后再操作'}
            >准备解除</button>
          </div>}
          {promise.status === 'breached' && promise.late_returned !== true && promiseId && role === 'promisor' && promise.can_return_late === true && <div className="promise-actions">
            <button
              className="button button-secondary button-small"
              onClick={() => onPrepareAction(`我现在补还已逾期的归还承诺 ${promiseId}，请按承诺约定归还物资。`)}
              disabled={!canPrepareAction}
              aria-label="把补还逾期承诺的行动填入输入框"
              title={canPrepareAction ? '填入行动草稿；发送后由规则引擎处理' : '清空输入框或等待当前请求完成后再操作'}
            >准备补还</button>
          </div>}
        </article>
      })}
    </div>}
  </details>
}

function isOwnPromise(value: unknown): value is OwnPromise {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isMarketOfferQuote(value: unknown): value is MarketOfferQuote {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const offer = value as Partial<MarketOfferQuote>
  return typeof offer.listing_id === 'string'
    && typeof offer.resource_id === 'string'
    && typeof offer.unit_price_minor === 'string'
    && typeof offer.minimum_quantity === 'string'
}

function MarketQuotesPanel({ offers, currencyResourceId, canPrepareQuery, onAsk }: {
  offers: MarketOfferQuote[]
  currencyResourceId: string | null
  canPrepareQuery: boolean
  onAsk: (text: string) => void
}) {
  return <details className="shipment-panel market-quotes-panel" aria-label="本地市场挂牌报价">
    <summary className="shipment-panel-summary">
      <span><Icon name="box" size={14} />本地市场报价</span>
      <small>{offers.length} 条挂牌</small>
    </summary>
    <div className="shipment-list" aria-live="polite">
      {offers.map((offer) => <article className="shipment-card market-quote-card" key={offer.listing_id}>
        <header className="shipment-card-heading">
          <strong>{offer.resource_id}</strong>
          <small>挂牌报价</small>
        </header>
        <dl className="shipment-parties market-quote-values">
          <div><dt>挂牌价（最小货币单位）</dt><dd>{offer.unit_price_minor}{currencyResourceId ? ` ${currencyResourceId}` : ''}</dd></div>
          <div><dt>最低购买量</dt><dd>{offer.minimum_quantity}</dd></div>
        </dl>
        <button
          className="button button-secondary button-small shipment-receive-button market-quote-ask"
          onClick={() => onAsk(`请帮我查询这条本地挂牌报价：${offer.resource_id}（报价单 ${offer.listing_id}）。请说明挂牌价与最低购买量。`)}
          disabled={!canPrepareQuery}
          aria-label={`把询问 ${offer.resource_id} 挂牌报价的文字填入 GM 输入框`}
          title={canPrepareQuery ? '填入 GM 查询草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}
        >问 GM</button>
      </article>)}
    </div>
  </details>
}

function MessageReferences({ references, entities, promises, marketOffers }: {
  references?: string[]
  entities: Record<string, CharacterEntity>
  promises: OwnPromise[]
  marketOffers: MarketOfferQuote[]
}) {
  if (!Array.isArray(references)) return null
  const promiseIds = new Set(promises.flatMap((promise) =>
    typeof promise.promise_id === 'string' && promise.promise_id ? [promise.promise_id] : [],
  ))
  const marketById = new Map(marketOffers.map((offer) => [offer.listing_id, offer]))
  const chips: { id: string; kind: 'entity' | 'promise' | 'market'; label: string }[] = []
  for (const id of references) {
    if (typeof id !== 'string' || !id) continue
    const entity = entities[id]
    if (entity) chips.push({ id, kind: 'entity', label: entity.name || `实体 ${id.slice(0, 8)}` })
    else if (promiseIds.has(id)) chips.push({ id, kind: 'promise', label: '归还承诺' })
    else {
      const offer = marketById.get(id)
      if (offer) chips.push({ id, kind: 'market', label: `市场挂牌 · ${offer.resource_id}` })
    }
  }
  if (!chips.length) return null
  const hasEntity = chips.some((chip) => chip.kind === 'entity')
  const nonEntityKinds = new Set(chips.filter((chip) => chip.kind !== 'entity').map((chip) => chip.kind))
  const groupLabel = nonEntityKinds.size > 1 || hasEntity && nonEntityKinds.size > 0
    ? '关联记录'
    : chips[0].kind === 'promise' ? '归还承诺' : chips[0].kind === 'market' ? '市场挂牌' : '关联实体'
  return <div className="message-references" role="group" aria-label={groupLabel}>
    <span>{groupLabel}</span>
    {chips.map((chip) => <span className="message-reference" key={`${chip.kind}-${chip.id}`}>
      <Icon name={chip.kind === 'entity' ? 'users' : chip.kind === 'market' ? 'box' : 'book'} size={11} />{chip.label}
    </span>)}
  </div>
}

function promiseStatusLabel(status?: string) {
  return ({ active: '待履行', fulfilled: '已履行', breached: '未按期履行', released: '已解除' } as Record<string, string>)[status ?? ''] ?? '状态未提供'
}

function ShipmentsPanel({ shipments, entities, playerName, projectionAvailable, loading, viewError, canPrepareAction, onReceive }: {
  shipments: OwnShipment[]
  entities: Record<string, CharacterEntity>
  playerName: string
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onReceive: (shipmentId: string) => void
}) {
  return <details className="shipment-panel" aria-label="角色货运状态">
    <summary className="shipment-panel-summary">
      <span><Icon name="box" size={14} />货运状态</span>
      <small>{shipments.length ? `关联运单 ${shipments.length} 笔` : '展开查看运单'}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取货运投影…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的货运状态。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && shipments.length === 0 && <p className="shipment-empty">目前没有与你的角色关联的运单。</p>}
    {!loading && !viewError && projectionAvailable && shipments.length > 0 && <div className="shipment-list" aria-live="polite">
      {shipments.map((shipment) => {
        const spoiled = shipment.status === 'spoiled' || shipment.spoiled === true
        const senderName = shipment.role === 'sender'
          ? playerName
          : shipment.sender_id ? entities[shipment.sender_id]?.name ?? '发货方' : '发货方（身份未提供）'
        const origin = shipment.origin_place_name || entities[shipment.origin_place_id]?.name || '未显示地点'
        const destinationName = shipment.destination_place_name || entities[shipment.destination_place_id]?.name
        const destination = destinationName ?? '未显示地点'
        const receiverName = shipment.role === 'receiver'
          ? playerName
          : shipment.receiving_custody === 'place_depot' ? `${destinationName ?? '目的地'}仓点`
            : shipment.receiver_id ? entities[shipment.receiver_id]?.name ?? '收货方' : '收货方（身份未提供）'
        const canReceive = shipment.can_receive && shipment.role === 'receiver'
        const arrived = shipment.arrived_tick !== undefined && shipment.arrived_tick !== null
        return <article className="shipment-card" key={shipment.shipment_id}>
          <header className="shipment-card-heading">
            <span className={`shipment-status ${spoiled ? 'shipment-status-spoiled' : ''}`}>{shipmentStatusLabel(shipment.status, spoiled)}</span>
            <small>{shipment.role === 'sender' ? '你的角色：发货方' : shipment.role === 'receiver' ? '你的角色：收货方' : '货运参与方'}</small>
          </header>
          <p className="shipment-route" aria-label={`路线：${origin} 至 ${destination}`}>
            <span>{origin}</span><span aria-hidden="true">→</span><span>{destination}</span>
          </p>
          <dl className="shipment-parties">
            <div><dt>发货方</dt><dd>{senderName}</dd></div>
            <div><dt>收货方</dt><dd>{receiverName}</dd></div>
          </dl>
          <div className="shipment-cargo" aria-label="货物清单">
            {shipment.cargo.map((line) => <div className="shipment-cargo-row" key={line.resource_id}>
              <span>{line.resource_id}</span>
              <strong>{line.remaining_quantity !== undefined && line.remaining_quantity !== null
                ? `${String(line.remaining_quantity)} / ${String(line.quantity)}`
                : spoiled ? `原始 ${String(line.quantity)} · 余量未提供` : String(line.quantity)}</strong>
              {line.spoiled_quantity !== undefined && line.spoiled_quantity !== null && <small>变质 {String(line.spoiled_quantity)}</small>}
              {line.delivered_quantity !== undefined && line.delivered_quantity !== null && <small>已交付 {String(line.delivered_quantity)}</small>}
            </div>)}
          </div>
          {shipment.remaining_quantity !== undefined && shipment.remaining_quantity !== null && <p className="shipment-remaining">剩余总量：{String(shipment.remaining_quantity)}</p>}
          <p className="shipment-arrival">{arrived ? `已抵达 · tick ${String(shipment.arrived_tick)}` : `预计抵达 · tick ${String(shipment.arrival_tick)}`}</p>
          {shipment.status === 'awaiting_receipt' && !canReceive && shipment.role === 'receiver' && <p className="shipment-receive-note">到达收货地点后即可领取。</p>}
          {canReceive && <button
            className="button button-secondary button-small shipment-receive-button"
            onClick={() => onReceive(shipment.shipment_id)}
            disabled={!canPrepareAction}
            aria-label={`把运单 ${shipment.shipment_id.slice(0, 8)} 的接收行动填入输入框`}
            title={canPrepareAction ? '填入行动草稿；发送后由规则引擎处理' : '清空输入框或等待当前请求完成后再领取'}
          >准备领取</button>}
        </article>
      })}
    </div>}
  </details>
}

function shipmentStatusLabel(status: string, spoiled: boolean) {
  if (spoiled) return '货物已变质'
  return ({ in_transit: '运输中', awaiting_receipt: '等待收货', delivered: '已交付' } as Record<string, string>)[status] ?? `状态：${status}`
}

const privateGiftStatusLabels: Record<OwnPrivateGiftView['status'] | 'expired', string> = {
  pending: '等待回应',
  expired: '已过期限',
  accepted: '已接受并交付',
  declined: '已拒绝',
  withdrawn: '已撤回',
}

function PrivateGiftsPanel({ gifts: rawGifts, entities, playerId, currentTick, projectionAvailable, loading, viewError, canPrepareAction, onPrepareAction }: {
  gifts: unknown
  entities: Record<string, CharacterEntity>
  playerId?: string
  currentTick: string | number
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const dataAvailable = Array.isArray(rawGifts)
  const gifts = dataAvailable ? rawGifts.filter(isOwnPrivateGift) : []
  const invalidCount = dataAvailable ? rawGifts.length - gifts.length : 0
  const currentTickValue = parsePrivateGiftTick(currentTick)
  const pendingCount = gifts.filter((gift) => gift.status === 'pending'
    && !(currentTickValue !== null && parsePrivateGiftTick(gift.expires_at_tick) !== null
      && currentTickValue >= parsePrivateGiftTick(gift.expires_at_tick)!)).length

  return <details className="shipment-panel promise-panel" aria-label="我的私人赠与">
    <summary className="shipment-panel-summary">
      <span><Icon name="box" size={14} />私人赠与</span>
      <small>{loading ? '读取中' : !projectionAvailable ? '角色投影未载入' : !dataAvailable ? '赠与投影未提供' : `${gifts.length} 条记录 · ${pendingCount} 条待回应`}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取私人赠与记录…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的私人赠与记录。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !dataAvailable && <p className="shipment-empty">当前投影未提供私人赠与记录。</p>}
    {!loading && !viewError && projectionAvailable && dataAvailable && gifts.length === 0 && <p className="shipment-empty">目前没有与你相关的私人赠与。</p>}
    {!loading && !viewError && projectionAvailable && dataAvailable && gifts.length > 0 && <div className="shipment-list" aria-live="polite">
      {invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条赠与记录无法读取，已隐藏。</p>}
      {gifts.map((gift) => {
        const isGiver = gift.giver_id === playerId
        const counterpartId = isGiver ? gift.recipient_id : gift.giver_id
        const counterpartName = entities[counterpartId]?.name?.trim() || '对方（角色资料未载入）'
        const status = gift.status === 'pending' && currentTickValue !== null
          && (parsePrivateGiftTick(gift.expires_at_tick) ?? BigInt(-1)) <= currentTickValue
          ? 'expired'
          : gift.status
        const actionAllowed = canPrepareAction && Boolean(gift.gift_id)
        const actionDetails = `私人赠与 ${gift.gift_id}，当前版本 ${gift.revision}`
        return <article className="shipment-card promise-card" key={gift.gift_id}>
          <header className="shipment-card-heading">
            <span className={`shipment-status ${status === 'declined' || status === 'withdrawn' || status === 'expired' ? 'shipment-status-spoiled' : ''}`}>{privateGiftStatusLabels[status]}</span>
            <small>{isGiver ? '你提出的赠与' : '向你提出的赠与'}</small>
          </header>
          <dl className="shipment-parties">
            <div><dt>送礼人</dt><dd>{isGiver ? '你' : counterpartName}</dd></div>
            <div><dt>收礼人</dt><dd>{isGiver ? counterpartName : '你'}</dd></div>
            <div><dt>礼物</dt><dd>{gift.resource_id} · {gift.quantity}</dd></div>
            <div><dt>提出 tick</dt><dd>{formatPrivateGiftTick(gift.offered_tick)}</dd></div>
            <div><dt>回应期限</dt><dd>{formatPrivateGiftTick(gift.expires_at_tick)}</dd></div>
            {gift.resolved_tick !== null && <div><dt>回应 tick</dt><dd>{formatPrivateGiftTick(gift.resolved_tick)}</dd></div>}
          </dl>
          <p className="occupation-id">赠与记录 · {gift.gift_id}</p>
          {gift.can_accept && status === 'pending' && <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确接受${actionDetails}。`)} disabled={!actionAllowed} aria-label={`准备接受 ${counterpartName} 提出的私人赠与`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备接受</button>
          </div>}
          {gift.can_decline && status === 'pending' && <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确拒绝${actionDetails}。`)} disabled={!actionAllowed} aria-label={`准备拒绝 ${counterpartName} 提出的私人赠与`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备拒绝</button>
          </div>}
          {gift.can_withdraw && status === 'pending' && <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`我明确撤回${actionDetails}。`)} disabled={!actionAllowed} aria-label={`准备撤回给 ${counterpartName} 的私人赠与`} title={canPrepareAction ? '填入行动草稿；编辑后再发送' : '清空输入框或等待当前请求完成后再操作'}>准备撤回</button>
          </div>}
          {gift.can_request_reply && status === 'pending' && <div className="promise-actions">
            <button type="button" className="button button-secondary button-small" onClick={() => onPrepareAction(`请让收礼人自行决定是否接受${actionDetails}。`)} disabled={!actionAllowed} aria-label={`请求 ${counterpartName} 自行回应私人赠与`} title={canPrepareAction ? '填入行动草稿；对方回应仍须由世界规则确认' : '清空输入框或等待当前请求完成后再操作'}>请求对方自行回应</button>
          </div>}
        </article>
      })}
    </div>}
    {!loading && !viewError && projectionAvailable && dataAvailable && gifts.length > 0 && <p className="shipment-empty">按钮只会填入行动草稿；确认并发送后由世界规则处理。</p>}
  </details>
}

function isOwnPrivateGift(value: unknown): value is OwnPrivateGiftView {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const gift = value as Partial<OwnPrivateGiftView>
  return typeof gift.gift_id === 'string' && gift.gift_id.trim().length > 0
    && typeof gift.giver_id === 'string' && gift.giver_id.trim().length > 0
    && typeof gift.recipient_id === 'string' && gift.recipient_id.trim().length > 0
    && typeof gift.place_id === 'string' && gift.place_id.trim().length > 0
    && typeof gift.resource_id === 'string' && gift.resource_id.trim().length > 0
    && typeof gift.quantity === 'string' && isPositiveDecimalText(gift.quantity)
    && parsePrivateGiftTick(gift.offered_tick) !== null
    && parsePrivateGiftTick(gift.expires_at_tick) !== null
    && typeof gift.revision === 'number' && Number.isSafeInteger(gift.revision) && gift.revision >= 1
    && (gift.status === 'pending' || gift.status === 'accepted' || gift.status === 'declined' || gift.status === 'withdrawn')
    && (gift.resolved_tick === null || parsePrivateGiftTick(gift.resolved_tick) !== null)
    && typeof gift.can_accept === 'boolean' && typeof gift.can_decline === 'boolean'
    && typeof gift.can_withdraw === 'boolean' && typeof gift.can_request_reply === 'boolean'
}

function parsePrivateGiftTick(value: unknown): bigint | null {
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) return null
    return BigInt(value)
  }
  if (typeof value !== 'string' || !/^-?(?:0|[1-9]\d*)$/.test(value)) return null
  try {
    const parsed = BigInt(value)
    return parsed >= -(2n ** 63n) && parsed <= (2n ** 63n) - 1n ? parsed : null
  } catch {
    return null
  }
}

function formatPrivateGiftTick(value: string | number): string {
  const parsed = parsePrivateGiftTick(value)
  return parsed === null ? '无法精确读取' : parsed.toString()
}

function isPositiveDecimalText(value: string): boolean {
  return /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) && /[1-9]/.test(value)
}

function narrativeRewriteSource(requestId: string, kind?: string, rewrites?: NarrativeRewrites): GameMessage['rewriteSource'] {
  return requestId && kind && rewritableMessageKinds.has(kind) ? { requestId, rewrites } : undefined
}

function NarrativeRewriteText({ worldId, source, original, scopeRevision }: {
  worldId: string
  source: NonNullable<GameMessage['rewriteSource']>
  original: string
  scopeRevision: RefObject<number>
}) {
  const [rewrites, setRewrites] = useState<NarrativeRewrites>({})
  const [selectedStyle, setSelectedStyle] = useState<NarrativeRewriteStyle | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const available = { ...source.rewrites, ...rewrites }

  const chooseStyle = async (style: NarrativeRewriteStyle) => {
    if (inFlight.current) return
    setError(null)
    if (available[style]) {
      setSelectedStyle(style)
      return
    }

    const scope = scopeRevision.current
    inFlight.current = true
    setPending(true)
    try {
      const result = await api.rewriteNarrative(worldId, source.requestId, style)
      if (scope !== scopeRevision.current) return
      if (result.narrative_id !== source.requestId || result.style !== style || result.world_effect !== false || !result.content.trim()) {
        throw new Error('服务器返回的改写结果与请求不一致。')
      }
      setRewrites((current) => ({ ...current, [style]: result.content }))
      setSelectedStyle(style)
    } catch (reason) {
      if (scope === scopeRevision.current) setError(errorMessage(reason))
    } finally {
      inFlight.current = false
      if (scope === scopeRevision.current) setPending(false)
    }
  }

  return (
    <>
      {selectedStyle && <div className="message-meta"><span>{rewriteStyleLabels[selectedStyle]}改写</span></div>}
      <p>{selectedStyle ? available[selectedStyle] : original}</p>
      <div className="message-references" role="group" aria-label="回复改写">
        {rewriteStyles.some((style) => available[style]) && (
          <button type="button" className="text-button" aria-pressed={selectedStyle === null} disabled={pending} onClick={() => { setSelectedStyle(null); setError(null) }}>查看原文</button>
        )}
        {rewriteStyles.map((style) => (
          <button key={style} type="button" className="text-button" aria-pressed={selectedStyle === style} disabled={pending} onClick={() => { void chooseStyle(style) }}>
            {available[style] ? '查看' : ''}{rewriteStyleLabels[style]}改写
          </button>
        ))}
        {pending && <span role="status">正在改写…</span>}
      </div>
      {error && <p role="alert">改写失败。{error}</p>}
    </>
  )
}

function messagesFromConversation(items: ConversationItem[]): GameMessage[] {
  return items.flatMap((item) => {
    const status = item.status
    const playerMessage: GameMessage = {
      id: `request-${item.request_id}`,
      role: 'player',
      content: item.text,
      source: 'api',
      delivery: status === 'succeeded' ? 'accepted' : status === 'running' ? 'pending' : 'failed',
    }
    const messages: GameMessage[] = [playerMessage]
    if (status === 'succeeded' && item.result?.content) {
      messages.push({
        id: item.result.message_id ?? `response-${item.request_id}`,
        role: 'gm',
        content: item.result.content,
        source: 'api',
        kind: item.result.kind,
        worldEffect: item.result.world_effect,
        elapsedTicks: item.result.elapsed_ticks,
        references: item.result.references,
        rewriteSource: narrativeRewriteSource(item.request_id, item.result.kind, item.result.rewrites),
      })
    } else if (status === 'running') {
      messages.push({ id: `pending-${item.request_id}`, role: 'system', content: '请求仍在处理中。', source: 'api' })
    } else if (status === 'failed') {
      const error = item.result?.error
      const errorMessageValue = typeof error === 'string'
        ? error
        : error && typeof error === 'object' && typeof error.message === 'string'
          ? error.message
          : undefined
      messages.push({
        id: `error-${item.request_id}`,
        role: 'error',
        content: item.result?.message ?? errorMessageValue ?? '此请求未确认完成。请先核对世界历史与调用记录。',
        source: 'api',
      })
    }
    return messages
  })
}

function messageKindLabel(kind: string) {
  return ({ query: '询问', hypothetical: '假设推演', action: '行动', dialogue: '角色对话', author: '作者指令', clarification: '需要澄清', intent: '意图识别' } as Record<string, string>)[kind] ?? kind
}

function jobStatusLabel(status: string) { return ({ queued: '等待中', running: '处理中', waiting_input: '等待输入', paused: '已暂停', succeeded: '已完成', failed: '失败', cancelled: '已取消', unavailable: '状态未知' } as Record<string, string>)[status] ?? status }
