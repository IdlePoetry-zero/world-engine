export type ApiErrorBody = {
  code?: string
  message?: string
  retryable?: boolean
  field_errors?: Record<string, string> | { path: string; type: string; message?: string }[]
  correlation_id?: string
  committed_through_tick?: string
}
type ApiErrorEnvelope = ApiErrorBody & { error?: ApiErrorBody }

export class ApiError extends Error {
  readonly code?: string
  readonly retryable?: boolean
  readonly field_errors?: Record<string, string> | { path: string; type: string; message?: string }[]
  readonly correlation_id?: string
  readonly committed_through_tick?: string
  readonly status: number

  constructor(status: number, body: ApiErrorBody) {
    super(body.message ?? `本地 API 请求失败 (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.retryable = body.retryable
    this.field_errors = body.field_errors
    this.correlation_id = body.correlation_id
    this.committed_through_tick = body.committed_through_tick
  }
}

export type Session = { principal: { id: string; display_name: string }; permissions: string[] }
export type World = { world_id: string; title: string; active_branch_id: string | null; current_tick: string; state_version: string; status: string; ruleset_version?: string; pack_version?: string; world_mode?: 'story' }
export type Pack = { pack_id: string; title: string; version: string; available_versions?: string[]; sustained_available_versions?: string[]; description: string }
export type WorldPackPreview = {
  manifest: { pack_id: string; version: string; title: string; license: string; engine_range: string }
  compatibility: {
    dependencies: { pack_id: string; version: string }[]
    unsupported_fields: { section: string; field: string }[]
    inert_asset_count: number
  }
  can_import: boolean
  blockers: string[]
  contains_author_material: boolean
  archive_sha256: string
  story_openings?: {
    opening_id: string
    title: string
    position: string
    known_history: string[]
    opening_state?: { player_name: string | null; place_name: string | null }
  }[]
}
export type WorldPackImportResult = World & { duplicate: boolean }
export type ChallengeOption = {
  challenge_id: string
  title: string
  description: string
  skill_label: string
  pass_level: string
  fee: { resource_id: string; quantity: string; label: string } | null
  passed: boolean | null
}
export type ContestAction =
  | { operation: 'invite'; definition_id: string; recipient_id: string; routine: string }
  | { operation: 'reply'; contest_id: string; routine: string | null }
  | { operation: 'ask_npc' | 'settle' | 'withdraw'; contest_id: string }
export type ContestResult = {
  contest_id: string
  status: 'invited' | 'accepted' | 'declined' | 'withdrawn' | 'settled'
  proposer_id: string
  recipient_id: string
  winner_id: string | null
  proposer_wins: number
  recipient_wins: number
  rounds: number
}
export type ContestOption = {
  definition_id: string
  title: string
  terms: string
  can_invite: boolean
  opponents: { id: string; name: string }[]
  routines: { key: string; title: string }[]
  recent: (ContestResult & { can_ask_npc: boolean })[]
}
export type TemplateUpdateChange = {
  entity_id: string
  entity_name: string
  component: string
  status: 'ready' | 'already_applied' | 'conflict' | 'unsupported'
  message: string
  fields?: string[]
}
export type TemplateUpdatePreview = {
  preview_id: string
  source_version: string
  target_version: string
  base_sequence: string
  base_version: string
  changes: TemplateUpdateChange[]
  can_apply: boolean
}
export type TemplateUpdateResult = {
  branch_id: string
  checkpoint_sequence: string
  state_version: string
  duplicate: boolean
}
export type Branch = { branch_id: string; label: string; current_tick: string; state_version: string; head_seq: string }
export type PlayerContinuationOption = { actor_id: string; name: string; role: string }
export type PlayerContinuationOptions = {
  previous_player_id: string
  state_version: string
  items: PlayerContinuationOption[]
}
export type PlayerContinuationResult = {
  branch_id: string
  player_id: string
  state_version: string
  command_id: string
}
export type CharacterEntity = { id: string; kind: string; name?: string | null; location_id?: string; status?: string; components?: Record<string, unknown> }
export type AuthorKinshipLink = {
  link_id: string
  parent_id: string
  child_id: string
  relationship: 'birth' | 'adoptive'
  created_tick: string | number
  ended_tick?: string | number | null
}
export type AuthorKinshipResult = {
  command_id: string
  event_id: string
  link_id: string
  state_version: string
  duplicate: boolean
  world_time_effect: false
}
export type ResourceAccount = { resource_id: string; balance: string; owner_id?: string; account_type?: string; credit_limit?: string }
export type OwnShipmentCargo = {
  resource_id: string
  quantity: string | number
  remaining_quantity?: string | number | null
  spoiled_quantity?: string | number | null
  delivered_quantity?: string | number | null
}
export type OwnShipment = {
  shipment_id: string
  role: 'sender' | 'receiver' | string
  status: 'in_transit' | 'awaiting_receipt' | 'delivered' | 'spoiled' | string
  departure_tick: string | number
  arrival_tick: string | number
  arrived_tick?: string | number | null
  origin_place_id: string
  destination_place_id: string
  origin_place_name?: string | null
  destination_place_name?: string | null
  receiving_custody?: 'place_depot' | 'designated_holder'
  sender_id?: string
  receiver_id?: string
  cargo: OwnShipmentCargo[]
  can_receive: boolean
  remaining_quantity?: string | number | null
  spoiled?: boolean
}
export type ShipmentReliefOrganizationOption = { organization_id: string; name: string }
export type ShipmentReliefRecipientOption = { recipient_id: string; name: string }
export type ShipmentReliefOption = {
  shipment_id: string
  depot_name: string
  resource_id: string
  available_quantity: string
  organizations: ShipmentReliefOrganizationOption[]
  recipients: ShipmentReliefRecipientOption[]
}
export type OwnPromise = {
  promise_id?: string
  role?: 'promisor' | 'promisee' | string
  counterparty_id?: string
  text?: string
  status?: 'active' | 'fulfilled' | 'breached' | 'released' | string
  late_returned?: boolean
  can_return_late?: boolean
  created_tick?: string | number
  deadline_tick?: string | number
  resolved_tick?: string | number | null
  terms?: {
    kind?: string
    resource_id?: string
    quantity?: string | number
  } | null
}
export type OwnRelationshipInvitation = {
  invitation_id: string
  proposer_id: string
  invitee_id: string
  status: 'pending' | 'declined' | 'accepted' | 'withdrawn' | 'ended' | string
  proposed_tick: string | number
  updated_tick: string | number
  revision: string | number
  meeting?: { start_tick: string | number; end_tick: string | number; venue_id?: string } | null
  meeting_venue_name?: string
  meeting_attendance?: { actor_id: string; attended_tick: string | number }[] | null
}
export type OwnMeetingReflection = {
  invitation_id: string
  actor_id: string
  outlook: 'closer' | 'unchanged' | 'uneasy'
  recorded_tick: string | number
}
export type OwnRelationshipPartnership = {
  partnership_id: string
  proposer_id: string
  partner_id: string
  current_proposal_author_id?: string
  current_proposal_recipient_id?: string
  status: 'pending' | 'active' | 'declined' | 'withdrawn' | 'ended'
  proposed_tick: string | number
  updated_tick: string | number
  revision: string | number
  proposal_terms?: string[]
  can_revise?: boolean
  can_counter?: boolean
  can_accept?: boolean
  can_decline?: boolean
  can_withdraw?: boolean
  can_request_response?: boolean
}
export type RelationshipDisclosureRecipientOption = {
  recipient_id: string
  recipient_name: string
}
export type RelationshipDisclosureOption = {
  partnership_id: string
  expected_revision: string | number
  opposite_participant_id: string
  opposite_participant_name: string
  recipient_options: RelationshipDisclosureRecipientOption[]
}
export type OwnRelationshipDisclosure = {
  record_id: string
  statement: string
  source: 'past_statement_by_discloser'
  discloser_id: string
  opposite_participant_id: string
  location_id: string
  disclosed_tick: string | number
}
export type OwnMembership = {
  organization_id: string
  name?: string | null
  is_leader: boolean
  ballot_policy?: 'election' | 'council' | string
  ballot_nominees?: { member_id: string; name?: string | null }[]
  vacancy_ballot_nominees?: { member_id: string; name?: string | null }[]
  vacancy_ballot_existing_office_holder_id?: string
}
export type OwnLeadershipBallot = {
  ballot_id: string
  organization_id: string
  nominee_id: string
  nominee_name?: string | null
  status: 'open' | 'approved' | 'rejected' | 'cancelled' | string
  policy: 'election' | 'council' | string
  revision: string | number
  is_incumbent: boolean
  is_vacancy_proposer?: boolean
  is_voter: boolean
  own_vote: 'yes' | 'no' | 'abstain' | null
  waiting_voters?: { voter_id: string; name?: string | null }[]
}
export type SafeConductProposalOption = {
  proposer_organization_id: string
  proposer_name: string
  recipient_organization_id: string
  recipient_name: string
  from_place_id: string
  from_place_name: string
  to_place_id: string
  to_place_name: string
  route_id: string
}
export type OwnSafeConductTreaty = {
  treaty_id: string
  proposer_organization_id: string
  recipient_organization_id: string
  from_place_id: string
  from_place_name?: string | null
  to_place_id: string
  to_place_name?: string | null
  route_id: string
  expires_at_tick: string | number
  status: 'pending' | 'countered' | 'active' | 'declined' | 'withdrawn' | 'revoked' | string
  revision: string | number
  proposer_name?: string | null
  recipient_name?: string | null
  counter_expires_at_tick?: string | number | null
  expired: boolean
  represents_proposer: boolean
  represents_recipient: boolean
  can_ratify?: boolean
  can_request_response?: boolean
  can_counter?: boolean
  can_accept_counter?: boolean
}
export type CombatTruceOptions = {
  opponent_id: string
  opponent_name?: string | null
  expected_last_event_id: string
  offer_id: string | null
  proposed_by_self: boolean
  can_request_response: boolean
}
export type OwnCombatTruce = {
  offer_id: string
  proposer_id: string
  recipient_id: string
  status: 'pending' | 'accepted' | 'declined'
  offered_tick: string | number
  responded_tick: string | number | null
}
export type CultivationBreakthroughOption = {
  policy_id: string
  source_realm_name: string
  target_realm_name: string
  duration_ticks: string
  qi_cost: string
  minimum_skill_level: string
  practice_skill_level?: string | null
  failure_damage: number
  available: boolean
  chance_percent: number | null
  blocked_reason: string | null
}
export type WorkSkillRequirementView = {
  skill_id: string
  minimum_level: string | number
}
export type LocalWorkPosting = {
  posting_id: string
  title: string
  required_skill?: WorkSkillRequirementView | null
  revision: string | number
}
export type OwnWorkAssignment = {
  assignment_id: string
  posting_id: string
  title: string
  started_tick: string | number
  ended_tick?: string | number | null
  revision: string | number
  work_shift?: WorkShiftOffer | null
}
export type OwnStudySkill = {
  skill_id: string
  level: string | number
}
export type AuthorWorkAssignment = {
  assignment_id: string
  actor_id: string
  posting_id: string
  title?: string
  ended_tick?: string | number | null
  revision?: number
}
export type LegacyVacancySettlement = {
  settled: boolean
  commit: { state_version: string; duplicate: boolean } | null
}
export type AuthorLeadershipBallot = {
  ballot_id: string
  organization_id: string
  nominee_id: string
  revision: number | string
  status: 'open' | 'approved' | 'rejected' | 'cancelled' | string
}
export type InvalidatedLeadershipBallotCancellation = {
  commit: { state_version: string; duplicate: boolean } | null
}
export type WorkShiftOffer = {
  duration_seconds: number
  resource_id: string
  wage: string
  can_work_shift: boolean
}
export type MarketOfferQuote = {
  listing_id: string
  resource_id: string
  unit_price_minor: string
  minimum_quantity: string
}
export type MarketOffersView = {
  currency_resource_id: string | null
  offers: MarketOfferQuote[]
}
export type RelationView = { id?: string; from_id: string; to_id: string; dimensions?: Record<string, string | number>; label?: string; evidence_refs?: string[] }
export type PlayerKnowledgeView = {
  id: string
  claim: string
  summary?: string | null
  confidence?: string | number
  source_refs?: string[]
  received_tick?: number
  kind?: string
}
export type AvailableCaseAnchor = {
  anchor_id: string
  case_id: string
  title: string
  brief: string
}
export type InvestigationCaseFileView = {
  id: string
  case_id: string
  anchor_id: string
  owner_id: string
  title: string
  brief: string
  status: 'open' | 'closed'
  revision: number
  opened_tick: number
  updated_tick: number
  evidence_refs: string[]
  closed_tick?: number | null
  outcome?: 'supported' | 'disconfirmed' | 'contested' | 'unresolved' | null
  suspect_id?: string | null
  public_accusation?: boolean
  public_accusation_corrected_tick?: string | number
}
export type AccusationRepairOfferView = {
  offer_id: string
  revision: string | number
  accuser_id: string
  suspect_id: string
  statement: string
  status: 'pending' | 'accepted' | 'declined'
  last_operation?: 'accept' | 'decline' | 'defer' | null
}
export type HistoryEvent = {
  event_id: string
  world_id?: string
  branch_id: string
  type: string
  tick: string
  payload: unknown
  visibility?: { policy?: string; subject_ids?: string[]; [key: string]: unknown }
  actor_id?: string
  sequence: string
  can_fork: boolean
}
export type MediaKind = 'speech' | 'image'
export type MediaSubscription = { event_type: string; kind: MediaKind }
export type MediaManifest = {
  id: string
  version: string
  protocol: string
  capabilities: MediaKind[]
  subscriptions: MediaSubscription[]
  maximum_asset_bytes: number
}
export type MediaRequestMetadata = {
  source_event_id: string
  plugin_id: string
  kind: MediaKind
  audience: 'public' | 'participants' | 'author_only'
  actor_id?: string | null
}
export type MediaJobRequest = Omit<MediaRequestMetadata, 'actor_id'> & {
  actor_id?: string
  description: string
}
export type StoredMediaAsset = {
  request_id: string
  world_id: string
  branch_id: string
  source_event_ids: string[]
  content_hash: string
  content_type: string
  byte_size: number
  visibility: { policy: 'public' | 'participants' | 'author_only'; subject_ids: string[] }
}
export type InstitutionalCreditInstitution = {
  organization_id: string
  name: string | null
  currency_resource_ids: string[]
  currency_resource_ids_truncated: boolean
}
export type InstitutionalCreditCounterparty = {
  represented_organization_id: string
  counterparty_organization_id: string
  counterparty_name: string | null
  shared_currency_resource_ids: string[]
  shared_currency_resource_ids_truncated: boolean
}
export type InstitutionalCreditContract = {
  credit_id: string
  status: 'offered' | 'active' | 'declined' | 'withdrawn' | 'overdue' | 'settled'
  phase: string
  revision: number | string
  lender_organization_id: string
  borrower_organization_id: string
  lender_name?: string
  borrower_name?: string
  currency_resource_id: string
  principal_minor: string
  total_due_minor: string
  repaid_minor: string
  remaining_total_minor: string
  accepted_through_tick: string
  maturity_tick: string
  accepted_tick?: string
  overdue_at_tick?: string
  can_accept: boolean
  can_decline: boolean
  can_withdraw: boolean
  can_pay: boolean
  extension_options?: InstitutionalCreditExtensionOption[]
  extension_options_truncated?: boolean
  extensions?: InstitutionalCreditExtension[]
  extensions_truncated?: boolean
}
export type InstitutionalCreditExtensionOption = {
  organization_id: string
  name: string | null
}
export type InstitutionalCreditExtension = {
  extension_id: string
  proposer_organization_id: string
  previous_maturity_tick: string
  new_maturity_tick: string
  accept_through_tick: string
  proposed_tick: string
  resolved_tick?: string | null
  previous_overdue_tick?: string | null
  status: 'proposed' | 'accepted' | 'declined' | 'withdrawn'
  expired: boolean
  can_accept: boolean
  can_decline: boolean
  can_withdraw: boolean
}
export type InstitutionalCreditContext = {
  snapshot_tick: string
  represented_institutions: InstitutionalCreditInstitution[]
  represented_institutions_truncated: boolean
  visible_credit_counterparties: InstitutionalCreditCounterparty[]
  visible_credit_counterparties_truncated: boolean
  institutional_credit_contracts: InstitutionalCreditContract[]
  institutional_credit_contracts_truncated: boolean
}
export type InstitutionalFinanceLiquidityStatus = 'no_due_credit' | 'treasury_unknown' | 'covered' | 'cash_shortfall'
export type InstitutionalFinanceCurrency = {
  currency_resource_id: string
  treasury_balance_minor: string | null
  reserve_cash_minor?: string
  due_credit_minor: string
  overdue_credit_minor: string
  future_credit_minor: string
  due_cash_shortfall_minor: string | null
  liquidity_status: InstitutionalFinanceLiquidityStatus
  due_credit_ids: string[]
  due_credit_ids_truncated: boolean
}
export type InstitutionalFinanceAsset = { resource_id: string; quantity: string }
export type InstitutionalFinanceOrganization = {
  organization_id: string
  name: string | null
  currencies: InstitutionalFinanceCurrency[]
  currencies_truncated: boolean
  other_assets: InstitutionalFinanceAsset[]
  other_assets_truncated: boolean
}
export type InstitutionalFinanceContext = {
  snapshot_tick: string
  organizations: InstitutionalFinanceOrganization[]
  organizations_truncated: boolean
}
export type WorldState = {
  entities: Record<string, CharacterEntity>
  player_id?: string
  calendar?: { name?: string; calendar_id?: string; day_seconds?: number; year_days?: number; [key: string]: unknown }
  world_book_entries?: WorldBookEntry[] | null
  story_milestones?: { record_id: string; title: string; summary: string; resolved_tick: string }[]
  local_challenges?: ChallengeOption[]
  local_contests?: ContestOption[]
  metadata?: Record<string, unknown>
  resources: Record<string, ResourceAccount>
  own_shipments?: OwnShipment[]
  shipment_relief_options?: ShipmentReliefOption[] | null
  travel_options?: TravelOption[] | null
  itinerary_route_options?: ItineraryRouteOption[] | null
  own_active_travel?: OwnActiveTravel | null
  own_promises?: OwnPromise[] | null
  own_relationship_invitations?: OwnRelationshipInvitation[] | null
  own_meeting_reflections?: OwnMeetingReflection[] | null
  own_relationship_partnerships?: OwnRelationshipPartnership[] | null
  partnership_proposal_target_ids?: string[] | null
  relationship_disclosure_options?: RelationshipDisclosureOption[] | null
  own_relationship_disclosures?: OwnRelationshipDisclosure[] | null
  own_memberships?: OwnMembership[] | null
  own_leadership_ballots?: OwnLeadershipBallot[] | null
  safe_conduct_proposal_options?: SafeConductProposalOption[] | null
  own_safe_conduct_treaties?: OwnSafeConductTreaty[] | null
  combat_truce_options?: CombatTruceOptions | null
  own_combat_truces?: OwnCombatTruce[] | null
  cultivation_breakthrough_options?: CultivationBreakthroughOption[] | null
  leadership_ballots?: AuthorLeadershipBallot[]
  invalidated_leadership_ballot_ids?: string[]
  market_offers?: MarketOffersView | null
  local_work_postings?: LocalWorkPosting[] | null
  own_work_assignments?: OwnWorkAssignment[] | null
  own_study_skills?: OwnStudySkill[] | null
  work_assignments?: AuthorWorkAssignment[]
  kinship_links?: AuthorKinshipLink[]
  relations: RelationView[]
  knowledge: PlayerKnowledgeView[]
  goals: unknown[]
  scheduled_events?: unknown[]
  available_case_anchors?: AvailableCaseAnchor[]
  case_files?: InvestigationCaseFileView[]
  own_accusation_repair_offers?: AccusationRepairOfferView[]
  institutional_credit?: InstitutionalCreditContext | null
  institutional_finance?: InstitutionalFinanceContext | null
}

export type TravelOption = {
  route_id: string
  destination_name: string
  travel_ticks: string | number
}
export type ItineraryRouteOption = {
  route_id: string
  from_place_id: string
  from_place_name: string
  to_place_id: string
  to_place_name: string
  travel_ticks: string
}
export type OwnActiveTravel = {
  journey_id: string
  route_id: string
  status: 'in_transit'
  arrival_tick: string | number
  departed_tick: string | number
  from_place_id: string
  to_place_id: string
}
export type WorldView = {
  world: World & { [key: string]: unknown }
  branch: Branch
  state: WorldState
  last_seq: string
  state_hash: string
}
export type WorldBookEntry = {
  entry_id: string
  source_key: string
  title: string
  content: string
  keywords: string[]
  always_on: boolean
  visibility: 'public' | 'author_only'
}
export type WorldBookImportRequest = {
  branch_id: string
  expected_state_version: string
  title: string
  book_json: string
}
export type WorldBookPreviewEntry = Pick<WorldBookEntry, 'source_key' | 'title' | 'content' | 'keywords' | 'always_on'>
export type WorldBookSkippedEntry = { source_key: string; reason: string }
export type WorldBookPreview = {
  preview_id: string
  title: string
  expected_version: string
  entries: WorldBookPreviewEntry[]
  skipped_entries: WorldBookSkippedEntry[]
  expires_at?: string
}
export type WorldBookImportResult = { entry_count: number; state_version: string; duplicate: boolean }
export type MessageRequest = {
  request_id: string
  world_id: string
  branch_id: string
  session_id: string
  actor_id: string
  expected_state_version: string
  mode: 'auto' | 'ask_gm' | 'roleplay' | 'action' | 'author'
  text: string
  profile: { speed: 'fast' | 'balanced' | 'patient'; depth: 'standard' | 'deep' | 'exhaustive' }
}
export type MessageResponse = { message_id: string; kind: string; content?: string; command_id?: string; job_id?: string; world_effect: boolean; committed_tick?: string; elapsed_ticks?: string; references?: string[]; state_version?: string }
export type CommandStatus = { status: 'queued' | 'running' | 'waiting_input' | 'succeeded' | 'failed' | 'cancelled' | string; result?: string | { content?: string; kind?: string; world_effect?: boolean; elapsed_ticks?: string; references?: string[]; state_version?: string }; error?: ApiErrorBody; committed_through_tick?: string }
export type LongActionWorkProgress = {
  shifts_paid: number
  wages_by_resource: Record<string, string>
  last_paid_tick: string
  balance_resource_id: string
  balance_after_last_pay: string
}
export type LongActionWorkResume = {
  skipped_shifts: number
  next_shift_tick: string | null
}
export type WorkPurchaseTargetRequest = {
  listing_id: string
  quantity: string
}
export type LongActionWorkPurchaseTarget = {
  listing_id: string
  resource_id: string
  currency_resource_id: string
  quantity: string
}
export type LongActionWorkPurchaseAffordability = {
  listing_id: string
  quantity: string
  currency_resource_id: string
  total_price_minor: string
  wage_balance: string
  confirmed_tick: string
}
export type LongActionItineraryProgress = {
  legs_total: number
  legs_arrived: number
}
export type JobStatusRecord = {
  job_id: string
  world_id: string
  branch_id: string
  job_type: string
  status: 'queued' | 'running' | 'waiting_input' | 'paused' | 'succeeded' | 'failed' | 'cancelled' | string
  committed_through_tick: string | null
  committed_state_version?: string | null
  started_tick?: string | null
  target_tick?: string | null
  created_at: string
  updated_at: string
  reason_code: string | null
  work_progress?: LongActionWorkProgress | null
  work_resume?: LongActionWorkResume | null
  work_purchase_target?: LongActionWorkPurchaseTarget | null
  work_purchase_affordability?: LongActionWorkPurchaseAffordability | null
  itinerary_progress?: LongActionItineraryProgress | null
  itinerary_route?: {
    from_name: string | null
    to_name: string | null
    travel_ticks: string
    arrived: boolean
    expected_arrival_tick: string | number | null
  }[]
  itinerary_requires_replan?: boolean
  study_stop_safety_at_or_below?: number
  asset?: StoredMediaAsset
  media_request?: MediaRequestMetadata
}
type LongActionRequestBase = {
  branch_id: string
  expected_state_version: string
  duration_seconds: number
  profile: { speed: Settings['speed']; depth: Settings['depth'] }
}
export type LongActionRequest = LongActionRequestBase & (
  | { activity: 'wait' | 'rest'; route_id?: never; skill_id?: never; study_stop_safety_at_or_below?: never }
  | { activity: 'travel'; route_id: string; route_ids?: never; itinerary_auto_care?: never; skill_id?: never; study_stop_safety_at_or_below?: never }
  | { activity: 'travel'; route_ids: string[]; route_id?: never; itinerary_auto_care?: boolean; skill_id?: never; study_stop_safety_at_or_below?: never }
  | { activity: 'study'; skill_id: string; route_id?: never; study_stop_safety_at_or_below?: number }
  | { activity: 'work'; work_schedule: RoutineWorkSchedule; route_id?: never; skill_id?: never; study_stop_safety_at_or_below?: never }
)
type RoutineWorkScheduleFields = {
  assignment_id: string
  first_shift_tick: number
  repeat_every_days: number
}
export type RoutineWorkSchedule = RoutineWorkScheduleFields & (
  | { balance_target: string; purchase_target?: never }
  | { balance_target?: never; purchase_target: WorkPurchaseTargetRequest }
  | { balance_target?: never; purchase_target?: never }
)
export type WorldAdvanceRequest = LongActionRequestBase
export type LongActionAccepted = {
  job_id: string
  status: JobStatusRecord['status']
  committed_through_tick: string | null
  started_tick?: string | null
  target_tick?: string | null
}
export type JobDecisionOption = { option_id: string; label: string; effect_summary: string }
export type JobDecision = { request_id: string; title: string; options: JobDecisionOption[] }
export type Provider = { provider_id: string; kind: 'deepseek' | 'openai_compatible' | 'ollama' | 'anthropic'; name: string; base_url: string; secret_ref?: string; enabled: boolean; default_model?: string; ollama_context_tokens?: number | null }
export type ProviderDraft = Omit<Provider, 'provider_id'>
export type ProviderCredentialStatus = { configured: boolean; backend: 'windows_credential_manager' | 'unavailable' }
export type ModelInfo = { id: string; name?: string; provider_id?: string; capability_evidence?: Record<string, unknown> | null; context_length?: number }
export type ModelRouteTarget = { provider_id: string; model?: string; max_output_tokens?: number }
export type ModelRoute = { provider_id?: string; model?: string; max_output_tokens?: number; fallbacks?: ModelRouteTarget[] }
export type Settings = {
  speed: 'fast' | 'balanced' | 'patient'
  depth: 'standard' | 'deep' | 'exhaustive'
  routes: Record<string, ModelRoute>
  local_only: boolean
}
export type TransportFailureKind = 'connect_timeout' | 'connect' | 'read_timeout' | 'write_timeout' | 'pool_timeout' | 'timeout' | 'remote_protocol' | 'network' | 'request'
export type CallAttemptRecord = {
  attempt_number?: number
  outcome?: string
  retryable?: boolean | null
  possibly_billed?: boolean | null
  transport_failure_kind?: TransportFailureKind | string | null
  [key: string]: unknown
}
export type CallTraceRecord = {
  attempts?: CallAttemptRecord[] | null
  transport_failure_kind?: TransportFailureKind | string | null
  error_code?: string | null
  [key: string]: unknown
}
export type CallRecord = {
  request_id: string
  role: string
  provider?: string
  model?: string
  started_at?: string
  duration_ms?: number
  usage?: { source?: string; total_tokens?: number | null; input_tokens?: number | null; output_tokens?: number | null; cached_input_tokens?: number | null; reasoning_tokens?: number | null }
  cost?: { state?: 'reported' | 'estimated' | 'unknown' | string; amount?: string | number | null; currency?: string | null; source_url?: string | null; price_effective_from?: string | null; components?: Record<string, string | number | null> } | null
  status: string | null
  trace?: CallTraceRecord | null
}
export type NarrativeRewriteStyle = 'concise' | 'literary'
export type NarrativeRewrites = Partial<Record<NarrativeRewriteStyle, string>>
export type NarrativeRewriteResult = { narrative_id: string; style: NarrativeRewriteStyle; content: string; world_effect: false }
export type GameMessage = {
  id: string
  role: 'player' | 'gm' | 'system' | 'error'
  content: string
  source: 'local' | 'api'
  kind?: string
  delivery?: 'pending' | 'accepted' | 'failed'
  worldEffect?: boolean
  elapsedTicks?: string
  references?: string[]
  rewriteSource?: { requestId: string; rewrites?: NarrativeRewrites }
}
export type ConversationItem = {
  request_id: string
  text: string
  status: 'running' | 'succeeded' | 'failed' | string
  result?: { message_id?: string; kind?: string; content?: string; world_effect?: boolean; elapsed_ticks?: string; references?: string[]; state_version?: string; message?: string; error?: string | Record<string, unknown>; rewrites?: NarrativeRewrites } | null
  created_at?: string
}
export type Conversation = { session_id: string; actor_id: string; items: ConversationItem[] }
export type AuthorRevisionPreview = {
  preview_id: string
  world_id: string
  branch_id: string
  expected_version: string
  entity_id: string
  component_name: string
  before: unknown
  after: unknown
  reason: string
  checkpoint_id: string
  checkpoint_sequence: string
  expires_at: string
  world_time_effect: boolean
}
export type AuthorRevisionResult = { command_id: string; state_version: string; duplicate: boolean; checkpoint_id: string; world_time_effect: boolean }
export type AuthorCharacterDraft = {
  branch_id: string
  expected_state_version: string
  name: string
  location_id: string
  public_role: string
  description: string
  values: string[]
  traits: string[]
  secret: string
  goal: string
  goal_destination_id?: string
  review_count?: number
  review_interval_days?: number
}
export type NpcReviewSchedule = { due_tick: string; max_runs: number; repeat_ticks: string | null }
export type NpcReviewDraft = Required<Pick<AuthorCharacterDraft, 'branch_id' | 'expected_state_version' | 'review_count' | 'review_interval_days'>> & { actor_id: string; goal_destination_id?: string; goal_description?: string }
export type NpcReviewPreview = { preview_id: string; expires_at: string; review_schedule: NpcReviewSchedule; goal?: { description: string; conditions: { target_id: string }[] } }
export type NpcRehearsalScenario = 'inducement' | 'threat' | 'family_conflict' | 'rebuttal' | 'scarcity' | 'absence'
export type NpcRehearsalRequest = {
  request_id: string
  branch_id: string
  actor_id: string
  expected_state_version: string
  scenario: NpcRehearsalScenario
}
export type NpcRehearsalReport = {
  scenario: NpcRehearsalScenario
  scenario_text: string
  state_version: string
  tick: string
  world_effect: false
  result: { choice: 'accept' | 'refuse' | 'negotiate' | 'defer'; reply: string; motivation: string; uncertainty: string }
  references: { id: string; kind: string; claim: string; confidence: string }[]
}
export type AuthorCharacterCardImport = Pick<AuthorCharacterDraft, 'branch_id' | 'expected_state_version' | 'location_id'> & {
  card_json: string
}
export type AuthorCharacterPngImport = Pick<AuthorCharacterDraft, 'branch_id' | 'expected_state_version' | 'location_id'>

export type AuthorCharacterPreview = {
  preview_id: string
  entity_id: string
  expected_version: string
  expires_at: string
  checkpoint_sequence: string
  draft: Omit<AuthorCharacterDraft, 'branch_id' | 'expected_state_version'>
  review_schedule?: NpcReviewSchedule
  import_report?: { card_version: '1' | '2.0' | '3.0'; mapped_fields: string[]; unmapped_fields: string[]; source_retained_on_commit?: boolean }
}
export type AuthorCharacterResult = { entity_id: string; command_id: string; state_version: string; duplicate: boolean }
export type AuthorPlaceRouteDraft = {
  to_place_id: string
  travel_ticks: number
  capacity?: number | null
  risk: string | number
  access_tags?: string[]
  travel_modes?: string[]
  is_open: boolean
}
export type AuthorRenewableHarvestRecipientDraft = {
  entity_id: string
  account_id: string
  per_period: string | number
}
export type AuthorRenewableHarvestDraft = {
  policy_id: string
  resource_id: string
  period_ticks: number
  maximum_total: string | number
  recipients: AuthorRenewableHarvestRecipientDraft[]
}
export type AuthorPlaceDraft = {
  branch_id: string
  expected_state_version: string
  name: string
  place_type: 'region' | 'settlement' | 'building' | 'wilderness' | 'site' | 'other'
  access_policy: 'open' | 'restricted' | 'private'
  region_id?: string | null
  parent_id?: string | null
  capacity?: number | null
  terrain?: string | null
  environment: Record<string, string | number | boolean>
  routes: AuthorPlaceRouteDraft[]
  renewable_harvests: AuthorRenewableHarvestDraft[]
  description: string
}
export type AuthorGovernanceDraft = {
  charter?: string
  members?: string[]
  leader_id?: string | null
  offices?: {
    office_id: string
    title: string
    holder_id?: string | null
    authority?: string[]
    term_end_tick?: number | string | null
  }[]
  succession_policy?: 'election' | 'appointment' | 'inheritance' | 'council' | 'none'
  decision_threshold?: string | number
  authority_tags?: string[]
  charter_clause_ids?: string[]
  membership_recruitment?: Record<string, unknown>[]
}
export type AuthorOrganizationDraft = {
  branch_id: string
  expected_state_version: string
  name: string
  kind: 'polity' | 'organization' | 'faction' | 'company' | 'government'
  location_id?: string | null
  public_role: string
  description: string
  governance?: AuthorGovernanceDraft | null
  treasury_resources: string[]
}
export type AuthorCreatedAccount = {
  account_id: string
  resource_id: string
  owner_id: string
  account_type: string
  balance: string
  credit_limit?: string
}
export type AuthorEntityPreview = {
  preview_id: string
  entity_id: string
  creation_kind: 'place' | 'organization'
  expected_version: string
  expires_at: string
  checkpoint_sequence: string
  draft: Omit<AuthorPlaceDraft | AuthorOrganizationDraft, 'branch_id' | 'expected_state_version'>
  entity: CharacterEntity
  created_accounts: AuthorCreatedAccount[]
  world_time_effect: false
}
export type AuthorEntityResult = {
  entity_id: string
  command_id: string
  state_version: string
  duplicate: boolean
  created_accounts: AuthorCreatedAccount[]
  world_time_effect: false
}

const apiBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') || '/api/v1'

async function request<T>(path: string, init: RequestInit = {}, retrySafeGet = true): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const url = `${apiBase}${path}`
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  headers.set('Accept', 'application/json')
  const response = await fetch(url, { ...init, headers, credentials: 'same-origin' })
  if (response.status === 401 && method === 'GET' && retrySafeGet && !path.startsWith('/session')) {
    await request<Session>('/session', { method: 'GET' }, false)
    return request<T>(path, init, false)
  }
  if (!response.ok) {
    let body: ApiErrorBody = {}
    try {
      const envelope = await response.json() as ApiErrorEnvelope
      body = envelope.error ?? envelope
    } catch { /* retain a readable status error */ }
    throw new ApiError(response.status, body)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const get = <T,>(path: string) => request<T>(path)
export type RestorePreview = {
  title: string
  branch_count: number
  event_count: number
  archive_sha256: string
  preview_id: string
  format_version?: string
  ruleset_version?: string
  pack_version?: string
  has_source_world_pack?: boolean
  ruleset_upgrades?: { target_version: string; preview_id: string; summary: string }[]
}
export function previewRestore(file: File): Promise<RestorePreview> {
  return request('/restores/preview', {
    method: 'POST', body: file, headers: { 'Content-Type': 'application/zip' },
  }, false)
}
export function confirmRestore(file: File, previewId: string, targetRulesetVersion?: string): Promise<{ data_directory: string; ruleset_version?: string }> {
  return request('/restores', {
    method: 'POST', body: file,
    headers: {
      'Content-Type': 'application/zip',
      'Preview-Id': previewId,
      ...(targetRulesetVersion ? { 'Target-Ruleset-Version': targetRulesetVersion } : {}),
    },
  }, false)
}
const post = <T,>(path: string, body: unknown, idempotencyKey?: string) => request<T>(path, {
  method: 'POST', body: JSON.stringify(body), headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
}, false)
const put = <T,>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }, false)

export const api = {
  session: () => get<Session>('/session'),
  listWorlds: () => get<{ items: World[] }>('/worlds'),
  listPacks: () => get<{ items: Pack[] }>('/packs'),
  previewWorldPack: (archive: File, signal: AbortSignal) => request<WorldPackPreview>('/packs/import/preview', {
    method: 'POST', body: archive, signal, headers: { 'Content-Type': 'application/zip' },
  }, false),
  importWorldPack: (archive: File, previewDigest: string, idempotencyKey: string, openingId?: string) => request<WorldPackImportResult>('/packs/import', {
    method: 'POST', body: archive,
    headers: {
      'Content-Type': 'application/zip',
      'X-World-Pack-Sha256': previewDigest,
      'Idempotency-Key': idempotencyKey,
      ...(openingId ? { 'X-Story-Opening': openingId } : {}),
    },
  }, false),
  createWorld: (body: {
    pack_id: string
    pack_version?: string
    title: string
    player_name: string
    seed?: number
    population_mode?: 'natural' | 'sustained'
    npc_autonomy_mode?: 'off' | 'limited_v1' | 'diverse_v1'
  }, idempotencyKey: string = crypto.randomUUID()) => post<World>('/worlds', body, idempotencyKey),
  worldView: (worldId: string, branchId?: string, mode: 'player' | 'author' = 'player') => {
    const query = new URLSearchParams({ mode })
    if (branchId) query.set('branch_id', branchId)
    return get<WorldView>(`/worlds/${encodeURIComponent(worldId)}/view?${query}`)
  },
  listBranches: (worldId: string) => get<{ items: Branch[] }>(`/worlds/${encodeURIComponent(worldId)}/branches`),
  playerContinuationOptions: (worldId: string, branchId: string) => get<PlayerContinuationOptions>(
    `/worlds/${encodeURIComponent(worldId)}/continuation-options?branch_id=${encodeURIComponent(branchId)}`,
  ),
  continueAfterDeath: (worldId: string, body: {
    branch_id: string
    previous_player_id: string
    new_player_id: string
    expected_state_version: string
  }, idempotencyKey: string = crypto.randomUUID()) => post<PlayerContinuationResult>(
    `/worlds/${encodeURIComponent(worldId)}/continue-after-death`, body, idempotencyKey,
  ),
  conversation: (worldId: string, branchId: string) => get<Conversation>(`/worlds/${encodeURIComponent(worldId)}/conversation?branch_id=${encodeURIComponent(branchId)}`),
  rewriteNarrative: (worldId: string, requestId: string, style: NarrativeRewriteStyle) => post<NarrativeRewriteResult>(
    `/worlds/${encodeURIComponent(worldId)}/narratives/${encodeURIComponent(requestId)}/rewrite`, { style },
  ),
  history: (worldId: string, branchId: string, after?: string) => {
    const query = new URLSearchParams({ branch_id: branchId, mode: 'auto' })
    if (after) query.set('after', after)
    return get<{ items: HistoryEvent[]; next_cursor: string | null; mode: 'author' | 'player'; has_source_world_pack?: boolean }>(`/worlds/${encodeURIComponent(worldId)}/history?${query}`)
  },
  previewTemplateUpdate: (worldId: string, branchId: string, archive: File, signal: AbortSignal) => request<TemplateUpdatePreview>(
    `/worlds/${encodeURIComponent(worldId)}/template-updates/preview?branch_id=${encodeURIComponent(branchId)}`,
    { method: 'POST', body: archive, signal, headers: { 'Content-Type': 'application/zip' } },
    false,
  ),
  applyTemplateUpdate: (worldId: string, body: { preview_id: string; label: string }) => post<TemplateUpdateResult>(
    `/worlds/${encodeURIComponent(worldId)}/template-updates`, body,
  ),
  createBranch: (worldId: string, body: { branch_id: string; sequence: string; label: string }, idempotencyKey: string) => post<Branch>(`/worlds/${encodeURIComponent(worldId)}/branches`, body, idempotencyKey),
  sendMessage: (worldId: string, body: MessageRequest) => post<MessageResponse>(`/worlds/${encodeURIComponent(worldId)}/messages`, body, body.request_id),
  performChallenge: (worldId: string, challengeId: string, body: {
    branch_id: string; actor_id: string; expected_state_version: string
  }, key: string) => post<{ passed: boolean; state_version: string }>(
    `/worlds/${encodeURIComponent(worldId)}/challenges/${encodeURIComponent(challengeId)}/attempt`, body, key,
  ),
  changeContest: (worldId: string, body: { branch_id: string; actor_id: string; expected_state_version: string; action: ContestAction }, key: string) =>
    post<ContestResult & { state_version: string }>(`/worlds/${encodeURIComponent(worldId)}/contests`, body, key),
  command: (commandId: string) => get<CommandStatus>(`/commands/${encodeURIComponent(commandId)}`),
  listProviders: () => get<{ items: Provider[] }>('/providers'),
  createProvider: (body: ProviderDraft) => post<Provider>('/providers', body, crypto.randomUUID()),
  updateProvider: (providerId: string, body: ProviderDraft) => put<Provider>(`/providers/${encodeURIComponent(providerId)}`, body),
  getProviderCredential: (providerId: string) => get<ProviderCredentialStatus>(`/providers/${encodeURIComponent(providerId)}/credential`),
  saveProviderCredential: (providerId: string, secret: string) => put<{ configured: true; secret_ref: string }>(`/providers/${encodeURIComponent(providerId)}/credential`, { secret }),
  deleteProviderCredential: (providerId: string) => request<void>(`/providers/${encodeURIComponent(providerId)}/credential`, { method: 'DELETE' }, false),
  probeProvider: (providerId: string) => request<{ status?: string; models?: ModelInfo[]; detail?: string }>(`/providers/${encodeURIComponent(providerId)}/probe`, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } }, false),
  listModels: (providerId: string) => get<{ items: ModelInfo[] }>(`/models?provider_id=${encodeURIComponent(providerId)}`),
  listCalls: () => get<{ items: CallRecord[] }>('/operations/calls'),
  getSettings: () => get<Settings>('/settings'),
  updateSettings: (body: Settings) => put<Settings>('/settings', body),
  validatePack: (draft: unknown) => post<{ valid: boolean; issues: { path: string; message: string; severity: string }[] }>('/packs/validate', draft, crypto.randomUUID()),
  createInterventionPreview: (worldId: string, patchProposal: unknown) => post<unknown>(`/worlds/${encodeURIComponent(worldId)}/interventions/preview`, { patch_proposal: patchProposal }, crypto.randomUUID()),
  createIntervention: (worldId: string, body: { preview_id: string; expected_version: string }) => post<unknown>(`/worlds/${encodeURIComponent(worldId)}/interventions`, body, crypto.randomUUID()),
  previewAuthorRevision: (worldId: string, body: { branch_id: string; patch_proposal: { entity_id: string; component_name: string; replacement: unknown; reason: string } }) => post<AuthorRevisionPreview>(`/worlds/${encodeURIComponent(worldId)}/interventions/preview`, body),
  commitAuthorRevision: (worldId: string, body: { preview_id: string; expected_version: string }) => post<AuthorRevisionResult>(`/worlds/${encodeURIComponent(worldId)}/interventions`, body),
  previewAuthorCharacter: (worldId: string, body: AuthorCharacterDraft) => post<AuthorCharacterPreview>(`/worlds/${encodeURIComponent(worldId)}/author/characters/preview`, body),
  previewNpcReview: (worldId: string, body: NpcReviewDraft) => post<NpcReviewPreview>(`/worlds/${encodeURIComponent(worldId)}/author/npc-reviews/preview`, body),
  commitNpcReview: (worldId: string, body: { preview_id: string }) => post<AuthorCharacterResult>(`/worlds/${encodeURIComponent(worldId)}/author/npc-reviews/commit`, body),
  rehearseNpc: (worldId: string, body: NpcRehearsalRequest) => post<NpcRehearsalReport>(`/worlds/${encodeURIComponent(worldId)}/author/npc-rehearsals`, body),
  previewAuthorCharacterCard: (worldId: string, body: AuthorCharacterCardImport) => post<AuthorCharacterPreview>(`/worlds/${encodeURIComponent(worldId)}/author/characters/import/preview`, body),
  previewAuthorCharacterPng: (worldId: string, file: Blob, query: AuthorCharacterPngImport) => {
    const params = new URLSearchParams(query)
    return request<AuthorCharacterPreview>(
      `/worlds/${encodeURIComponent(worldId)}/author/characters/import/png/preview?${params}`,
      { method: 'POST', body: file, headers: { 'Content-Type': 'image/png' } },
      false,
    )
  },
  commitAuthorCharacter: (worldId: string, body: { preview_id: string }) => post<AuthorCharacterResult>(`/worlds/${encodeURIComponent(worldId)}/author/characters/commit`, body),
  previewAuthorWorldBook: (worldId: string, body: WorldBookImportRequest) => post<WorldBookPreview>(`/worlds/${encodeURIComponent(worldId)}/author/world-books/preview`, body),
  commitAuthorWorldBook: (worldId: string, body: { preview_id: string; public_keys: string[] }) => post<WorldBookImportResult>(`/worlds/${encodeURIComponent(worldId)}/author/world-books/commit`, body),
  previewAuthorPlace: (worldId: string, body: AuthorPlaceDraft) => post<AuthorEntityPreview>(`/worlds/${encodeURIComponent(worldId)}/author/places/preview`, body),
  commitAuthorPlace: (worldId: string, body: { preview_id: string }) => post<AuthorEntityResult>(`/worlds/${encodeURIComponent(worldId)}/author/places/commit`, body),
  previewAuthorOrganization: (worldId: string, body: AuthorOrganizationDraft) => post<AuthorEntityPreview>(`/worlds/${encodeURIComponent(worldId)}/author/organizations/preview`, body),
  commitAuthorOrganization: (worldId: string, body: { preview_id: string }) => post<AuthorEntityResult>(`/worlds/${encodeURIComponent(worldId)}/author/organizations/commit`, body),
  recordAuthorKinshipLink: (worldId: string, body: {
    branch_id: string
    expected_state_version: string
    parent_id: string
    child_id: string
    relationship: AuthorKinshipLink['relationship']
  }, idempotencyKey: string) => post<AuthorKinshipResult>(`/worlds/${encodeURIComponent(worldId)}/author/kinship-links`, body, idempotencyKey),
  endAuthorKinshipLink: (worldId: string, linkId: string, body: {
    branch_id: string
    expected_state_version: string
    expected_created_tick: string
    expected_relationship?: AuthorKinshipLink['relationship']
  }, idempotencyKey: string) => post<AuthorKinshipResult>(`/worlds/${encodeURIComponent(worldId)}/author/kinship-links/${encodeURIComponent(linkId)}/end`, body, idempotencyKey),
  settleLegacyOccupationVacancies: (worldId: string, branchId: string, body: { command_id: string; expected_state_version: string }) => post<LegacyVacancySettlement>(
    `/worlds/${encodeURIComponent(worldId)}/branches/${encodeURIComponent(branchId)}/author/occupations/settle-legacy-vacancies`, body,
  ),
  cancelInvalidatedLeadershipBallot: (worldId: string, branchId: string, ballotId: string, body: {
    command_id: string
    expected_state_version: string
    expected_ballot_revision: number
  }) => post<InvalidatedLeadershipBallotCancellation>(
    `/worlds/${encodeURIComponent(worldId)}/branches/${encodeURIComponent(branchId)}/author/leadership-ballots/${encodeURIComponent(ballotId)}/cancel-invalidated`, body,
  ),
  startLongAction: (worldId: string, body: LongActionRequest, idempotencyKey: string) => post<LongActionAccepted>(`/worlds/${encodeURIComponent(worldId)}/long-actions`, body, idempotencyKey),
  startWorldAdvance: (worldId: string, body: WorldAdvanceRequest, idempotencyKey: string) => post<LongActionAccepted>(`/worlds/${encodeURIComponent(worldId)}/world-advance`, body, idempotencyKey),
  listJobs: (worldId: string, branchId: string) => get<{ items: JobStatusRecord[] }>(`/worlds/${encodeURIComponent(worldId)}/jobs?branch_id=${encodeURIComponent(branchId)}`),
  jobStatus: (jobId: string) => get<JobStatusRecord>(`/jobs/${encodeURIComponent(jobId)}`),
  controlJob: (jobId: string, action: 'pause' | 'resume' | 'cancel') => post<LongActionAccepted>(`/jobs/${encodeURIComponent(jobId)}/control`, { action }, crypto.randomUUID()),
  jobDecisions: (jobId: string) => get<{ items: JobDecision[] }>(`/jobs/${encodeURIComponent(jobId)}/decision`),
  answerJobDecision: (jobId: string, request_id: string, option_id: string) => post<LongActionAccepted>(`/jobs/${encodeURIComponent(jobId)}/decision`, { request_id, option_id }),
  extensions: () => get<{ items: MediaManifest[] }>('/extensions'),
  enqueueMediaJob: (worldId: string, branchId: string, body: MediaJobRequest) => post<JobStatusRecord>(
    `/worlds/${encodeURIComponent(worldId)}/branches/${encodeURIComponent(branchId)}/media-jobs`, body,
  ),
  mediaAssetUrl: (worldId: string, branchId: string, contentHash: string, actorId?: string) => {
    const query = actorId ? `?actor_id=${encodeURIComponent(actorId)}` : ''
    return `${apiBase}/worlds/${encodeURIComponent(worldId)}/branches/${encodeURIComponent(branchId)}/media/${encodeURIComponent(contentHash)}${query}`
  },
}
