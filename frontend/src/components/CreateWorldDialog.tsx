import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import type { Pack } from '../api/client'
import { Icon } from './Icon'
import { useModalKeyboard } from './useModalKeyboard'

export type CreateWorldDraft = {
  pack_id: string
  pack_version?: string
  title: string
  player_name: string
  seed?: number
  population_mode: 'natural' | 'sustained'
  npc_autonomy_mode: 'off' | 'limited_v1' | 'diverse_v1'
}

function packVersions(pack: Pack, populationMode: 'natural' | 'sustained') {
  const advertisedVersions = Array.isArray(pack.available_versions) ? pack.available_versions : []
  const sustainedVersions = populationMode === 'sustained' && Array.isArray(pack.sustained_available_versions)
    ? pack.sustained_available_versions
    : []
  return [...new Set([...advertisedVersions, ...sustainedVersions, pack.version]
    .filter((version): version is string => typeof version === 'string' && version.trim().length > 0))]
}

export function CreateWorldDialog({ packs, onClose, onSubmit }: { packs: Pack[]; onClose: () => void; onSubmit: (draft: CreateWorldDraft, idempotencyKey: string) => Promise<void> }) {
  const [packId, setPackId] = useState(packs[0]?.pack_id ?? '')
  const [packVersion, setPackVersion] = useState(packs[0]?.version ?? '')
  const [title, setTitle] = useState('')
  const [playerName, setPlayerName] = useState('')
  const [seed, setSeed] = useState('')
  const [populationMode, setPopulationMode] = useState<'natural' | 'sustained'>('natural')
  const [npcAutonomyMode, setNpcAutonomyMode] = useState<'off' | 'limited_v1' | 'diverse_v1'>('off')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const key = useRef(crypto.randomUUID())
  const dialogRef = useRef<HTMLElement>(null)
  useModalKeyboard(dialogRef, { onClose, busy: submitting })
  const updateDraft = (update: () => void) => { key.current = crypto.randomUUID(); update() }
  const selectedPack = packs.find((pack) => pack.pack_id === packId)
  const availableVersions = selectedPack ? packVersions(selectedPack, populationMode) : []
  const sustainedVersions = selectedPack && Array.isArray(selectedPack.sustained_available_versions)
    ? selectedPack.sustained_available_versions
    : []
  const selectedVersion = selectedPack && availableVersions.includes(packVersion) ? packVersion : selectedPack?.version ?? ''
  const advertisesV23 = availableVersions.includes('2.3.0')
  const advertisesSustainedV24 = populationMode === 'sustained' && sustainedVersions.includes('2.4.0')
  const advertisesSustainedV25 = populationMode === 'sustained' && sustainedVersions.includes('2.5.0')
  const advertisesV28 = availableVersions.includes('2.8.0')
  const advertisesSustainedV29 = populationMode === 'sustained'
    && selectedPack?.pack_id === 'low_magic_feudal'
    && sustainedVersions.includes('2.9.0')
  const advertisesSustainedModern210 = populationMode === 'sustained'
    && selectedPack?.pack_id === 'modern_city'
    && sustainedVersions.includes('2.10.0')
  const advertisesSustainedCultivation211 = populationMode === 'sustained'
    && selectedPack?.pack_id === 'long_life_cultivation'
    && sustainedVersions.includes('2.11.0')
  const waypointVersions = [
    ...(advertisesV28 ? ['v2.8.0'] : []),
    ...(advertisesSustainedV29 ? ['v2.9.0'] : []),
    ...(advertisesSustainedModern210 ? ['v2.10.0'] : []),
    ...(advertisesSustainedCultivation211 ? ['v2.11.0'] : []),
  ]
  const advertisesWaypointFeatures = waypointVersions.length > 0
  const waypointVersionLabel = waypointVersions.join('、')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (submitting || !packId || !title.trim() || !playerName.trim()) return
    const parsedSeed = seed.trim() === '' ? undefined : Number(seed)
    if (seed.trim() !== '' && (!/^-?\d+$/.test(seed.trim()) || !Number.isSafeInteger(parsedSeed))) {
      setError('随机种子必须是安全整数；留空则由引擎按默认方式生成。')
      return
    }
    setSubmitting(true); setError('')
    try {
      const submittedVersion = selectedPack && availableVersions.includes(packVersion) ? packVersion : selectedPack?.version
      await onSubmit({
        pack_id: packId,
        ...(selectedPack && submittedVersion && submittedVersion !== selectedPack.version ? { pack_version: submittedVersion } : {}),
        title: title.trim(),
        player_name: playerName.trim(),
        ...(parsedSeed === undefined ? {} : { seed: parsedSeed }),
        population_mode: populationMode,
        npc_autonomy_mode: npcAutonomyMode,
      }, key.current)
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : '创建请求失败，请重试。') }
    finally { setSubmitting(false) }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !submitting) onClose() }}>
      <section ref={dialogRef} tabIndex={-1} className="create-dialog" role="dialog" aria-modal="true" aria-labelledby="create-title">
        <div className="dialog-topline"><span className="eyebrow"><span className="eyebrow-line" />新建存档</span><button type="button" className="icon-button" onClick={onClose} aria-label="关闭" disabled={submitting}><Icon name="close" /></button></div>
        <div className="dialog-heading"><div className="dialog-glyph"><Icon name="orbit" size={23} /></div><h2 id="create-title">让一个世界开始。</h2><p>选择一份已发布的设定包，再为你的存档与角色命名。</p></div>
        {packs.length === 0 ? <div className="pack-empty"><div className="pack-empty-icon"><Icon name="archive" size={20} /></div><strong>当前没有可用的设定包</strong><span>后台尚未发布创建模板。这里不会用演示数据代替真实世界。</span></div> : <form onSubmit={(event) => void submit(event)}>
          <label className="field-label" htmlFor="pack-choice">世界设定包</label><select id="pack-choice" value={packId} onChange={(event) => updateDraft(() => {
            const nextPackId = event.target.value
            setPackId(nextPackId)
            setPackVersion(packs.find((pack) => pack.pack_id === nextPackId)?.version ?? '')
          })}>{packs.map((pack) => <option key={pack.pack_id} value={pack.pack_id}>{pack.title} · v{pack.version}</option>)}</select>
          {selectedPack && <div className="pack-description">{selectedPack.description}</div>}
          {selectedPack && availableVersions.length > 1 && <>
            <label className="field-label" htmlFor="pack-version">设定包版本</label>
            <select id="pack-version" value={selectedVersion} aria-describedby="pack-version-help" onChange={(event) => updateDraft(() => setPackVersion(event.target.value))}>
              {availableVersions.map((version) => <option key={version} value={version}>
                {version === selectedPack.version ? `默认版本 · v${version}` : version === '2.2.0' ? 'v2.2.0 · 市场报价与逾期补还' : version === '2.3.0' && advertisesV23 ? 'v2.3.0 · 家庭与职业' : version === '2.4.0' && advertisesSustainedV24 ? 'v2.4.0 · 具名居民扩充' : version === '2.5.0' && advertisesSustainedV25 ? 'v2.5.0 · 地区职业与技能' : version === '2.8.0' && advertisesV28 ? 'v2.8.0 · 途中机会' : version === '2.9.0' && advertisesSustainedV29 ? 'v2.9.0 · 地区组织驻地与职业供给' : version === '2.10.0' && advertisesSustainedModern210 ? 'v2.10.0 · 地区组织招募' : version === '2.11.0' && advertisesSustainedCultivation211 ? 'v2.11.0 · 地区职业与招募候选' : `v${version}`}
              </option>)}
            </select>
            <div className="pack-description" id="pack-version-help">
              默认选项沿用设定包当前版本。{availableVersions.includes('2.2.0') && ' v2.2.0 增加市场报价与逾期承诺补还。'}{advertisesV23 && ' v2.3.0 包含家庭关系、入职和付薪班次。'}{advertisesSustainedV24 && ' v2.4.0 在持续模式下缓冲具名角色的正常更替。'}{advertisesSustainedV25 && ' v2.5.0 提供地区职业与技能配置。'}{advertisesSustainedV29 && ' v2.9.0 为组织标注地区驻地，并补充地区职业供给。'}{advertisesSustainedModern210 && ' v2.10.0 在持续模式下为地区组织维护符合条件的招募候选人。'}{advertisesSustainedCultivation211 && ' v2.11.0 为 12 个地区配置职业，按当地组织章程认可的职业筛选席位候选人；席位补员本身不会自动授予组织成员资格。'}{advertisesWaypointFeatures && ` ${waypointVersionLabel} 均包含途中机会：连续行程中会在少数实际抵达且符合条件的地点遇到可选停留；无需自定义作者设定。`}选择其他版本会固定写入新存档。
            </div>
          </>}
          <label className="field-label" htmlFor="world-title">世界名称</label><input id="world-title" data-dialog-initial-focus value={title} onChange={(event) => updateDraft(() => setTitle(event.target.value))} placeholder="例如：雾港之后" maxLength={80} required />
          <label className="field-label" htmlFor="player-name">你的角色名</label><input id="player-name" value={playerName} onChange={(event) => updateDraft(() => setPlayerName(event.target.value))} placeholder="角色在世界里的名字" maxLength={60} required />
          <label className="field-label" htmlFor="population-mode">人口补充方式</label>
          <select id="population-mode" value={populationMode} aria-describedby="population-mode-help" onChange={(event) => updateDraft(() => {
            const nextMode = event.target.value as 'natural' | 'sustained'
            const nextAvailableVersions = selectedPack ? packVersions(selectedPack, nextMode) : []
            if (selectedPack && !nextAvailableVersions.includes(packVersion)) setPackVersion(selectedPack.version)
            setPopulationMode(nextMode)
          })}>
            <option value="natural">自然演化</option>
            <option value="sustained">有限补充</option>
          </select>
          <div className="pack-description" id="population-mode-help">自然模式不设置固定席位回补。有限补充会按地区配额和总量上限回补具名角色。</div>
          <label className="field-label" htmlFor="npc-autonomy-mode">NPC 自主性</label>
          <select id="npc-autonomy-mode" value={npcAutonomyMode} aria-describedby="npc-autonomy-mode-help" onChange={(event) => updateDraft(() => setNpcAutonomyMode(event.target.value as 'off' | 'limited_v1' | 'diverse_v1'))}>
            <option value="off">关闭</option>
            <option value="limited_v1">有限自主（旧版 v1）</option>
            <option value="diverse_v1">旅行与采购</option>
          </select>
          <div className="pack-description" id="npc-autonomy-mode-help">
            {npcAutonomyMode === 'off'
              ? '默认关闭。'
              : npcAutonomyMode === 'limited_v1'
                ? '旧版有限自主：为少量具名 NPC 记录自主与复审安排；到达复审节点时调用已配置的模型。模型不可用时，推进可能停在待决定节点。'
                : '预置 1 位关键 NPC 的旅行节点（第 1 天）和 2 位居民的储备复审（第 3、7 天），每项各 1 次。推进到节点时才调用已配置的模型；模型不可用时，推进可能停在待决定节点。'}
          </div>
          <label className="field-label" htmlFor="world-seed">随机种子 <span className="field-optional">可选</span></label>
          <input id="world-seed" type="number" inputMode="numeric" step="1" value={seed} onChange={(event) => updateDraft(() => setSeed(event.target.value))} placeholder="留空使用默认生成" aria-describedby="world-seed-help" />
          <div className="pack-description" id="world-seed-help">相同设定包、世界名、角色名和种子会生成相同的初始世界。支持正负安全整数。</div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button type="submit" className="button button-primary dialog-submit" disabled={submitting || !packId}>{submitting ? <><Icon name="spinner" size={16} />提交中…</> : <>创建世界 <Icon name="arrow-right" size={16} /></>}</button>
        </form>}
        <div className="dialog-foot"><Icon name="lock" size={13} />创建任务由本地引擎处理；离开页面不会让未提交的世界生效。</div>
      </section>
    </div>
  )
}
