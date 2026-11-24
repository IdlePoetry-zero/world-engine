import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, type WorldPackImportResult, type WorldPackPlayerSetup, type WorldPackPreview } from '../api/client'
import { errorMessage } from '../utils'
import { Icon } from './Icon'

const MAX_WORLD_PACK_BYTES = 16 * 1024 * 1024

// An unacknowledged submission can be retried even after leaving the library.
const pendingImportKeys = new Map<string, string>()

export function WorldPackImport({ connecting, onImported }: { connecting: boolean; onImported: () => void }) {
  const [archive, setArchive] = useState<File | null>(null)
  const [preview, setPreview] = useState<WorldPackPreview | null>(null)
  const [openingId, setOpeningId] = useState('')
  const [playerSetup, setPlayerSetup] = useState<WorldPackPlayerSetup>({})
  const openings = preview?.story_openings ?? []
  const selectedOpening = openings.find((opening) => opening.opening_id === openingId)
  const playerProfiles = selectedOpening?.player_profiles
  const selectedProfile = playerProfiles?.find((profile) => profile.profile_id === playerSetup.profile_id)
  const [result, setResult] = useState<WorldPackImportResult | null>(null)
  const [failure, setFailure] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [importing, setImporting] = useState(false)
  const previewRequest = useRef<AbortController | null>(null)
  const importPending = useRef(false)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => () => previewRequest.current?.abort(), [])

  function selectArchive(file: File | undefined) {
    if (importPending.current) return
    previewRequest.current?.abort()
    previewRequest.current = null
    setPreviewing(false)
    setPreview(null)
    setOpeningId('')
    setPlayerSetup({})
    setResult(null)
    const problem = file?.size === 0 ? '请选择非空的 ZIP 文件。'
      : file && file.size > MAX_WORLD_PACK_BYTES ? '世界包 ZIP 不能超过 16 MiB。'
      : ''
    setFailure(problem)
    setArchive(problem ? null : file ?? null)
    if (problem && input.current) input.current.value = ''
  }

  async function makePreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!archive || previewRequest.current || importPending.current || connecting) return
    const request = new AbortController()
    previewRequest.current = request
    setPreviewing(true)
    setFailure('')
    setPreview(null)
    try {
      const report = await api.previewWorldPack(archive, request.signal)
      if (previewRequest.current === request && !request.signal.aborted) setPreview(report)
    } catch (reason) {
      if (!request.signal.aborted) setFailure(errorMessage(reason))
    } finally {
      if (previewRequest.current === request && !request.signal.aborted) {
        previewRequest.current = null
        setPreviewing(false)
      }
    }
  }

  async function confirmImport() {
    if (!archive || !preview?.can_import || previewRequest.current || importPending.current || connecting
      || (openings.length > 0 && !selectedOpening)) return
    const digest = preview.archive_sha256
    const name = playerSetup.name?.trim()
    const description = playerSetup.description?.trim()
    const setup = playerProfiles && (selectedProfile || name || description) ? {
      profile_id: selectedProfile?.profile_id,
      name: name || undefined,
      description: description || undefined,
    } : undefined
    const openingKey = selectedOpening ? `${digest}/${selectedOpening.opening_id}` : digest
    const selectionKey = setup ? JSON.stringify([openingKey, setup]) : openingKey
    importPending.current = true
    setImporting(true)
    setFailure('')
    try {
      const key = pendingImportKeys.get(selectionKey) ?? crypto.randomUUID()
      pendingImportKeys.set(selectionKey, key)
      const imported = await api.importWorldPack(archive, digest, key, selectedOpening?.opening_id, setup)
      if (pendingImportKeys.get(selectionKey) === key) pendingImportKeys.delete(selectionKey)
      setResult(imported)
      setPreview(null)
      setOpeningId('')
      setPlayerSetup({})
      setArchive(null)
      if (input.current) input.current.value = ''
    } catch (reason) {
      setFailure(errorMessage(reason))
      if (reason instanceof ApiError && reason.code === 'WORLD_PACK_PREVIEW_MISMATCH') setPreview(null)
      return
    } finally {
      importPending.current = false
      setImporting(false)
    }
    onImported()
  }

  return (
    <section className="author-revision-panel" aria-labelledby="world-pack-import-heading">
      <div className="author-revision-heading">
        <div>
          <h3 id="world-pack-import-heading">导入世界包</h3>
          <p>预览 ZIP 的兼容性，确认后将开局导入为新世界。</p>
        </div>
        <Icon name="archive" size={18} />
      </div>
      <form className="world-book-import-form" onSubmit={(event) => void makePreview(event)}>
        <label className="author-simple-field" htmlFor="world-pack-archive">
          <span>世界包 ZIP <small>最多 16 MiB</small></span>
          <input id="world-pack-archive" ref={input} type="file" accept=".zip,application/zip" disabled={importing}
            onChange={(event) => selectArchive(event.currentTarget.files?.[0])} />
        </label>
        <div className="character-create-actions">
          <button className="button button-secondary" type="submit" disabled={!archive || connecting || previewing || importing}>
            {previewing ? '正在检查兼容性…' : '预览世界包'}
          </button>
        </div>
      </form>
      {preview && (
        <div className="author-revision-preview">
          <WorldPackReview preview={preview} />
          {openings.length > 0 && <>
            <label className="author-simple-field">
              <span>故事介入点</span>
              <select disabled={importing} value={openingId} onChange={(event) => {
                setOpeningId(event.currentTarget.value)
                setPlayerSetup({})
              }}>
                <option value="">选择介入点</option>
                {openings.map((opening) => <option key={opening.opening_id} value={opening.opening_id}>{opening.title}</option>)}
              </select>
            </label>
            {selectedOpening && <div className="author-revision-help">
              <p>{selectedOpening.position}</p>
              {selectedOpening.opening_state?.player_name && <p>开场角色 {selectedOpening.opening_state.player_name}</p>}
              {selectedOpening.opening_state?.place_name && <p>开场地点 {selectedOpening.opening_state.place_name}</p>}
              {selectedOpening.known_history.length > 0 && <><strong>开局已知历史</strong>
                <ul>{selectedOpening.known_history.map((item, index) => <li key={index}>{item}</li>)}</ul>
              </>}
            </div>}
            {playerProfiles && <>
              <label className="author-simple-field">
                <span>角色身份 <small>可选</small></span>
                <select value={selectedProfile?.profile_id ?? ''} disabled={importing}
                  onChange={(event) => setPlayerSetup({ ...playerSetup, profile_id: event.currentTarget.value })}>
                  <option value="">沿用开局身份与饮食需求</option>
                  {playerProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.title}</option>)}
                </select>
              </label>
              {selectedProfile && <div className="author-revision-help">
                <p>{selectedProfile.description}</p>
                <p>{selectedProfile.food_requirement === 'not_required' ? '此身份无需进食。' : '饮食需求沿用原开局。'}</p>
              </div>}
              <label className="author-simple-field">
                <span>角色姓名 <small>可选，最多 80 字</small></span>
                <input type="text" maxLength={80} value={playerSetup.name ?? ''} disabled={importing}
                  placeholder={selectedOpening?.opening_state?.player_name ?? undefined}
                  onChange={(event) => setPlayerSetup({ ...playerSetup, name: event.currentTarget.value })} />
              </label>
              <label className="author-simple-field">
                <span>身份描述 <small>可选，最多 240 字</small></span>
                <textarea rows={2} maxLength={240} value={playerSetup.description ?? ''} disabled={importing}
                  aria-describedby="world-pack-player-description-help"
                  onChange={(event) => setPlayerSetup({ ...playerSetup, description: event.currentTarget.value })} />
              </label>
              <p id="world-pack-player-description-help" className="author-revision-help">开局后可自行决定目标和行动。超自然能力由世界规则授予。</p>
            </>}
          </>}
          {preview.can_import && (
            <div className="character-preview-actions">
              <button className="button button-primary" type="button" disabled={importing || connecting || (openings.length > 0 && !selectedOpening)} onClick={() => void confirmImport()}>
                {importing ? '正在导入…' : '确认导入为新世界'}
              </button>
            </div>
          )}
        </div>
      )}
      {failure && <p className="character-create-error" role="alert">{failure}</p>}
      {result && <p className="author-revision-help" role="status">{result.duplicate ? `世界「${result.title}」此前已导入。` : `世界「${result.title}」已导入。`}</p>}
    </section>
  )
}

