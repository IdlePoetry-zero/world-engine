import { useRef, useState, type ChangeEvent } from 'react'
import {
  ApiError,
  exportAuthorWorldPack,
  previewAuthorWorldPack,
  type WorldPackAuthoringDraft,
  type WorldPackAuthoringPreview,
} from '../api/client'
import { errorMessage } from '../utils'
import { Icon } from './Icon'
import './WorldPackAuthoringPanel.css'

const SAMPLE_SOURCE = JSON.stringify({
  manifest: {
    schema_version: '1.0',
    pack_id: 'my_world',
    version: '0.1.0',
    engine_range: '*',
    title: '我的世界',
    dependencies: [],
    license: 'UNLICENSED',
  },
  template: {
    base_pack_id: 'modern_city',
    population_mode: 'natural',
  },
}, null, 2)

type ParsedSource = { draft: WorldPackAuthoringDraft | null; error: string | null }
type FieldDiagnostic = { path: string; message: string }

export function WorldPackAuthoringPanel() {
  const [source, setSource] = useState(SAMPLE_SOURCE)
  const [sourceFileName, setSourceFileName] = useState('')
  const [preview, setPreview] = useState<WorldPackAuthoringPreview | null>(null)
  const [failure, setFailure] = useState<{ message: string; diagnostics: FieldDiagnostic[] } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [readingSource, setReadingSource] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const parsed = parseAuthoringSource(source)
  const archiveVerified = Boolean(preview?.can_export && preview.roundtrip_verified && isSha256(preview.archive_sha256))
  const busy = previewing || exporting || readingSource

  function changeSource(value: string) {
    setSource(value)
    setPreview(null)
    setFailure(null)
  }

  async function selectSourceFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0]
    if (!file) return
    setPreview(null)
    setFailure(null)
    setReadingSource(true)
    try {
      changeSource(await file.text())
      setSourceFileName(file.name)
    } catch {
      setFailure({ message: '无法读取所选 JSON 文件。', diagnostics: [] })
      setSourceFileName('')
    } finally {
      if (fileInput.current) fileInput.current.value = ''
      setReadingSource(false)
    }
  }

  function loadSample() {
    changeSource(SAMPLE_SOURCE)
    setSourceFileName('')
    if (fileInput.current) fileInput.current.value = ''
  }

  async function makePreview() {
    if (!parsed.draft || busy) return
    setPreview(null)
    setFailure(null)
    setPreviewing(true)
    try {
      setPreview(await previewAuthorWorldPack(parsed.draft))
    } catch (reason) {
      setFailure({ message: errorMessage(reason), diagnostics: fieldDiagnostics(reason) })
    } finally {
      setPreviewing(false)
    }
  }

  async function downloadArchive() {
    if (!parsed.draft || !preview || !archiveVerified || busy) return
    setFailure(null)
    setExporting(true)
    try {
      const archive = await exportAuthorWorldPack({
        ...parsed.draft,
        expected_archive_sha256: preview.archive_sha256,
      })
      const actualDigest = await sha256(archive)
      if (actualDigest !== preview.archive_sha256.toLowerCase()) {
        setPreview(null)
        throw new Error('下载文件摘要与已验证预览不一致，文件未保存。请重新预览。')
      }
      saveBlob(archive, archiveFilename(preview.manifest))
    } catch (reason) {
      if (reason instanceof ApiError && reason.code?.includes('PREVIEW_MISMATCH')) setPreview(null)
      setFailure({ message: errorMessage(reason), diagnostics: fieldDiagnostics(reason) })
    } finally {
      setExporting(false)
    }
  }

  return <section className="author-revision-panel world-pack-authoring" aria-labelledby="world-pack-authoring-heading">
    <div className="author-revision-heading">
      <div>
        <h3 id="world-pack-authoring-heading">制作世界包</h3>
        <p>编辑清单与模板 JSON。预览会校验并回读归档；下载时再次核对同一摘要。</p>
      </div>
      <Icon name="archive" size={18} />
    </div>

    <div className="world-pack-authoring-notice">
      <strong>来源由你选择</strong>
      <p>只使用此编辑器中的 JSON；不会把当前分支的存档事实自动打包。下载后可在「世界库 → 导入世界包」预览并导入为新世界。</p>
    </div>

    <div className="world-pack-authoring-source-actions">
      <label className="author-simple-field" htmlFor="world-pack-authoring-file">
        <span>选择 JSON 文件 <small>manifest 与 template 的组合对象</small></span>
        <input id="world-pack-authoring-file" ref={fileInput} type="file" accept=".json,application/json" disabled={busy}
          onChange={(event) => void selectSourceFile(event)} />
      </label>
      <button className="button button-secondary button-small" type="button" onClick={loadSample} disabled={busy}>载入最小示例</button>
    </div>
    {sourceFileName && <p className="world-pack-authoring-file-name" role="status">正在编辑：{sourceFileName}</p>}

    <label className="author-simple-field world-pack-authoring-editor" htmlFor="world-pack-authoring-source">
      <span>世界包 JSON 来源</span>
      <textarea id="world-pack-authoring-source" className="author-json-input" rows={22} spellCheck={false}
        aria-invalid={Boolean(parsed.error)} aria-describedby="world-pack-authoring-help world-pack-authoring-json-status"
        value={source} disabled={busy} onChange={(event) => changeSource(event.currentTarget.value)} />
    </label>
    <p id="world-pack-authoring-help" className="author-revision-help">
      外层对象包含 manifest 与 template。示例模板基于 modern_city 背景；完整结构由服务端按世界包版本规则校验。
    </p>
    <p id="world-pack-authoring-json-status" className={parsed.error ? 'world-pack-authoring-json-error' : 'world-pack-authoring-json-ok'}
      role={parsed.error ? 'alert' : 'status'} aria-live={parsed.error ? 'assertive' : 'polite'}>
      {parsed.error ?? 'JSON 格式有效；字段、背景版本与引用尚待服务端验证。'}
    </p>

    <div className="character-create-actions world-pack-authoring-actions">
      <button className="button button-secondary" type="button" disabled={!parsed.draft || busy} onClick={() => void makePreview()}>
        {previewing ? '正在校验并回读…' : '预览并验证'}
      </button>
      <button className="button button-primary" type="button" disabled={!archiveVerified || busy} onClick={() => void downloadArchive()}>
        {exporting ? '正在下载并校验…' : '下载已验证 ZIP'}
      </button>
    </div>

    {failure && <div className="character-create-error world-pack-authoring-error" role="alert">
      <p>{failure.message}</p>
      {failure.diagnostics.length > 0 && <ul>{failure.diagnostics.map((item, index) => <li key={`${item.path}:${index}`}>
        <code>{item.path}</code>{item.message && `：${item.message}`}
      </li>)}</ul>}
    </div>}

    {preview && <WorldPackAuthoringReview preview={preview} />}
  </section>
}

