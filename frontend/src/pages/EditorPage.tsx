import { useEffect, useMemo, useState } from 'react'
import { api, type CharacterEntity, type World, type WorldView } from '../api/client'
import { errorMessage, worldStatusLabel, initials } from '../utils'
import { InlineState, PageEmpty } from '../components/States'
import { Icon } from '../components/Icon'

export function EditorPage({ world, branchId, onSelectWorld }: { world: World | null; branchId: string | null; onSelectWorld: () => void }) {
  const [tab, setTab] = useState<'world' | 'people'>('world')
  const [selectedPerson, setSelectedPerson] = useState<string | null>(null)
  const [view, setView] = useState<WorldView | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const entities = Object.values(view?.state.entities ?? {})
  const people = useMemo(() => entities.filter((entity) => entity.kind === 'character') as CharacterEntity[], [entities])
  const person = people.find((entry) => entry.id === selectedPerson) ?? people[0]

  useEffect(() => {
    if (!world) { setView(null); return }
    let active = true
    setLoading(true); setError(null); setView(null)
    void api.worldView(world.world_id, branchId ?? world.active_branch_id ?? undefined, 'author').then((authorView) => {
      if (active) setView(authorView)
    }).catch((reason) => { if (active) setError(errorMessage(reason)) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [world?.world_id, branchId])

  if (!world) return <PageEmpty eyebrow="作者工作区" title="选择一个世界以查看档案。" body="作者设定需要通过服务端的作者权限校验后才能载入。" action={<button className="button button-primary" onClick={onSelectWorld}>前往世界库 <Icon name="arrow-right" size={16} /></button>} />

  return <section className="editor-page page-enter">
    <div className="editor-heading"><div><div className="eyebrow"><span className="eyebrow-line" />作者工作区</div><h1>设定档案</h1><p>作者视角会显示世界真相与秘密。所有变更只有经过服务端提交后才会生效。</p></div><span className="spoiler-badge"><Icon name="eye" size={13} />作者视角 · 剧透</span></div>
    <div className="editor-toolbar"><div className="editor-world-select"><span className="mini-world-mark"><Icon name="orbit" size={15} /></span><span><strong>{world.title}</strong><small>{world.world_id}</small></span></div><div className="editor-tabs" role="tablist" aria-label="档案类别"><button role="tab" aria-selected={tab === 'world'} className={tab === 'world' ? 'active' : ''} onClick={() => setTab('world')}>世界设定</button><button role="tab" aria-selected={tab === 'people'} className={tab === 'people' ? 'active' : ''} onClick={() => setTab('people')}>关键人物 <span>{people.length}</span></button></div><button className="button button-secondary button-small" disabled title="等待作者变更 API"><Icon name="edit" size={14} />编辑草稿</button></div>
    {loading ? <div className="editor-loading"><Icon name="spinner" />读取作者投影…</div> : error ? <InlineState icon="alert" title="无法打开作者档案" description={error} action={<span className="permission-note">作者视角需要世界所有者权限。</span>} /> : !view ? <InlineState icon="book" title="作者投影为空" description="服务端未返回当前分支的档案数据。" /> : tab === 'world' ? <div className="editor-content"><div className="editor-main"><div className="editor-panel-title"><div><span className="panel-overline">WORLD PROFILE</span><h2>世界概览</h2></div><span className="version-chip">版本 {view.world.state_version}</span></div><div className="profile-grid"><ReadField label="世界名称" value={view.world.title} /><ReadField label="存档状态" value={worldStatusLabel(view.world.status)} /><ReadField label="当前分支" value={view.branch.label || view.branch.branch_id} /><ReadField label="世界时间" value={view.branch.current_tick} /><ReadField label="历法" value={view.state.calendar?.name ?? view.state.calendar?.calendar_id ?? '投影未提供'} /><ReadField label="规则版本" value={view.world.ruleset_version ?? '投影未提供'} /></div><div className="metadata-panel"><div className="metadata-heading"><span>作者备注与基础描述</span><span>作者投影</span></div>{view.state.metadata && Object.keys(view.state.metadata).length ? <pre>{JSON.stringify(view.state.metadata, null, 2)}</pre> : <p>服务端没有返回可展示的元数据。</p>}</div><div className="editor-placeholder"><span className="placeholder-icon"><Icon name="edit" size={17} /></span><div><strong>设定变更接口尚未开放</strong><p>本页展示经作者权限校验的世界真相。修改世界规则需要版本化预览与提交，接口接入后才会开放保存。</p></div></div></div><aside className="editor-side"><div className="side-title">作者投影</div><ProjectionStat label="实体" value={entities.length} caption="作者权限可见" /><ProjectionStat label="人物" value={people.length} caption="含当前分支角色实体" /><ProjectionStat label="资源账目" value={Object.keys(view.state.resources ?? {}).length} caption="按当前分支读取" /><div className="privacy-note spoiler-note"><Icon name="eye" size={14} /><span>此视角可能包含玩家未知的秘密与真相。</span></div></aside></div> : <div className="people-editor-layout"><aside className="people-index"><div className="people-index-heading"><div><span className="panel-overline">CAST</span><h2>关键人物</h2></div><span>{people.length}</span></div>{people.length ? people.map((entry) => <button key={entry.id} className={`people-index-item ${person?.id === entry.id ? 'selected' : ''}`} onClick={() => setSelectedPerson(entry.id)}><span className="character-avatar small-character-avatar">{initials(entry.name ?? '')}</span><span><strong>{entry.name ?? '未命名人物'}</strong><small>{entry.status ?? '状态未知'}</small></span><Icon name="chevron" size={15} /></button>) : <div className="people-empty-compact">当前世界没有返回人物实体。</div>}</aside>{person ? <article className="person-profile"><div className="person-profile-top"><span className="character-avatar large-character-avatar">{initials(person.name ?? '')}</span><div><span className="panel-overline">CHARACTER PROFILE</span><h2>{person.name ?? '未命名人物'}</h2><p>{person.status ?? '状态未知'}</p></div><span className="spoiler-badge compact-spoiler"><Icon name="eye" size={12} />剧透</span></div><div className="character-fields"><ReadField label="人物名称" value={person.name ?? '服务端未提供'} /><ReadField label="当前状态" value={person.status ?? '服务端未提供'} /><ReadField label="地点引用" value={person.location_id ?? '服务端未提供'} /><ReadField label="实体标识" value={person.id} /></div><div className="metadata-panel"><div className="metadata-heading"><span>人物组件</span><span>作者投影</span></div>{person.components && Object.keys(person.components).length ? <pre>{JSON.stringify(person.components, null, 2)}</pre> : <p>人物的扩展档案尚未由服务端提供。</p>}</div><div className="editor-placeholder"><span className="placeholder-icon"><Icon name="edit" size={17} /></span><div><strong>人物编辑与试演接口尚未开放</strong><p>人物实例从作者投影读取。档案修改、关系编辑和试演报告需要后端变更接口。</p></div></div></article> : <div className="person-profile-empty"><Icon name="user" size={22} /><strong>没有可打开的人物档案</strong></div>}</div>}
  </section>
}

function ReadField({ label, value }: { label: string; value: string | number }) { return <div className="read-field"><span>{label}</span><strong>{value}</strong></div> }
function ProjectionStat({ label, value, caption }: { label: string; value: number; caption: string }) { return <div className="projection-stat"><span>{label}</span><strong>{value}</strong><small>{caption}</small></div> }
