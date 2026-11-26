import { useEffect, useMemo, useState } from 'react'
import type { HistoryEvent, JobStatusRecord, MediaJobRequest, MediaManifest, MediaRequestMetadata, MediaSubscription } from '../api/client'
import { api } from '../api/client'
import { errorMessage } from '../utils'
import { isTerminalMediaPollingError, mediaStatusRetryDelay, MEDIA_STATUS_RETRY_DELAYS_MS } from './mediaStatusPolling'
import './HistoryMediaControl.css'

type Props = {
  worldId: string
  branchId: string
  event: HistoryEvent
  manifests: MediaManifest[]
  initialJob?: JobStatusRecord
}

type MediaOption = { manifest: MediaManifest; subscription: MediaSubscription }
type Audience = MediaJobRequest['audience']

const ACTIVE_STATUSES = new Set(['queued', 'running'])
const AUDIO_TYPES = new Set(['audio/wav', 'audio/mpeg', 'audio/ogg'])
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])

type MediaPollingFailure = {
  message: string
  retryDelayMs?: number
  retryNumber?: number
  stopped: boolean
  terminal: boolean
}

export function HistoryMediaControl({ worldId, branchId, event, manifests, initialJob }: Props) {
  const options = useMemo<MediaOption[]>(() => manifests.flatMap((manifest) =>
    manifest.subscriptions
      .filter((subscription) => subscription.event_type === event.type)
      .map((subscription) => ({ manifest, subscription }))), [event.type, manifests])
  const subjects = Array.isArray(event.visibility?.subject_ids)
    ? Array.from(new Set(event.visibility.subject_ids.filter((subject): subject is string => typeof subject === 'string')))
    : []
  const audiences = permittedAudiences(event.visibility?.policy, subjects)
  const [expanded, setExpanded] = useState(false)
  const [optionKey, setOptionKey] = useState('')
  const [audience, setAudience] = useState<Audience>(audiences[0] ?? 'author_only')
  const [actorId, setActorId] = useState(subjects[0] ?? '')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [job, setJob] = useState<JobStatusRecord | null>(initialJob ?? null)
  const [jobRequest, setJobRequest] = useState<MediaRequestMetadata | null>(initialJob?.media_request ?? null)
  const [jobPollingFailure, setJobPollingFailure] = useState<MediaPollingFailure | null>(null)
  const [pollRetryEpoch, setPollRetryEpoch] = useState(0)

  const selectedOption = options.find((option) => optionKey === mediaOptionKey(option)) ?? options[0]
  const terminal = job && !ACTIVE_STATUSES.has(job.status)
  const asset = job?.status === 'succeeded' ? job.asset ?? null : null
  const validAsset = asset && jobRequest && asset.world_id === worldId && asset.branch_id === branchId
    && asset.source_event_ids.includes(event.event_id)
    && asset.visibility.policy === jobRequest.audience
    && (jobRequest.audience !== 'participants' || asset.visibility.subject_ids.includes(jobRequest.actor_id ?? ''))
    && asset.content_type && asset.content_hash
    && /^[0-9a-f]{64}$/.test(asset.content_hash)
    && (jobRequest.kind === 'speech'
      ? AUDIO_TYPES.has(asset.content_type)
      : IMAGE_TYPES.has(asset.content_type))
    ? asset : null
  const mediaUrl = validAsset
    ? api.mediaAssetUrl(worldId, branchId, validAsset.content_hash,
      jobRequest?.audience === 'participants' ? jobRequest.actor_id ?? undefined : undefined)
    : ''
  const selectedOptionKey = selectedOption ? mediaOptionKey(selectedOption) : ''
  const pollingJobId = job?.job_id
  const pollingJobStatus = job?.status
  useEffect(() => {
    if (!pollingJobId || !pollingJobStatus || !ACTIVE_STATUSES.has(pollingJobStatus)) return
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let retriesUsed = 0
    const poll = async () => {
      try {
        const latest = await api.jobStatus(pollingJobId)
        if (!active) return
        setJob(latest)
        setJobPollingFailure(null)
        retriesUsed = 0
        if (ACTIVE_STATUSES.has(latest.status)) timer = setTimeout(() => void poll(), 900)
      } catch (reason) {
        if (!active) return
        const delay = mediaStatusRetryDelay(reason, retriesUsed)
        const terminalError = isTerminalMediaPollingError(reason)
        if (delay === null) {
          setJobPollingFailure({ message: errorMessage(reason), stopped: true, terminal: terminalError })
          return
        }
        retriesUsed += 1
        setJobPollingFailure({
          message: errorMessage(reason), retryDelayMs: delay, retryNumber: retriesUsed,
          stopped: false, terminal: false,
        })
        timer = setTimeout(() => void poll(), delay)
      }
    }
    timer = setTimeout(() => void poll(), 500)
    return () => {
      active = false
      if (timer) clearTimeout(timer)
    }
  }, [pollingJobId, pollingJobStatus, worldId, branchId, event.event_id, pollRetryEpoch])

  async function enqueue() {
    if (!selectedOption || !audiences.includes(audience) || (audience === 'participants' && !actorId)) return
    const trimmedDescription = description.trim()
    if (!trimmedDescription || trimmedDescription.length > 24_000) return
    setBusy(true)
    setSubmitError('')
    setJobPollingFailure(null)
    setJob(null)
    const metadata: MediaRequestMetadata = {
      source_event_id: event.event_id,
      plugin_id: selectedOption.manifest.id,
      kind: selectedOption.subscription.kind,
      audience,
      ...(audience === 'participants' ? { actor_id: actorId } : { actor_id: null }),
    }
    const body: MediaJobRequest = {
      source_event_id: metadata.source_event_id,
      plugin_id: metadata.plugin_id,
      kind: metadata.kind,
      audience: metadata.audience,
      ...(audience === 'participants' ? { actor_id: actorId } : {}),
      description: trimmedDescription,
    }
    setJobRequest(metadata)
    try {
      const accepted = await api.enqueueMediaJob(worldId, branchId, body)
      setJob(accepted)
      setExpanded(false)
    } catch (reason) {
      setJobRequest(null)
      setSubmitError(errorMessage(reason))
    } finally {
      setBusy(false)
    }
  }

  if (!job && !options.length) return null
  if (!job && options.length > 0 && !audiences.length) return <div className="history-media-capability-note">此事件的记录范围无法授权媒体输出，未提交任务。</div>

  return <div className="history-media-control">
    {options.length > 0 && audiences.length > 0 && <button type="button" className="history-media-open" onClick={() => setExpanded((value) => !value)}
      aria-expanded={expanded} disabled={busy || Boolean(job && ACTIVE_STATUSES.has(job.status))}>
      {expanded ? '收起媒体制作' : '为此事件制作媒体'}
    </button>}
    {expanded && options.length > 0 && audiences.length > 0 && <form className="history-media-form" onSubmit={(event) => { event.preventDefault(); void enqueue() }}>
      <label>
        媒体插件与格式
        <select value={selectedOptionKey} onChange={(event) => setOptionKey(event.target.value)}>
          {options.map((option) => <option key={mediaOptionKey(option)} value={mediaOptionKey(option)}>
            {option.manifest.id} · {option.subscription.kind === 'speech' ? '音频' : '图片'}
          </option>)}
        </select>
      </label>
      <label>
        输出可见范围
        <select value={audience} onChange={(event) => setAudience(event.target.value as Audience)}>
          {audiences.map((item) => <option key={item} value={item}>{audienceLabel(item)}</option>)}
        </select>
        <small>范围受原事件的访问范围约束，不能在这里扩大。</small>
      </label>
      {audience === 'participants' && <label>
        接收角色
        <select value={actorId} onChange={(event) => setActorId(event.target.value)}>
          {subjects.map((subject) => <option key={subject} value={subject}>{subject}</option>)}
        </select>
      </label>}
      <label>
        作者描述
        <textarea value={description} maxLength={24_000} rows={4}
          onChange={(event) => setDescription(event.target.value)} required
          placeholder="描述希望呈现的场景、语气或画面。插件只会收到这段描述和已授权的媒体请求。" />
      </label>
      {submitError && <p className="history-media-error" role="alert">提交失败：{submitError}</p>}
      <div className="history-media-actions">
        <span>描述由作者提供；提交后不会修改世界状态。</span>
        <button type="submit" className="button button-primary button-small"
          disabled={busy || !description.trim() || (audience === 'participants' && !actorId)}>
          {busy ? '正在提交…' : '提交媒体任务'}
        </button>
      </div>
    </form>}
    {job && <div className="history-media-job" role="status" aria-live="polite">
      <div className="history-media-job-heading">
        <strong>{jobStatusLabel(job.status)}</strong><small>{job.job_id.slice(0, 8)}</small>
      </div>
      {jobPollingFailure && <>
        <p className="history-media-error">
          {jobPollingFailure.stopped
            ? jobPollingFailure.terminal ? '任务已不可访问或当前账户无权读取，自动轮询已停止。' : '连续读取失败，自动轮询已暂停。'
            : `暂时无法读取任务状态，将在 ${Math.ceil((jobPollingFailure.retryDelayMs ?? 0) / 1_000)} 秒后重试（${jobPollingFailure.retryNumber}/${MEDIA_STATUS_RETRY_DELAYS_MS.length}）。`}
          {' '}{jobPollingFailure.message}
        </p>
        {jobPollingFailure.stopped && <button type="button" className="history-media-dismiss" onClick={() => {
          setJobPollingFailure(null)
          setPollRetryEpoch((value) => value + 1)
        }}>手动重试读取状态</button>}
      </>}
      {job.status === 'failed' && <p>媒体插件未能完成此任务。世界事件保持不变。</p>}
      {job.status === 'cancelled' && <p>媒体任务已取消。</p>}
      {job.status === 'succeeded' && !validAsset && <p>任务已完成，但返回的媒体类型或摘要无效，未显示文件。</p>}
      {validAsset && jobRequest?.kind === 'speech' && <audio controls preload="none" src={mediaUrl} />}
      {validAsset && jobRequest?.kind === 'image' && <img src={mediaUrl} alt={`事件 ${event.sequence} 的生成图片`} />}
      {terminal && <button type="button" className="history-media-dismiss" onClick={() => { setJob(null); setJobRequest(null); setJobPollingFailure(null) }}>关闭结果</button>}
    </div>}
  </div>
}

function permittedAudiences(policy: string | undefined, subjects: string[]): Audience[] {
  if (policy === 'public') return ['public']
  if (policy === 'author_only') return ['author_only']
  if ((policy === 'private' || policy === 'subject_ids') && subjects.length > 0) return ['participants']
  return []
}

function mediaOptionKey(option: MediaOption) {
  return `${option.manifest.id}:${option.subscription.kind}`
}

function audienceLabel(audience: Audience) {
  if (audience === 'public') return '公开'
  if (audience === 'participants') return '指定参与角色'
  return '仅作者与编辑'
}

function jobStatusLabel(status: string) {
  if (status === 'queued' || status === 'running') return '正在生成媒体…'
  if (status === 'succeeded') return '媒体已生成'
  if (status === 'cancelled') return '已取消'
  if (status === 'failed') return '生成失败'
  return '任务状态未知'
}
