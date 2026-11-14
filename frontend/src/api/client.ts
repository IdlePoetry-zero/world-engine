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
export type World = { world_id: string; title: string; active_branch_id: string | null; current_tick: string; state_version: string; status: string; ruleset_version?: string }
export type Pack = { pack_id: string; title: string; version: string; description: string }
export type Branch = { branch_id: string; label: string; current_tick: string; state_version: string }
export type CharacterEntity = { id: string; kind: string; name?: string | null; location_id?: string; status?: string; components?: Record<string, unknown> }
export type ResourceAccount = { resource_id: string; balance: string }
export type HistoryEvent = { event_id: string; type: string; tick: string; payload: unknown; sequence: string }
export type WorldState = {
  entities: Record<string, CharacterEntity>
  player_id?: string
  calendar?: { name?: string; calendar_id?: string; [key: string]: unknown }
  metadata?: Record<string, unknown>
  resources: Record<string, ResourceAccount>
  relations: unknown[]
  knowledge: unknown[]
  goals: unknown[]
  scheduled_events?: unknown[]
}
export type WorldView = {
  world: World & { [key: string]: unknown }
  branch: Branch
  state: WorldState
  last_seq: string
  state_hash: string
}
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
export type MessageResponse = { message_id: string; kind: string; content?: string; command_id?: string; job_id?: string; world_effect: boolean; committed_tick?: string }
export type CommandStatus = { status: 'queued' | 'running' | 'waiting_input' | 'succeeded' | 'failed' | 'cancelled' | string; result?: string | { content?: string; kind?: string; world_effect?: boolean }; error?: ApiErrorBody; committed_through_tick?: string }
export type Provider = { provider_id: string; kind: 'deepseek' | 'openai_compatible' | 'ollama' | 'anthropic'; name: string; base_url: string; secret_ref?: string; enabled: boolean; default_model?: string }
export type ProviderDraft = Omit<Provider, 'provider_id'>
export type ModelInfo = { id: string; name?: string; provider_id?: string; capability_evidence?: Record<string, unknown> | null; context_length?: number }
export type Settings = {
  speed: 'fast' | 'balanced' | 'patient'
  depth: 'standard' | 'deep' | 'exhaustive'
  routes: Record<string, { provider_id?: string; model?: string }>
  local_only: boolean
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
  status: string
  trace?: Record<string, unknown>
}
export type GameMessage = { id: string; role: 'player' | 'gm' | 'system' | 'error'; content: string; source: 'local' | 'api'; kind?: string }

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
const post = <T,>(path: string, body: unknown, idempotencyKey?: string) => request<T>(path, {
  method: 'POST', body: JSON.stringify(body), headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
}, false)
const put = <T,>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }, false)

export const api = {
  session: () => get<Session>('/session'),
  listWorlds: () => get<{ items: World[] }>('/worlds'),
  listPacks: () => get<{ items: Pack[] }>('/packs'),
  createWorld: (body: { pack_id: string; title: string; player_name: string }, idempotencyKey: string = crypto.randomUUID()) => post<World>('/worlds', body, idempotencyKey),
  worldView: (worldId: string, branchId?: string, mode: 'player' | 'author' = 'player') => {
    const query = new URLSearchParams({ mode })
    if (branchId) query.set('branch_id', branchId)
    return get<WorldView>(`/worlds/${encodeURIComponent(worldId)}/view?${query}`)
  },
  listBranches: (worldId: string) => get<{ items: Branch[] }>(`/worlds/${encodeURIComponent(worldId)}/branches`),
  history: (worldId: string, branchId: string, after?: string) => {
    const query = new URLSearchParams({ branch_id: branchId })
    if (after) query.set('after', after)
    return get<{ items: HistoryEvent[]; next_cursor?: string }>(`/worlds/${encodeURIComponent(worldId)}/history?${query}`)
  },
  createBranch: (worldId: string, body: { checkpoint: string; label: string }, idempotencyKey: string = crypto.randomUUID()) => post<Branch>(`/worlds/${encodeURIComponent(worldId)}/branches`, body, idempotencyKey),
  sendMessage: (worldId: string, body: MessageRequest) => post<MessageResponse>(`/worlds/${encodeURIComponent(worldId)}/messages`, body, body.request_id),
  command: (commandId: string) => get<CommandStatus>(`/commands/${encodeURIComponent(commandId)}`),
  listProviders: () => get<{ items: Provider[] }>('/providers'),
  createProvider: (body: ProviderDraft) => post<Provider>('/providers', body, crypto.randomUUID()),
  updateProvider: (providerId: string, body: ProviderDraft) => put<Provider>(`/providers/${encodeURIComponent(providerId)}`, body),
  probeProvider: (providerId: string) => request<{ status?: string; models?: ModelInfo[]; detail?: string }>(`/providers/${encodeURIComponent(providerId)}/probe`, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() } }, false),
  listModels: (providerId: string) => get<{ items: ModelInfo[] }>(`/models?provider_id=${encodeURIComponent(providerId)}`),
  listCalls: () => get<{ items: CallRecord[] }>('/operations/calls'),
  getSettings: () => get<Settings>('/settings'),
  updateSettings: (body: Settings) => put<Settings>('/settings', body),
  validatePack: (draft: unknown) => post<{ valid: boolean; issues: { path: string; message: string; severity: string }[] }>('/packs/validate', draft, crypto.randomUUID()),
  createInterventionPreview: (worldId: string, patchProposal: unknown) => post<unknown>(`/worlds/${encodeURIComponent(worldId)}/interventions/preview`, { patch_proposal: patchProposal }, crypto.randomUUID()),
  createIntervention: (worldId: string, body: { preview_id: string; expected_version: string }) => post<unknown>(`/worlds/${encodeURIComponent(worldId)}/interventions`, body, crypto.randomUUID()),
  controlJob: (jobId: string, action: 'pause' | 'resume' | 'cancel') => post<{ status: string }>(`/jobs/${encodeURIComponent(jobId)}/control`, { action }, crypto.randomUUID()),
  extensions: () => get<{ items: unknown[] }>('/extensions'),
}
