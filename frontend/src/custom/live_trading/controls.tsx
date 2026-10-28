/**
 * 共享控件 — 模式徽标、Arm/Disarm 按钮。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, Snowflake, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function ModeBadge({ mode }: { mode: string }) {
  const labels: Record<string, string> = {
    disarmed: '已停用',
    armed_manual: '已激活(人工)',
    armed_auto: '已激活(自动)',
    degraded: '降级',
    halted: '已熔断',
  }
  const colors: Record<string, string> = {
    disarmed: 'bg-zinc-500/15 text-zinc-600 dark:text-zinc-400',
    armed_manual: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
    armed_auto: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
    degraded: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
    halted: 'bg-red-500/15 text-red-600 dark:text-red-400',
  }
  return (
    <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${colors[mode] || colors.disarmed}`}>
      {labels[mode] || mode}
    </span>
  )
}

export function ArmDisarmButtons({ account_id, mode, disabled = false }: { account_id: string; mode?: string; disabled?: boolean }) {
  const qc = useQueryClient()
  const current = mode ?? 'disarmed'
  const is_armed = current === 'armed_manual' || current === 'armed_auto'

  const armM = useMutation({
    mutationFn: () => api.liveArm(account_id),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.liveHealth(account_id) }),
  })
  const disarmM = useMutation({
    mutationFn: () => api.liveDisarm(account_id),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.liveHealth(account_id) }),
  })

  if (is_armed) {
    return (
      <div className="flex items-center gap-2">
        {disarmM.isError && <span title={String(disarmM.error)}><AlertCircle size={14} className="text-danger" /></span>}
        <button
          onClick={() => disarmM.mutate()}
          disabled={disarmM.isPending}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-base px-3 py-1.5 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground"
        >
          <Snowflake size={14} />
          停止新单
        </button>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      {armM.isError && <span title={String(armM.error)}><AlertCircle size={14} className="text-danger" /></span>}
      <button
        onClick={() => armM.mutate()}
        disabled={armM.isPending || disabled}
        title={disabled ? '需先连接券商并完成干净对账' : undefined}
        className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <ShieldCheck size={14} />
        启用交易
      </button>
    </div>
  )
}
