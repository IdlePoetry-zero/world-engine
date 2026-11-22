import { useState } from 'react'
import type { FormEvent } from 'react'

import { api, ApiError, type WorldBookEntry, type WorldBookPreview, type WorldBookPreviewEntry } from '../api/client'
import type { Notice } from '../App'
import { errorMessage } from '../utils'
import { Icon } from './Icon'

const MAX_WORLD_BOOK_BYTES = 256 * 1024
const MAX_PUBLIC_WORLD_BOOK_KEYS = 128

export function WorldBookImport({
  worldId,
  branchId,
  stateVersion,
  entries,
  onNotice,
  onCommitted,
}: {
  worldId: string
  branchId: string
  stateVersion: string
  entries?: WorldBookEntry[] | null
  onNotice: (notice: Notice) => void
  onCommitted: () => void
}) {
  const [title, setTitle] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<WorldBookPreview | null>(null)
  const [publicKeys, setPublicKeys] = useState<Set<string>>(() => new Set())
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [refreshRequired, setRefreshRequired] = useState(false)

  const existingEntries = Array.isArray(entries) ? entries : null
  const canPreview = Boolean(title.trim() && title.trim().length <= 120 && file && !busy && !refreshRequired)

  const selectFile = (nextFile: File | undefined) => {
    setError(null)
    setRefreshRequired(false)
    if (!nextFile) {
      setFile(null)
      return
    }
    if (nextFile.size === 0) {
      setFile(null)
      setError('世界书 JSON 文件不能为空。')
      return
    }
    const isJson = nextFile.type.toLowerCase() === 'application/json' || nextFile.name.toLowerCase().endsWith('.json')
    if (!isJson) {
      setFile(null)
      setError('请选择 JSON 格式的世界书文件。')
      return
    }
    if (nextFile.size > MAX_WORLD_BOOK_BYTES) {
      setFile(null)
      setError('世界书 JSON 不能超过 256 KiB。')
      return
    }
    setFile(nextFile)
  }

  const makePreview = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canPreview || !file) return
    setBusy(true)
    setError(null)
    setRefreshRequired(false)
    try {
      let bookJson: string
      try {
        bookJson = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer())
      } catch {
        setError('无法读取世界书文件。请选择 UTF-8 编码的 JSON。')
        return
      }
      const result = await api.previewAuthorWorldBook(worldId, {
        branch_id: branchId,
        expected_state_version: stateVersion,
        title: title.trim(),
        book_json: bookJson,
      })
      setPreview(result)
      setPublicKeys(new Set())
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally {
      setBusy(false)
    }
  }

  const setEntryPublic = (sourceKey: string, isPublic: boolean) => {
    setPublicKeys((current) => {
      const next = new Set(current)
      if (isPublic) next.add(sourceKey)
      else next.delete(sourceKey)
      return next
    })
  }

  const clearPreview = () => {
    setPreview(null)
    setPublicKeys(new Set())
    setError(null)
    setRefreshRequired(false)
  }

  const commit = async () => {
    if (!preview || busy || refreshRequired || preview.entries.length === 0) return
    setBusy(true)
    setError(null)
    const previewKeys = new Set(preview.entries.map((entry) => entry.source_key))
    const reviewedPublicKeys = Array.from(publicKeys).filter((sourceKey) => previewKeys.has(sourceKey))
    try {
      const result = await api.commitAuthorWorldBook(worldId, {
        preview_id: preview.preview_id,
        public_keys: reviewedPublicKeys,
      })
      onNotice({
        kind: 'success',
        text: `已保存 ${result.entry_count} 条世界书条目；${reviewedPublicKeys.length} 条可进入玩家 GM 上下文，其余仅作者可见。`,
      })
      setPreview(null)
      setPublicKeys(new Set())
      setFile(null)
      setTitle('')
      onCommitted()
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally {
      setBusy(false)
    }
  }

  return <section className="author-revision-panel" aria-labelledby="world-book-heading">
    <div className="author-revision-heading">
      <div>
        <span className="panel-overline">WORLD INFO</span>
        <h3 id="world-book-heading">世界书</h3>
        <p>审阅独立 JSON 文件后导入。符合格式的条目会保存，默认仅作者可见；你可以逐条允许其进入玩家 GM 上下文。</p>
      </div>
      <Icon name="book" size={18} />
    </div>

    <div className="world-book-current">
      <div className="entity-components-heading">
        <div><span className="panel-overline">CURRENT BRANCH</span><h3>当前分支条目</h3></div>
        {existingEntries && <span>{existingEntries.length} 条</span>}
      </div>
      {existingEntries === null
        ? <p className="author-revision-help">作者投影未提供现有世界书条目列表。</p>
        : existingEntries.length === 0
          ? <p className="author-revision-help">当前分支尚无世界书条目。</p>
          : <details className="world-book-saved-list">
            <summary>查看已保存条目（公开可用 {existingEntries.filter((entry) => entry.visibility === 'public').length} 条）</summary>
            <div className="world-book-entry-list">
              {existingEntries.map((entry) => <WorldBookEntryCard key={entry.entry_id} entry={entry} />)}
            </div>
          </details>}
    </div>

    {!preview ? <form className="world-book-import-form" onSubmit={(event) => void makePreview(event)}>
      <label className="author-simple-field" htmlFor="world-book-title">
        <span>导入批次名称 <small>最多 120 字</small></span>
        <input id="world-book-title" value={title} maxLength={120} onChange={(event) => { setTitle(event.target.value); setError(null) }} required />
      </label>
      <label className="author-simple-field" htmlFor="world-book-file">
        <span>世界书 JSON <small>UTF-8，最多 256 KiB</small></span>
        <input id="world-book-file" type="file" accept=".json,application/json" disabled={busy || refreshRequired} onChange={(event) => {
          const selected = event.currentTarget.files?.[0]
          event.currentTarget.value = ''
          selectFile(selected)
        }} />
      </label>
      {file && <p className="author-revision-help">已选择：{file.name} · {file.size.toLocaleString('zh-HK')} 字节</p>}
      <p className="author-revision-help">玩家上下文只按审核过的字面关键词与常驻标记匹配；来源扩展字段和设置会随作者备份保存。导入内容按文本处理，不执行脚本或 HTML。</p>
      {error && <p className="character-create-error" role="alert">{error}</p>}
      {refreshRequired && <div className="character-conflict" role="alert"><span>分支版本已变化，请重新读取作者投影后再预览。</span><button type="button" className="text-button" onClick={onCommitted} disabled={busy}>重新读取作者投影</button></div>}
      <div className="character-create-actions">
        <span>分支版本 {stateVersion} · 预览前不会写入世界</span>
        <button className="button button-primary button-small" type="submit" disabled={!canPreview}>{busy ? <><Icon name="spinner" size={13} />正在生成预览…</> : <>生成预览 <Icon name="arrow-right" size={13} /></>}</button>
      </div>
    </form> : <div className="world-book-review">
      <div className="author-revision-preview">
        <div className="author-preview-heading">
          <div><span className="panel-overline">SERVER REVIEW · {preview.expected_version}</span><h4>{preview.title}</h4></div>
          <span className="version-chip">{preview.entries.length} 条可导入</span>
        </div>
        <p className="author-revision-help">未勾选的条目仍会保存为仅作者可见。勾选后，条目可进入玩家 GM 上下文。常驻标记与字面关键词作为匹配设置保留。</p>
        <p className="author-revision-help">来源扩展字段与设置会随作者备份保存，不会作为可执行内容运行。</p>
        {publicKeys.size >= MAX_PUBLIC_WORLD_BOOK_KEYS && <p className="author-revision-inline-warning" role="status">最多允许 {MAX_PUBLIC_WORLD_BOOK_KEYS} 条目进入玩家 GM 上下文。</p>}
      </div>

      {preview.entries.length > 0 ? <div className="world-book-entry-list">
        {preview.entries.map((entry) => <WorldBookPreviewCard
          key={entry.source_key}
          entry={entry}
          isPublic={publicKeys.has(entry.source_key)}
          publicLimitReached={publicKeys.size >= MAX_PUBLIC_WORLD_BOOK_KEYS}
          disabled={busy || refreshRequired}
          onPublicChange={(checked) => setEntryPublic(entry.source_key, checked)}
        />)}
      </div> : <p className="author-revision-inline-warning" role="status">没有可导入条目；请检查下方未导入原因。</p>}

      {preview.skipped_entries.length > 0 && <section className="world-book-skipped" aria-labelledby="world-book-skipped-title">
        <div className="entity-components-heading"><div><span className="panel-overline">NOT IMPORTED</span><h3 id="world-book-skipped-title">未导入条目</h3></div><span>{preview.skipped_entries.length} 条</span></div>
        <dl className="character-preview-fields">{preview.skipped_entries.map((entry, index) => <div className="character-preview-field" key={`${entry.source_key}-${index}`}><dt>{entry.source_key}</dt><dd>{entry.reason}</dd></div>)}</dl>
      </section>}

      {error && <p className="character-create-error" role="alert">{error}</p>}
      {refreshRequired && <div className="character-conflict" role="alert"><span>分支版本已变化，请重新读取作者投影后再预览。</span><button type="button" className="text-button" onClick={onCommitted} disabled={busy}>重新读取作者投影</button></div>}
      <div className="character-preview-actions">
        <button type="button" className="button button-secondary" onClick={clearPreview} disabled={busy}>返回修改</button>
        <button type="button" className="button button-primary" onClick={() => void commit()} disabled={busy || refreshRequired || preview.entries.length === 0}>
          {busy ? <><Icon name="spinner" size={13} />正在保存…</> : <>确认导入 {preview.entries.length} 条 <Icon name="check" size={13} /></>}
        </button>
      </div>
      <p className="author-revision-help">预览版本 {preview.expected_version} · 当前分支版本 {stateVersion} · 确认后才会写入</p>
    </div>}
  </section>
}

