type RuntimeProfile = { speed: string; depth: string }

export function RuntimeProfileStatus({ profile, loading, error, onRetry }: {
  profile: RuntimeProfile | null
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  return <span className="profile-chip" role={error && !loading ? 'status' : undefined}>
    {loading ? '读取配置…' : profile
      ? `${profile.speed} · ${profile.depth}`
      : <><span>发送已停用 · {error ?? '无法读取运行配置'}</span> <button type="button" className="text-button" onClick={onRetry}>重试配置读取</button></>}
  </span>
}
