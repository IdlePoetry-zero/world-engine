import type { Pack, World } from '../api/client'
import { InlineState } from '../components/States'
import { Icon } from '../components/Icon'
import { WorldPackImport } from '../components/WorldPackImport'
import { errorMessage, worldStatusLabel } from '../utils'
import { BackupPreview } from './HistoryPage'

export function LibraryPage({ worlds, packs, loading, error, onSelect, onCreate, onRetry }: { worlds: World[]; packs: Pack[]; loading: boolean; error: Error | null; onSelect: (id: string) => void; onCreate: () => void; onRetry: () => void }) {
  return (
    <section className="library-page page-enter">
      <BackupPreview />
      <div className="library-headline">
        <div className="headline-row">
          <div><h1>世界库</h1><p className="page-intro">从设定包创建新世界，或继续已有存档。</p></div>
          <button className="button button-primary" onClick={onCreate}><Icon name="plus" size={17} />创建世界</button>
        </div>
      </div>
      <div className="section-heading-row">
        <h2 className="section-title">本地存档</h2>
        {loading && <span className="subtle-label" role="status">正在同步…</span>}
      </div>
      {loading ? <div className="world-grid"><div className="world-skeleton" /><div className="world-skeleton" /></div> : error ? <InlineState icon="cloud-off" title="暂时无法载入世界库" description={errorMessage(error)} action={<button className="button button-secondary" onClick={onRetry}>重新连接</button>} /> : worlds.length === 0 ? <div className="empty-library"><div className="empty-orbit"><span className="empty-orbit-core" /><i /><i /><i /></div><div className="empty-library-copy"><div className="empty-kicker">静候第一段历史</div><h3>这里还没有世界。</h3><p>创建流程会从后台可用的设定包开始。创建后，世界状态由本地引擎保存与推进。</p><button className="button button-primary" onClick={onCreate}><Icon name="plus" size={16} />创建第一个世界</button></div><div className="empty-library-foot"><span><Icon name="shield" size={14} />数据保存在本机</span><span>·</span><span>当前可用设定包 {packs.length}</span></div></div> : <div className="world-grid">{worlds.map((world, index) => <WorldCard key={world.world_id} world={world} index={index} onClick={() => onSelect(world.world_id)} />)}</div>}
      <WorldPackImport connecting={loading} onImported={onRetry} />
      <div className="library-bottom-row"><div className="library-note"><span className="note-mark">✳</span><span><strong>存档保存在本机。</strong> 允许云端模型时，生成请求可能发送玩家输入和当前任务获准使用的世界内容。可在「模型与运行」查看目的地和调整权限。</span></div><button className="text-button" onClick={onCreate}>浏览可用设定包 <Icon name="arrow-right" size={15} /></button></div>
    </section>
  )
}

function WorldCard({ world, index, onClick }: { world: World; index: number; onClick: () => void }) {
  return <button className={`world-card world-card-${index % 3}`} onClick={onClick}>
    <div className="world-card-art" aria-hidden="true">
      <div className="card-art-grid" /><div className="card-land" /><div className="card-moon" />
      <span className="card-open"><Icon name="arrow-up-right" size={16} /></span>
    </div>
    <div className="world-card-body">
      <div className="world-card-meta"><span className={`status-tag status-${world.status}`}>{worldStatusLabel(world.status)}</span></div>
      <h3>{world.title}</h3>
    </div>
  </button>
}
