import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError, type AuthorKinshipLink, type CharacterEntity } from '../api/client'
import type { Notice } from '../App'
import { errorMessage } from '../utils'
import { Icon } from './Icon'

type RelationshipKind = AuthorKinshipLink['relationship']

export function AuthorKinshipEditor({
  worldId, branchId, stateVersion, people, links, onNotice, onCommitted,
}: {
  worldId: string
  branchId: string
  stateVersion: string
  people: CharacterEntity[]
  links?: AuthorKinshipLink[]
  onNotice: (notice: Notice) => void
  onCommitted: () => void
}) {
  const characters = people.filter((person) => person.kind === 'character' || person.kind === 'person')
  const [parentSelection, setParentSelection] = useState(characters[0]?.id ?? '')
  const [childSelection, setChildSelection] = useState(characters[1]?.id ?? '')
  const [relationship, setRelationship] = useState<RelationshipKind>('birth')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshRequired, setRefreshRequired] = useState(false)
  const [confirmingLinkId, setConfirmingLinkId] = useState<string | null>(null)
  const createKey = useRef(crypto.randomUUID())
  const endKeys = useRef(new Map<string, string>())

  const validLinks = Array.isArray(links) ? links.filter(isKinshipLink) : []
  const invalidLinkCount = Array.isArray(links) ? links.length - validLinks.length : 0
  const activeLinks = validLinks.filter((link) => link.ended_tick === null || link.ended_tick === undefined)
  const endedLinks = validLinks.filter((link) => link.ended_tick !== null && link.ended_tick !== undefined)
  const selectedParentId = characters.some((person) => person.id === parentSelection)
    ? parentSelection
    : characters[0]?.id ?? ''
  const selectedChildId = characters.some((person) => person.id === childSelection && person.id !== selectedParentId)
    ? childSelection
    : characters.find((person) => person.id !== selectedParentId)?.id ?? ''
  const names = new Map(characters.map((person) => [person.id, person.name?.trim() || '未命名人物']))
  const canCreate = Boolean(
    branchId && selectedParentId && selectedChildId && selectedParentId !== selectedChildId
    && !busy && !refreshRequired,
  )

  const updateParent = (value: string) => {
    createKey.current = crypto.randomUUID()
    setParentSelection(value)
    setError(null)
    setRefreshRequired(false)
  }
  const updateChild = (value: string) => {
    createKey.current = crypto.randomUUID()
    setChildSelection(value)
    setError(null)
    setRefreshRequired(false)
  }
  const updateRelationship = (value: RelationshipKind) => {
    createKey.current = crypto.randomUUID()
    setRelationship(value)
    setError(null)
    setRefreshRequired(false)
  }

  const createLink = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canCreate) return
    setBusy(true)
    setError(null)
    setRefreshRequired(false)
    try {
      await api.recordAuthorKinshipLink(worldId, {
        branch_id: branchId,
        expected_state_version: stateVersion,
        parent_id: selectedParentId,
        child_id: selectedChildId,
        relationship,
      }, createKey.current)
      const parentName = names.get(selectedParentId) ?? selectedParentId
      const childName = names.get(selectedChildId) ?? selectedChildId
      onNotice({ kind: 'success', text: `已记录「${parentName}」与「${childName}」的亲缘关系；世界时间未推进。` })
      createKey.current = crypto.randomUUID()
      onCommitted()
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally { setBusy(false) }
  }

  const endLink = async (link: AuthorKinshipLink) => {
    const expectedCreatedTick = exactWorldTick(link.created_tick)
    if (!expectedCreatedTick || busy || refreshRequired) return
    setBusy(true)
    setError(null)
    setRefreshRequired(false)
    const key = endKeys.current.get(link.link_id) ?? crypto.randomUUID()
    endKeys.current.set(link.link_id, key)
    try {
      await api.endAuthorKinshipLink(worldId, link.link_id, {
        branch_id: branchId,
        expected_state_version: stateVersion,
        expected_created_tick: expectedCreatedTick,
        expected_relationship: link.relationship,
      }, key)
      const parentName = names.get(link.parent_id) ?? link.parent_id
      const childName = names.get(link.child_id) ?? link.child_id
      onNotice({ kind: 'success', text: `已结束「${parentName}」与「${childName}」的亲缘关系；历史记录保留，世界时间未推进。` })
      endKeys.current.delete(link.link_id)
      setConfirmingLinkId(null)
      onCommitted()
    } catch (reason) {
      setError(errorMessage(reason))
      if (reason instanceof ApiError && reason.status === 409) setRefreshRequired(true)
    } finally { setBusy(false) }
  }

  const refresh = () => {
    setError(null)
    setRefreshRequired(false)
    setConfirmingLinkId(null)
    onCommitted()
  }

  return <section className="author-revision-panel" aria-labelledby="author-kinship-title">
    <div className="author-revision-heading">
      <div><span className="panel-overline">AUTHOR FAMILY RECORDS</span><h3 id="author-kinship-title">亲缘关系</h3><p>作者可记录与结束直接亲缘关系。角色名字不会用于推断亲属；操作不会推进世界时间。</p></div>
      <Icon name="users" size={17} />
    </div>

    <section className="entity-components" aria-label="当前作者投影中的亲缘记录">
      <div className="entity-components-heading"><div><span className="panel-overline">ACTIVE · HISTORY</span><h3>亲缘记录</h3></div><span>{activeLinks.length} 条有效 · {endedLinks.length} 条已结束</span></div>
      {invalidLinkCount > 0 && validLinks.length > 0 && <p className="author-revision-inline-warning" role="alert">有 {invalidLinkCount} 条亲缘记录无法读取，已隐藏；显示的行来自当前作者投影。</p>}
      {links === undefined
        ? <p className="entity-components-empty" role="status">此作者投影未提供亲缘记录字段；现有记录是否为空尚无法确认。</p>
        : invalidLinkCount > 0 && validLinks.length === 0
          ? <p className="author-revision-inline-warning" role="alert">亲缘记录字段无法读取，未将其显示为「没有关系」。请刷新作者投影或检查服务端数据。</p>
        : validLinks.length === 0
          ? <p className="entity-components-empty" role="status">当前作者投影没有亲缘记录。</p>
          : <div className="entity-component-list">
            {[...activeLinks, ...endedLinks].map((link) => {
              const active = link.ended_tick === null || link.ended_tick === undefined
              const preciseCreatedTick = exactWorldTick(link.created_tick)
              const canEnd = active && Boolean(preciseCreatedTick) && !busy && !refreshRequired
              return <article className="entity-component-card" key={link.link_id}>
                <div className="entity-component-card-heading">
                  <div><span className="entity-component-key">{link.link_id}</span><h4>{relationshipLabel(link.relationship)}</h4></div>
                  <span className="version-chip">{active ? '有效' : '已结束'}</span>
                </div>
                <dl className="entity-component-fields">
                  <div className="entity-component-field"><dt>家长</dt><dd>{names.get(link.parent_id) ?? `人物 ${link.parent_id}`}</dd></div>
                  <div className="entity-component-field"><dt>子女</dt><dd>{names.get(link.child_id) ?? `人物 ${link.child_id}`}</dd></div>
                  <div className="entity-component-field"><dt>建立 tick</dt><dd>{formatWorldTick(link.created_tick)}</dd></div>
                  {!active && <div className="entity-component-field"><dt>结束 tick</dt><dd>{formatWorldTick(link.ended_tick)}</dd></div>}
                </dl>
                {active && <div className="kinship-end-action">
                  {confirmingLinkId === link.link_id ? <div className="author-revision-inline-warning" role="group" aria-label="确认结束亲缘关系">
                    <p>确认在当前世界时间结束这条关系吗？历史记录会保留。</p>
                    <div className="character-preview-actions">
                      <button type="button" className="button button-secondary" onClick={() => setConfirmingLinkId(null)} disabled={busy}>取消</button>
                      <button type="button" className="button button-primary" onClick={() => void endLink(link)} disabled={!canEnd}>
                        {busy ? <><Icon name="spinner" size={14} />处理中…</> : '确认结束关系'}
                      </button>
                    </div>
                  </div> : <button
                    type="button"
                    className="button button-secondary button-small"
                    onClick={() => { setError(null); setConfirmingLinkId(link.link_id) }}
                    disabled={!canEnd}
                    aria-label={`结束 ${names.get(link.parent_id) ?? '家长'} 与 ${names.get(link.child_id) ?? '子女'} 的亲缘关系`}
                    title={preciseCreatedTick ? '结束关系前会再次检查分支版本和建立时点' : '建立 tick 无法精确读取，暂不能安全提交结束操作'}
                  >结束关系</button>}
                </div>}
              </article>
            })}
          </div>}
    </section>

    <form className="author-schema-form" onSubmit={(event) => void createLink(event)}>
      <div className="entity-components-heading"><div><span className="panel-overline">NEW DIRECT LINK</span><h3>记录亲缘关系</h3></div></div>
      {characters.length < 2 && <p className="author-revision-inline-warning" role="status">作者投影至少需要两位人物才能记录亲缘关系。</p>}
      <div className="author-trait-grid">
        <label className="author-simple-field"><span>家长</span><select value={selectedParentId} onChange={(event) => updateParent(event.target.value)} disabled={characters.length < 2 || busy || refreshRequired}>
          {characters.map((person) => <option key={person.id} value={person.id}>{person.name || '未命名人物'} · {person.id}</option>)}
        </select></label>
        <label className="author-simple-field"><span>子女</span><select value={selectedChildId} onChange={(event) => updateChild(event.target.value)} disabled={characters.length < 2 || busy || refreshRequired}>
          {characters.filter((person) => person.id !== selectedParentId).map((person) => <option key={person.id} value={person.id}>{person.name || '未命名人物'} · {person.id}</option>)}
        </select></label>
        <label className="author-simple-field author-wide-field"><span>关系类型</span><select value={relationship} onChange={(event) => updateRelationship(event.target.value as RelationshipKind)} disabled={busy || refreshRequired}>
          <option value="birth">出生亲缘</option>
          <option value="adoptive">收养亲缘</option>
        </select></label>
      </div>
      <p className="author-revision-help">请按作者已确认的资料直接记录关系。服务端会验证人物、重复边与谱系循环。</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {refreshRequired && <div className="author-revision-stale" role="alert"><div><strong>作者档案需要刷新</strong><span>分支状态已变化。刷新投影后，再检查亲缘记录并重新操作。</span></div><button type="button" className="button button-secondary button-small" onClick={refresh} disabled={busy}>刷新作者投影</button></div>}
      <div className="character-create-actions"><span>分支版本 {stateVersion} · 服务端提交后才会写入</span><button className="button button-primary" type="submit" disabled={!canCreate}>{busy ? <><Icon name="spinner" size={15} />处理中…</> : <>记录亲缘关系 <Icon name="arrow-right" size={15} /></>}</button></div>
    </form>
  </section>
}

function isKinshipLink(value: unknown): value is AuthorKinshipLink {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const link = value as Partial<AuthorKinshipLink>
  return typeof link.link_id === 'string'
    && typeof link.parent_id === 'string'
    && typeof link.child_id === 'string'
    && (link.relationship === 'birth' || link.relationship === 'adoptive')
    && (typeof link.created_tick === 'string' || typeof link.created_tick === 'number')
    && (link.ended_tick === undefined || link.ended_tick === null || typeof link.ended_tick === 'string' || typeof link.ended_tick === 'number')
}

function exactWorldTick(value: string | number | null | undefined): string | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) ? String(value) : null
  if (typeof value !== 'string' || !/^(0|-?[1-9]\d{0,18})$/.test(value)) return null
  try {
    const parsed = BigInt(value)
    return parsed >= -(1n << 63n) && parsed <= (1n << 63n) - 1n ? value : null
  } catch {
    return null
  }
}

function formatWorldTick(value: string | number | null | undefined): string {
  return exactWorldTick(value) ?? '无法精确读取'
}

function relationshipLabel(value: RelationshipKind): string {
  return value === 'birth' ? '出生亲缘' : '收养亲缘'
}
