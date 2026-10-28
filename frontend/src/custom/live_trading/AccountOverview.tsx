/**
 * 账户概览 — 资金 + 持仓快照 + 净值/收益率。
 */
import { useQuery } from '@tanstack/react-query'
import { Clock3, Landmark, PieChart, WalletCards } from 'lucide-react'
import { api } from '@/lib/api'
import { fmtPrice, priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'

export function AccountOverview({ account_id }: { account_id: string | null }) {
  const { data, isLoading, error } = useQuery({
    enabled: !!account_id,
    queryKey: QK.liveAccountDetail(account_id ?? ''),
    queryFn: () => api.liveAccountDetail(account_id!),
  })

  if (!account_id) return <div className="text-sm text-muted-foreground">请先创建账户</div>
  if (isLoading) return <div className="text-sm text-muted-foreground">加载中...</div>
  if (error) return <div className="text-sm text-danger">加载失败: {String(error)}</div>
  if (!data) return <div className="text-sm text-muted-foreground">无数据</div>

  const snap = data.snapshot
  const fmt = (n: number | undefined | null) => fmtPrice(n)
  const total = snap?.total ?? 0
  const cashRatio = total > 0 ? (snap?.cash ?? 0) / total : 0

  if (!snap) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface/50 px-5 py-10 text-center">
        <WalletCards className="mx-auto h-8 w-8 text-muted" />
        <div className="mt-3 text-sm font-medium text-foreground">尚无券商资金快照</div>
        <div className="mt-1 text-xs text-muted">请先连接券商，并在「健康 / 对账」执行一次真实对账。</div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <StatCard icon={Landmark} label="总资产" value={fmt(snap.total)} primary />
        <StatCard icon={WalletCards} label="可用现金" value={fmt(snap.cash)} sub={`现金占比 ${(cashRatio * 100).toFixed(1)}%`} />
        <StatCard icon={PieChart} label="持仓市值" value={fmt(snap.market_value)} sub={`冻结 ${fmt(snap.frozen_cash)}`} />
        <StatCard icon={Clock3} label="券商快照" value={new Date(snap.as_of).toLocaleString('zh-CN')} sub={`${data.positions.length} 个持仓标的`} compact />
      </div>

      <div>
        <div className="mb-2 flex items-end justify-between">
          <div><h3 className="text-sm font-medium">持仓明细</h3><p className="mt-0.5 text-[11px] text-muted">数量与可卖数量以券商快照为准</p></div>
          <span className="rounded-full bg-elevated px-2 py-1 text-[10px] text-muted">{data.positions.length} 项</span>
        </div>
        {data.positions.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border py-10 text-center text-sm text-muted-foreground">当前账户为空仓</div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="px-3 py-1.5 text-left">证券</th>
                  <th className="px-3 py-1.5 text-right">持仓</th>
                  <th className="px-3 py-1.5 text-right">可卖</th>
                  <th className="px-3 py-1.5 text-right">成本</th>
                  <th className="px-3 py-1.5 text-right">现价</th>
                  <th className="px-3 py-1.5 text-right">盈亏</th>
                </tr>
              </thead>
              <tbody>
                {data.positions.map((p) => (
                  <tr key={p.symbol} className="border-b border-border/50 transition-colors hover:bg-elevated/40">
                    <td className="px-3 py-1.5 font-mono">{p.symbol}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{p.qty}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{p.available_qty}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{fmt(p.avg_cost)}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{fmt(p.last_price)}</td>
                    <td className={`px-3 py-1.5 text-right tabular-nums ${priceColorClass(p.pnl)}`}>
                      {fmt(p.pnl)}
                    </td>
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

function StatCard({ icon: Icon, label, value, sub, primary = false, compact = false }: { icon: typeof Landmark; label: string; value: string; sub?: string; primary?: boolean; compact?: boolean }) {
  return (
    <div className={`rounded-xl border p-4 ${primary ? 'border-accent/25 bg-gradient-to-br from-accent/10 to-surface' : 'border-border bg-surface'}`}>
      <div className="flex items-center gap-2 text-xs text-muted-foreground"><Icon size={13} className={primary ? 'text-accent' : ''} />{label}</div>
      <div className={`mt-2 font-semibold tabular-nums text-foreground ${compact ? 'text-sm' : 'text-xl'}`}>{value}</div>
      {sub && <div className="mt-1 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}