function WorldBookEntryCard({ entry }: { entry: WorldBookEntry }) {
  return <article className="author-revision-preview">
    <div className="author-preview-heading">
      <div><span className="entity-component-key">{entry.source_key}</span><h4>{entry.title}</h4></div>
      <span className={entry.visibility === 'public' ? 'no-tick-effect' : 'version-chip'}>{entry.visibility === 'public' ? '玩家 GM 可用' : '仅作者'}</span>
    </div>
    <p className="entity-component-text">{entry.content}</p>
    <p className="author-revision-help">关键词：{entry.keywords.length ? entry.keywords.join('、') : '无'} · 常驻标记：{entry.always_on ? '开启' : '关闭'}</p>
  </article>
}

function WorldBookPreviewCard({ entry, isPublic, publicLimitReached, disabled, onPublicChange }: {
  entry: WorldBookPreviewEntry
  isPublic: boolean
  publicLimitReached: boolean
  disabled: boolean
  onPublicChange: (checked: boolean) => void
}) {
  return <article className="author-revision-preview">
    <div className="author-preview-heading">
      <div><span className="entity-component-key">{entry.source_key}</span><h4>{entry.title}</h4></div>
      <span className={isPublic ? 'no-tick-effect' : 'version-chip'}>{isPublic ? '玩家 GM 可用' : '仅作者'}</span>
    </div>
    <p className="entity-component-text">{entry.content}</p>
    <p className="author-revision-help">关键词：{entry.keywords.length ? entry.keywords.join('、') : '无'} · 常驻标记：{entry.always_on ? '开启' : '关闭'}</p>
    <label className="author-toggle-field">
      <span><strong>允许进入玩家 GM 上下文</strong><small>未勾选时仅作者可见</small></span>
      <input type="checkbox" checked={isPublic} disabled={disabled || (!isPublic && publicLimitReached)} onChange={(event) => onPublicChange(event.target.checked)} />
    </label>
  </article>
}
