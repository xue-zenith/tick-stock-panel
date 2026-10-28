/**
 * 健康/对账面板 — TradingMode、对账历史、系统事件流。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, AlertTriangle } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function HealthPanel({ account_id }: { account_id: string | null }) {
  const qc = useQueryClient()
  const healthQ = useQuery({
    enabled: !!account_id,
    queryKey: QK.liveHealth(account_id ?? ''),
    queryFn: () => api.liveHealth(account_id!),
  })
  const reconQ = useQuery({
    queryKey: QK.liveReconciliation,
    queryFn: () => api.liveReconciliation(account_id ?? undefined),
  })
  const schedQ = useQuery({
    queryKey: ['live', 'scheduler'],
    queryFn: () => api.liveScheduler(),
    staleTime: 15_000,
  })

  const reconM = useMutation({
    mutationFn: () => api.liveTriggerReconciliation(account_id!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.liveReconciliation })
      qc.invalidateQueries({ queryKey: QK.liveHealth(account_id ?? '') })
    },
  })
  const runNowM = useMutation({
    mutationFn: () => api.liveSchedulerRunNow(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['live', 'scheduler'] })
      qc.invalidateQueries({ queryKey: QK.liveReconciliation })
    },
  })

  if (!account_id) return <div className="text-sm text-muted-foreground">请先选择账户</div>

  const unresolved = healthQ.data?.unresolved_diffs ?? 0
  const runs = reconQ.data?.runs ?? []
  const diffs = reconQ.data?.diffs ?? []
  const events = healthQ.data?.recent_events ?? []
  const sched = schedQ.data
  const lastOrders = sched?.last_orders_recon?.[account_id]
  const lastPositions = sched?.last_positions_recon?.[account_id]

  return (
    <div className="space-y-4">
      {/* 调度器状态卡 */}
      <div className="rounded-md border border-border bg-surface p-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-medium">对账调度器</h3>
          <button
            onClick={() => runNowM.mutate()}
            disabled={runNowM.isPending || !sched?.running}
            className="rounded border border-border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-50"
          >
            {runNowM.isPending ? '执行中...' : '立即对账'}
          </button>
        </div>
        {sched?.running ? (
          <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
            <div>
              <div className="text-muted-foreground">订单对账频率</div>
              <div className="mt-0.5 font-medium tabular-nums">{sched.orders_interval_s}s</div>
            </div>
            <div>
              <div className="text-muted-foreground">持仓对账频率</div>
              <div className="mt-0.5 font-medium tabular-nums">{sched.positions_interval_s}s</div>
            </div>
            <div>
              <div className="text-muted-foreground">日终对账</div>
              <div className="mt-0.5 font-medium">{sched.daily_at}</div>
            </div>
            <div>
              <div className="text-muted-foreground">已注册 broker</div>
              <div className="mt-0.5 font-medium">{(sched.registered_brokers ?? []).join(', ') || '无'}</div>
            </div>
            <div>
              <div className="text-muted-foreground">最近订单对账</div>
              <div className="mt-0.5 tabular-nums">{lastOrders ?? '未执行'}</div>
            </div>
            <div>
              <div className="text-muted-foreground">最近持仓对账</div>
              <div className="mt-0.5 tabular-nums">{lastPositions ?? '未执行'}</div>
            </div>
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">调度器未启动 (后台对账不可用, 请手动对账)</div>
        )}
      </div>

      <div className="flex items-center gap-3">
        <div className="rounded-md border border-border bg-surface p-3">
          <div className="text-xs text-muted-foreground">未解决差异</div>
          <div className={`mt-1 text-xl font-semibold tabular-nums ${unresolved > 0 ? 'text-red-500' : 'text-emerald-500'}`}>
            {unresolved}
          </div>
        </div>
        <button
          onClick={() => reconM.mutate()}
          disabled={reconM.isPending}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-accent"
        >
          <RefreshCw size={14} className={reconM.isPending ? 'animate-spin' : ''} />
          手动对账 (本账户)
        </button>
      </div>

      {unresolved > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-600 dark:text-amber-400">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <div>
            存在 {unresolved} 处未解决对账差异, 账户已进入 DEGRADED, 仅允许受控减仓 (卖出)。
            请先排查差异或手动对账恢复后再启用交易。
          </div>
        </div>
      )}

      <div>
        <h3 className="mb-2 text-sm font-medium">最近对账</h3>
        {runs.length === 0 ? (
          <div className="text-sm text-muted-foreground">暂无对账记录</div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-3 py-1.5 text-left">开始</th>
                  <th className="px-3 py-1.5 text-left">结束</th>
                  <th className="px-3 py-1.5 text-left">状态</th>
                  <th className="px-3 py-1.5 text-right">差异数</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id} className="border-b border-border/50">
                    <td className="px-3 py-1.5 text-xs">{r.started_at}</td>
                    <td className="px-3 py-1.5 text-xs">{r.finished_at ?? '运行中'}</td>
                    <td className={`px-3 py-1.5 ${r.status === 'clean' ? 'text-emerald-500' : r.status === 'dirty' ? 'text-red-500' : 'text-amber-500'}`}>
                      {r.status}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{r.diff_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {diffs.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-medium">最近差异</h3>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-3 py-1.5 text-left">类型</th>
                  <th className="px-3 py-1.5 text-left">本地</th>
                  <th className="px-3 py-1.5 text-left">券商</th>
                  <th className="px-3 py-1.5 text-left">已解决</th>
                </tr>
              </thead>
              <tbody>
                {diffs.map((d) => (
                  <tr key={d.diff_id} className="border-b border-border/50">
                    <td className="px-3 py-1.5">{d.diff_kind}</td>
                    <td className="px-3 py-1.5 text-xs">{d.local_value ?? '--'}</td>
                    <td className="px-3 py-1.5 text-xs">{d.broker_value ?? '--'}</td>
                    <td className="px-3 py-1.5">{d.resolved ? '是' : '否'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div>
        <h3 className="mb-2 text-sm font-medium">系统事件</h3>
        {events.length === 0 ? (
          <div className="text-sm text-muted-foreground">无事件</div>
        ) : (
          <div className="max-h-64 overflow-y-auto rounded-md border border-border">
            <table className="w-full text-xs">
              <tbody>
                {events.map((e) => (
                  <tr key={e.event_id} className="border-b border-border/50">
                    <td className="px-3 py-1.5 text-muted-foreground">{e.ts}</td>
                    <td className="px-3 py-1.5">{e.kind}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{e.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}