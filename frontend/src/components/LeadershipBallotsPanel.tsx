import { type OwnLeadershipBallot, type OwnMembership } from '../api/client'
import { Icon } from './Icon'

const ballotStatusLabels: Record<string, string> = {
  open: '进行中',
  approved: '已通过',
  rejected: '未通过',
  cancelled: '已取消',
}
const voteLabels: Record<string, string> = { yes: '赞成', no: '反对', abstain: '弃权' }
const policyLabels: Record<string, string> = { election: '选举', council: '议会交权' }

export function LeadershipBallotsPanel({
  memberships: rawMemberships,
  ballots: rawBallots,
  projectionAvailable,
  loading,
  viewError,
  canPrepareAction,
  onPrepareAction,
}: {
  memberships?: OwnMembership[] | null
  ballots?: OwnLeadershipBallot[] | null
  projectionAvailable: boolean
  loading: boolean
  viewError: string | null
  canPrepareAction: boolean
  onPrepareAction: (text: string) => void
}) {
  const membershipDataAvailable = Array.isArray(rawMemberships)
  const ballotDataAvailable = Array.isArray(rawBallots)
  const memberships = membershipDataAvailable ? rawMemberships.filter(isMembership) : []
  const ballots = ballotDataAvailable ? rawBallots.filter(isBallot) : []
  const openOrganizations = new Set(ballots.filter((ballot) => ballot.status === 'open').map((ballot) => ballot.organization_id))
  const nominations = memberships.filter((membership) => membership.is_leader
    && (membership.ballot_policy === 'election' || membership.ballot_policy === 'council')
    && Array.isArray(membership.ballot_nominees)
    && !openOrganizations.has(membership.organization_id))
  const vacancyNominations = memberships.filter((membership) =>
    (membership.ballot_policy === 'election' || membership.ballot_policy === 'council')
    && Array.isArray(membership.vacancy_ballot_nominees)
    && !openOrganizations.has(membership.organization_id))
  const invalidCount = (membershipDataAvailable ? rawMemberships.length - memberships.length : 0)
    + (ballotDataAvailable ? rawBallots.length - ballots.length : 0)
  const actionableCount = ballots.filter((ballot) => ballot.status === 'open'
    && (ballot.is_incumbent || ballot.is_vacancy_proposer || ballot.is_voter)).length

  return <details className="shipment-panel promise-panel" aria-label="组织领导表决">
    <summary className="shipment-panel-summary">
      <span><Icon name="users" size={14} />领导表决</span>
      <small>{loading ? '读取中' : !projectionAvailable ? '角色投影未载入' : `${ballots.length} 项表决 · ${nominations.length + vacancyNominations.length} 项可提名`}</small>
    </summary>
    {loading && <p className="shipment-empty" role="status">正在读取组织表决…</p>}
    {!loading && viewError && <p className="shipment-empty" role="status">无法读取当前分支的组织表决。</p>}
    {!loading && !viewError && !projectionAvailable && <p className="shipment-empty">当前角色的世界投影尚未载入。</p>}
    {!loading && !viewError && projectionAvailable && !membershipDataAvailable && !ballotDataAvailable && <p className="shipment-empty">当前投影未提供提名或表决资料。</p>}
    {!loading && !viewError && projectionAvailable && membershipDataAvailable && ballotDataAvailable && nominations.length === 0 && vacancyNominations.length === 0 && ballots.length === 0 && <p className="shipment-empty">目前没有你可发起或参与的领导表决。</p>}
    {!loading && !viewError && projectionAvailable && <>
      {invalidCount > 0 && <p className="shipment-empty" role="alert">有 {invalidCount} 条表决资料无法读取，已隐藏。</p>}
      {membershipDataAvailable && nominations.length > 0 && <section aria-label="可提名组织">
        <p className="shipment-empty">在任者可从本组织列出的成员中明确提名。</p>
        <div className="shipment-list" aria-live="polite">
          {nominations.map((membership) => {
            const policy = membership.ballot_policy === 'election' ? 'election' : 'council'
            const candidates = (membership.ballot_nominees ?? []).filter(isNominee)
            return <article className="shipment-card" key={membership.organization_id}>
              <header className="shipment-card-heading">
                <span className="shipment-status">{policyLabels[policy]}</span>
                <small>{membership.name?.trim() || '组织名称未提供'}</small>
              </header>
              {candidates.length === 0
                ? <p className="shipment-empty">当前没有可提名的在任成员。</p>
                : <div className="promise-actions" aria-label={`${membership.name?.trim() || '组织'}可提名成员`}>
                  {candidates.map((candidate) => <button
                    type="button"
                    className="button button-secondary button-small"
                    key={candidate.member_id}
                    onClick={() => onPrepareAction(`我明确发起${policy === 'election' ? '选举' : '议会交权'}提名，组织 ${membership.organization_id}，提名 ${candidate.member_id}。`)}
                    disabled={!canPrepareAction}
                    aria-label={`把提名${candidate.name?.trim() || '该成员'}为领导的行动填入输入框`}
                    title={canPrepareAction ? '填入行动草稿；发送后由规则核验' : '清空输入框或等待当前请求完成后再操作'}
                  >提名 {candidate.name?.trim() || '成员'}</button>)}
                </div>}
              <p className="occupation-id">组织 ID · {membership.organization_id}</p>
            </article>
          })}
        </div>
      </section>}

      {membershipDataAvailable && vacancyNominations.length > 0 && <section aria-label="领导席位提名">
        <p className="shipment-empty">你可为领导席位明确提名；规则会核验提议人资格和表决结果。</p>
        <div className="shipment-list" aria-live="polite">
          {vacancyNominations.map((membership) => {
            const policy = membership.ballot_policy === 'election' ? 'election' : 'council'
            const candidates = (membership.vacancy_ballot_nominees ?? []).filter(isNominee)
            const existingOfficeHolder = candidates.find(
              (candidate) => candidate.member_id === membership.vacancy_ballot_existing_office_holder_id,
            )
            return <article className="shipment-card" key={membership.organization_id}>
              <header className="shipment-card-heading">
                <span className="shipment-status">{policyLabels[policy]}</span>
                <small>{membership.name?.trim() || '组织名称未提供'}</small>
              </header>
              {existingOfficeHolder && <p className="shipment-empty">
                现有领导职务由{existingOfficeHolder.name?.trim() || '该成员'}继续担任。本次表决定其是否成为组织领袖。
              </p>}
              {candidates.length === 0
                ? <p className="shipment-empty">当前没有可提名的在任成员。</p>
                : <div className="promise-actions" aria-label={`${membership.name?.trim() || '组织'}可提名领导人选`}>
                  {candidates.map((candidate) => {
                    const confirmsExistingHolder = candidate.member_id === existingOfficeHolder?.member_id
                    const name = candidate.name?.trim() || '成员'
                    return <button
                      type="button"
                      className="button button-secondary button-small"
                      key={candidate.member_id}
                      onClick={() => onPrepareAction(`我明确发起空缺领导席位提名，组织 ${membership.organization_id}，提名 ${candidate.member_id}。`)}
                      disabled={!canPrepareAction}
                      aria-label={confirmsExistingHolder
                        ? `提议确认${name}担任组织领袖`
                        : `提名${name}填补空缺领导席位`}
                      title={canPrepareAction
                        ? confirmsExistingHolder
                          ? '填入确认该成员担任组织领袖的提名草稿'
                          : '填入提名草稿；发送后由规则核验'
                        : '清空输入框或等待当前请求完成后再操作'}
                    >{confirmsExistingHolder ? `确认 ${name} 资格` : `提名 ${name}`}</button>
                  })}
                </div>}
              <p className="occupation-id">组织 ID · {membership.organization_id}</p>
            </article>
          })}
        </div>
      </section>}

      {ballotDataAvailable && ballots.length > 0 && <div className="shipment-list" aria-live="polite">
        {ballots.map((ballot) => {
          const status = ballotStatusLabels[ballot.status] ?? '状态暂不可识别'
          const policy = policyLabels[ballot.policy] ?? '领导表决'
          const revision = exactInteger(ballot.revision)
          const canAct = canPrepareAction && ballot.status === 'open' && revision !== null
          const waitingVoters = ballot.waiting_voters ?? []
          return <article className="shipment-card" key={ballot.ballot_id}>
            <header className="shipment-card-heading">
              <span className={`shipment-status ${ballot.status === 'rejected' || ballot.status === 'cancelled' ? 'shipment-status-spoiled' : ''}`}>{status}</span>
              <small>{policy}</small>
            </header>
            <dl className="shipment-parties">
              <div><dt>提名人选</dt><dd>{ballot.nominee_name?.trim() || '人选资料未载入'}</dd></div>
              <div><dt>我的身份</dt><dd>{ballot.is_incumbent ? '在任者' : ballot.is_vacancy_proposer ? '空缺提议人' : ballot.is_voter ? '选民' : '被提名人'}</dd></div>
              {ballot.is_voter && ballot.own_vote !== null && <div><dt>我的投票</dt><dd>{voteLabels[ballot.own_vote] ?? '无法读取'}</dd></div>}
              {ballot.is_voter && ballot.own_vote === null && ballot.status === 'open' && <div><dt>我的投票</dt><dd>尚未投票</dd></div>}
            </dl>
            <p className="occupation-id">表决 ID · {ballot.ballot_id}</p>

            {ballot.status === 'open' && ballot.is_voter && ballot.own_vote === null && <div className="promise-actions" aria-label="你的投票草稿">
              {(['yes', 'no', 'abstain'] as const).map((choice) => <button
                type="button"
                className="button button-secondary button-small"
                key={choice}
                onClick={() => onPrepareAction(`我明确投${choice === 'yes' ? '赞成票' : choice === 'no' ? '反对票' : '弃权票'}，表决 ${ballot.ballot_id}，版本 ${revision}。`)}
                disabled={!canAct}
                title={canPrepareAction ? '填入个人投票草稿；不会替其他选民投票' : '清空输入框或等待当前请求完成后再操作'}
              >准备{voteLabels[choice]}</button>)}
            </div>}

            {ballot.status === 'open' && (ballot.is_incumbent || ballot.is_vacancy_proposer) && <>
              {waitingVoters.length > 0
                ? <div className="ballot-waiting-list" aria-label="尚未投票的 NPC">
                  <span>尚未投票</span>
                  {waitingVoters.map((voter) => <button
                    type="button"
                    className="button button-secondary button-small"
                    key={voter.voter_id}
                    onClick={() => onPrepareAction(`我作为${ballot.is_incumbent ? '在任者' : '空缺提议人'}明确请 NPC ${voter.voter_id} 对表决 ${ballot.ballot_id} 独立投票，版本 ${revision}。`)}
                    disabled={!canAct}
                    aria-label={`请求${voter.name?.trim() || '该 NPC'}独立投票；不指定票向`}
                    title={canPrepareAction ? '只请求对方自行决定，不指定票向' : '清空输入框或等待当前请求完成后再操作'}
                  >请求 {voter.name?.trim() || 'NPC'} 投票</button>)}
                </div>
                : <p className="shipment-empty">目前没有待请求的 NPC 选民。</p>}
              <div className="promise-actions">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  onClick={() => onPrepareAction(`我明确要求结算表决 ${ballot.ballot_id}，版本 ${revision}。`)}
                  disabled={!canAct}
                  title={canPrepareAction ? '填入结算草稿；规则只会在结果可确认时结算' : '清空输入框或等待当前请求完成后再操作'}
                >准备结算表决</button>
              </div>
            </>}
          </article>
        })}
      </div>}
      {ballotDataAvailable && ballots.length > 0 && <p className="shipment-empty">草稿只会提交你明确选择的操作；其他选民的票向不会显示，也不会由此自动投票。</p>}
      {!ballotDataAvailable && membershipDataAvailable && nominations.length > 0 && <p className="shipment-empty">当前投影未提供既有表决记录。</p>}
      {actionableCount > 0 && !canPrepareAction && <p className="shipment-empty" role="status">清空输入框并等待当前请求完成后，才可准备表决行动。</p>}
    </>}
  </details>
}

