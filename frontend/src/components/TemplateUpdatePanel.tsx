import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, type TemplateUpdateChange, type TemplateUpdatePreview, type TemplateUpdateResult } from '../api/client'
import { errorMessage } from '../utils'
import './TemplateUpdatePanel.css'

const MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
const MAX_BRANCH_LABEL_LENGTH = 160
const changeStatus: Record<TemplateUpdateChange['status'], string> = {
  ready: '计划应用',
  already_applied: '已应用',
  conflict: '存在冲突',
  unsupported: '不支持',
}

type Submission = { preview_id: string; label: string }
type Receipt = { result: TemplateUpdateResult; label: string }

export function TemplateUpdatePanel({ worldId, branchId, branchLabel, onBranchCreated }: {
  worldId: string
  branchId: string
  branchLabel: string
  onBranchCreated: (worldId: string, branchId: string, label: string, sourceBranchId: string) => Promise<void>
}) {
  const [archive, setArchive] = useState<File | null>(null)
  const [preview, setPreview] = useState<TemplateUpdatePreview | null>(null)
  const [label, setLabel] = useState('')
  const [submission, setSubmission] = useState<Submission | null>(null)
  const [receipt, setReceipt] = useState<Receipt | null>(null)
  const [error, setError] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const previewRequest = useRef<AbortController | null>(null)
  const applyingRef = useRef(false)
  const mounted = useRef(true)
  const fileInput = useRef<HTMLInputElement>(null)
  const canApply = preview?.can_apply && preview.changes.some((item) => item.status === 'ready')

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      previewRequest.current?.abort()
    }
  }, [])

  function selectArchive(file: File | undefined) {
    if (applyingRef.current || submission || receipt) return
    previewRequest.current?.abort()
    previewRequest.current = null
    setPreviewing(false)
    setPreview(null)
    const problem = file?.size === 0 ? '请选择非空的 ZIP 文件。'
      : file && file.size > MAX_ARCHIVE_BYTES ? '世界包 ZIP 不能超过 16 MiB。'
      : ''
    setError(problem)
    setArchive(problem ? null : file ?? null)
    if (problem && fileInput.current) fileInput.current.value = ''
  }

  async function requestPreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!archive || previewRequest.current || applyingRef.current || submission || receipt) return
    const request = new AbortController()
    previewRequest.current = request
    setPreviewing(true)
    setPreview(null)
    setError('')
    try {
      const result = await api.previewTemplateUpdate(worldId, branchId, archive, request.signal)
      if (!request.signal.aborted) {
        setPreview(result)
        setLabel(`模板更新 ${result.target_version}`.slice(0, MAX_BRANCH_LABEL_LENGTH))
      }
    } catch (reason) {
      if (!request.signal.aborted) setError(errorMessage(reason))
    } finally {
      if (previewRequest.current === request && !request.signal.aborted) {
        previewRequest.current = null
        setPreviewing(false)
      }
    }
  }

  async function openCreatedBranch(created: Receipt) {
    try {
      await onBranchCreated(worldId, created.result.branch_id, created.label, branchId)
    } catch (reason) {
      if (mounted.current) setError(`更新分支「${created.label}」已创建。刷新分支列表失败。${errorMessage(reason)}`)
    }
  }

  async function confirmUpdate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmedLabel = label.trim()
    if (!canApply || !preview || !trimmedLabel || trimmedLabel.length > MAX_BRANCH_LABEL_LENGTH || applyingRef.current || receipt) return
    const attempt = submission ?? { preview_id: preview.preview_id, label: trimmedLabel }
    applyingRef.current = true
    setApplying(true)
    setSubmission(attempt)
    setError('')
    try {
      const result = await api.applyTemplateUpdate(worldId, attempt)
      if (!mounted.current) return
      const created = { result, label: attempt.label }
      setReceipt(created)
      await openCreatedBranch(created)
    } catch (reason) {
      if (!mounted.current) return
      if (reason instanceof ApiError && (reason.code === 'TEMPLATE_SOURCE_CHANGED' || reason.code === 'TEMPLATE_PREVIEW_EXPIRED')) {
        const message = reason.code === 'TEMPLATE_SOURCE_CHANGED' ? '源分支已有新的提交。' : '更新预览已过期。'
        setPreview(null)
        setSubmission(null)
        setError(`${message}请重新预览所选世界包。`)
      } else {
        setError(errorMessage(reason))
        if (reason instanceof ApiError && reason.status < 500 && !reason.retryable) setSubmission(null)
      }
    } finally {
      applyingRef.current = false
      if (mounted.current) setApplying(false)
    }
  }

  async function retryOpen() {
    if (!receipt || applyingRef.current) return
    applyingRef.current = true
    setApplying(true)
    setError('')
    try {
      await openCreatedBranch(receipt)
    } finally {
      applyingRef.current = false
      if (mounted.current) setApplying(false)
    }
  }

  return <details className="author-revision-panel template-update-panel">
    <summary>更新世界模板</summary>
    <p>选择新版世界包，预览它对「{branchLabel}」的影响。确认后会从预览指定的检查点复制到新分支，并在新分支应用更新。</p>
    <form className="world-book-import-form" onSubmit={(event) => void requestPreview(event)}>
      <label className="author-simple-field" htmlFor="template-update-archive">
        <span>新版世界包 ZIP <small>最多 16 MiB</small></span>
        <input id="template-update-archive" ref={fileInput} type="file" accept=".zip,application/zip"
          disabled={applying || Boolean(submission || receipt)} onChange={(event) => selectArchive(event.currentTarget.files?.[0])} />
      </label>
      <div className="character-create-actions">
        <button className="button button-secondary" type="submit" disabled={!archive || previewing || applying || Boolean(submission || receipt)}>
          {previewing ? '正在预览更新…' : '预览模板更新'}
        </button>
      </div>
    </form>
    <p className="template-update-status" role="status">{previewing ? '正在校验世界包并比较检查点…' : receipt ? `更新分支「${receipt.label}」已创建。` : ''}</p>
    {preview && <div className="author-revision-preview">
      <div className="author-preview-heading"><h4>模板更新预览</h4></div>
      <dl className="character-preview-fields">
        <div className="character-preview-field"><dt>源模板版本</dt><dd>{preview.source_version}</dd></div>
        <div className="character-preview-field"><dt>目标模板版本</dt><dd>{preview.target_version}</dd></div>
        <div className="character-preview-field"><dt>复制起点</dt><dd>「{branchLabel}」的检查点 #{preview.base_sequence}</dd></div>
        <div className="character-preview-field"><dt>检查点状态版本</dt><dd>{preview.base_version}</dd></div>
      </dl>
      {preview.changes.length > 0 && <ul className="template-update-changes">
        {preview.changes.map((item, index) => <li key={`${item.entity_id}:${item.component}:${index}`} data-status={item.status}>
          <strong>{changeStatus[item.status]}</strong> · {item.entity_name || item.entity_id} · {item.component}
          {Boolean(item.fields?.length) && <p>字段 {item.fields?.join('、')}</p>}
          {item.message && <p>{item.message}</p>}
        </li>)}
      </ul>}
      {!canApply && <p className="author-revision-inline-warning">{preview.changes.some(({ status }) => status === 'conflict' || status === 'unsupported')
        ? '当前预览无法应用。请查看各项更新的状态。' : '该世界包没有需要应用的模板改动。'}</p>}
      {canApply && !receipt && <form className="world-book-import-form" onSubmit={(event) => void confirmUpdate(event)}>
        <label className="author-simple-field" htmlFor="template-update-branch-label">
          <span>新分支名称</span>
          <input id="template-update-branch-label" value={label} maxLength={MAX_BRANCH_LABEL_LENGTH} required
            disabled={applying || Boolean(submission)} onChange={(event) => setLabel(event.currentTarget.value)} />
        </label>
        <div className="character-preview-actions">
          <button className="button button-primary" type="submit" disabled={applying || !label.trim()}>
            {applying ? '正在创建更新分支…' : submission ? '使用相同预览重试' : '确认将更新应用到新分支'}
          </button>
        </div>
      </form>}
    </div>}
    {error && <p className="character-create-error" role="alert">{error}</p>}
    {receipt && error && <button className="button button-secondary" type="button" disabled={applying} onClick={() => void retryOpen()}>
      重新读取分支并打开
    </button>}
  </details>
}