function WorldPackAuthoringReview({ preview }: { preview: WorldPackAuthoringPreview }) {
  const canDownload = preview.can_export && preview.roundtrip_verified && isSha256(preview.archive_sha256)
  return <div className="author-revision-preview world-pack-authoring-review" aria-live="polite">
    <div className="author-preview-heading">
      <h4>{stringValue(preview.manifest.title) || '世界包预览'}</h4>
      <span className="version-chip">{canDownload ? '回读验证通过' : '尚不可下载'}</span>
    </div>
    {!preview.can_export && <p className="world-pack-authoring-error" role="alert">服务端未通过导出校验。请查看详细报告并修正来源后重新预览。</p>}
    {!preview.roundtrip_verified && <p className="world-pack-authoring-error" role="alert">归档未完成回读验证，不能下载。</p>}
    {preview.can_export && preview.roundtrip_verified && !isSha256(preview.archive_sha256) && <p className="world-pack-authoring-error" role="alert">服务端没有返回有效的 SHA-256，不能下载。</p>}
    <dl className="world-pack-authoring-summary">
      <div><dt>包标识</dt><dd>{stringValue(preview.manifest.pack_id) || '未提供'}</dd></div>
      <div><dt>版本</dt><dd>{stringValue(preview.manifest.version) || '未提供'}</dd></div>
      <div><dt>开场数</dt><dd>{preview.opening_count}</dd></div>
      <div><dt>资源数</dt><dd>{preview.asset_count} · {formatBytes(preview.asset_bytes)}</dd></div>
    </dl>
    <div className="world-pack-authoring-digest"><span>已验证归档 SHA-256</span><code>{preview.archive_sha256}</code></div>
    <details className="world-pack-authoring-report">
      <summary>查看清单与兼容报告</summary>
      <div className="world-pack-authoring-report-grid">
        <div><h5>已规范化的清单</h5><pre>{formatJson(preview.manifest)}</pre></div>
        <div><h5>兼容性与保留字段</h5><pre>{formatJson(preview.compatibility)}</pre></div>
      </div>
    </details>
  </div>
}

function parseAuthoringSource(source: string): ParsedSource {
  let value: unknown
  try {
    value = JSON.parse(source)
  } catch (reason) {
    return { draft: null, error: `JSON 语法错误：${reason instanceof Error ? reason.message : '无法解析'}` }
  }
  if (!isRecord(value)) return { draft: null, error: 'JSON 根节点必须是包含 manifest 与 template 的对象。' }
  if (!isRecord(value.manifest)) return { draft: null, error: 'manifest 必须是 JSON 对象。' }
  if (!isRecord(value.template)) return { draft: null, error: 'template 必须是 JSON 对象。' }
  return { draft: { manifest: value.manifest, template: value.template }, error: null }
}

function fieldDiagnostics(reason: unknown): FieldDiagnostic[] {
  if (!(reason instanceof ApiError) || !reason.field_errors) return []
  if (Array.isArray(reason.field_errors)) return reason.field_errors.map((item) => ({
    path: item.path,
    message: item.message ?? item.type,
  }))
  return Object.entries(reason.field_errors).map(([path, message]) => ({ path, message }))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{64}$/i.test(value)
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function formatJson(value: unknown): string {
  try { return JSON.stringify(value, null, 2) ?? '未提供' } catch { return '报告无法显示。' }
}

function formatBytes(value: number): string {
  return value < 1024 ? `${value} B` : `${(value / 1024).toLocaleString('zh-HK', { maximumFractionDigits: 1 })} KiB`
}

function archiveFilename(manifest: Record<string, unknown>): string {
  const part = (value: unknown, fallback: string) => typeof value === 'string' && value.trim()
    ? value.trim().replace(/[^a-zA-Z0-9._-]+/g, '-')
    : fallback
  return `${part(manifest.pack_id, 'world-pack')}-${part(manifest.version, 'verified')}.zip`
}

async function sha256(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}