function isMembership(value: unknown): value is OwnMembership {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const membership = value as Partial<OwnMembership>
  return typeof membership.organization_id === 'string'
    && membership.organization_id.trim().length > 0
    && typeof membership.is_leader === 'boolean'
    && (membership.name === undefined || membership.name === null || typeof membership.name === 'string')
    && (membership.ballot_nominees === undefined || membership.ballot_nominees === null || Array.isArray(membership.ballot_nominees))
    && (membership.vacancy_ballot_nominees === undefined || membership.vacancy_ballot_nominees === null || Array.isArray(membership.vacancy_ballot_nominees))
    && (membership.vacancy_ballot_existing_office_holder_id === undefined
      || typeof membership.vacancy_ballot_existing_office_holder_id === 'string')
}

function isNominee(value: unknown): value is { member_id: string; name?: string | null } {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const nominee = value as { member_id?: unknown; name?: unknown }
  return typeof nominee.member_id === 'string' && nominee.member_id.trim().length > 0
    && (nominee.name === undefined || nominee.name === null || typeof nominee.name === 'string')
}

function isBallot(value: unknown): value is OwnLeadershipBallot {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const ballot = value as Partial<OwnLeadershipBallot>
  return typeof ballot.ballot_id === 'string' && ballot.ballot_id.trim().length > 0
    && typeof ballot.organization_id === 'string' && typeof ballot.nominee_id === 'string'
    && typeof ballot.status === 'string' && typeof ballot.policy === 'string'
    && (typeof ballot.revision === 'string' || typeof ballot.revision === 'number')
    && typeof ballot.is_incumbent === 'boolean' && typeof ballot.is_voter === 'boolean'
    && (ballot.own_vote === null || ballot.own_vote === 'yes' || ballot.own_vote === 'no' || ballot.own_vote === 'abstain')
    && (ballot.is_vacancy_proposer === undefined || typeof ballot.is_vacancy_proposer === 'boolean')
    && (ballot.waiting_voters === undefined || Array.isArray(ballot.waiting_voters) && ballot.waiting_voters.every(isWaitingVoter))
}

function isWaitingVoter(value: unknown): value is { voter_id: string; name?: string | null } {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const voter = value as { voter_id?: unknown; name?: unknown }
  return typeof voter.voter_id === 'string' && voter.voter_id.trim().length > 0
    && (voter.name === undefined || voter.name === null || typeof voter.name === 'string')
}

function exactInteger(value: string | number): string | null {
  if (typeof value === 'string') return /^[1-9]\d*$/.test(value) ? value : null
  return Number.isSafeInteger(value) && value >= 1 ? String(value) : null
}