function WorldPackReview({ preview }: { preview: WorldPackPreview }) {
  const { manifest, compatibility, blockers } = preview
  return <>
    <div className="author-preview-heading">
      <h4>{manifest.title}</h4>
      <span className="version-chip">{preview.can_import ? '可以导入' : '无法导入'}</span>
    </div>
    <dl className="character-preview-fields">
      <div className="character-preview-field"><dt>世界包标识</dt><dd>{manifest.pack_id}</dd></div>
      {Boolean(preview.story_openings?.length) && <div className="character-preview-field"><dt>世界类型</dt><dd>有主线世界</dd></div>}
      <div className="character-preview-field"><dt>版本</dt><dd>{manifest.version}</dd></div>
      <div className="character-preview-field"><dt>许可</dt><dd>{manifest.license}</dd></div>
      <div className="character-preview-field"><dt>引擎版本要求</dt><dd>{manifest.engine_range}</dd></div>
    </dl>
    {preview.contains_author_material && <p className="author-revision-inline-warning"><strong>含作者资料。</strong> 原始 ZIP 可能包含秘密，仅作者可下载。</p>}
    {blockers.length > 0 && <div className="author-revision-inline-warning" role="status">
      <strong>无法导入的原因</strong>
      <ul>{blockers.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
    </div>}
    {compatibility.dependencies.length > 0 && <div className="author-revision-help">
      <strong>依赖的背景版本</strong>
      <ul>{compatibility.dependencies.map(({ pack_id, version }) => <li key={`${pack_id}:${version}`}>{pack_id} {version}</li>)}</ul>
    </div>}
    {compatibility.unsupported_fields.length > 0 && <div className="author-revision-help">
      <strong>引擎不支持的字段仅保留在原始 ZIP 中</strong>
      <dl className="character-preview-fields">{compatibility.unsupported_fields.map(({ section, field }) => (
        <div className="character-preview-field" key={`${section}:${field}`}><dt>{section}</dt><dd><code>{field}</code></dd></div>
      ))}</dl>
    </div>}
    {compatibility.inert_asset_count > 0 && <p className="author-revision-help">另有 {compatibility.inert_asset_count} 个资源仅保留在原始 ZIP 中。</p>}
  </>
}
