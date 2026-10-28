/**
 * 账户概览 — 资金 + 持仓快照 + 净值/收益率。
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { fmtPrice, priceColorClass } from '@/lib/format'

export function AccountOverview({ account_id }: { account_id: string | null }) {
  const { data, isLoading, error } = useQuery({
    enabled: !!account_id,
    queryKey: ['live', 'detail', account_id],
    queryFn: () => api.liveAccountDetail(account_id!),
  })

  if (!account_id) return <div className="text-sm text-muted-foreground">请先创建账户</div>
  if (isLoading) return <div className="text-sm text-muted-foreground">加载中...</div>
  if (error) return <div className="text-sm text-danger">加载失败: {String(error)}</div>
  if (!data) return <div className="text-sm text-muted-foreground">无数据</div>

  const snap = data.snapshot
  const fmt = (n: number | undefined | null) => fmtPrice(n)
  const nav = snap?.nav ?? snap?.total ?? 0
  const total = snap?.total ?? 0
  const pnl = nav - total
  const pnlPct = total > 0 ? (pnl / total) : 0

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="可用现金" value={fmt(snap?.cash)} />
        <StatCard label="冻结资金" value={fmt(snap?.frozen_cash)} />
        <StatCard label="持仓市值" value={fmt(snap?.market_value)} />
        <StatCard label="总资产" value={fmt(snap?.total)} />
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="净值" value={fmtPrice(nav)} />
        <StatCard label="累计盈亏" value={fmtPrice(pnl)} valueClass={priceColorClass(pnl)} />
        <StatCard label="收益率" value={`${pnlPct >= 0 ? '+' : ''}${(pnlPct * 100).toFixed(2)}%`} valueClass={priceColorClass(pnl)} />
        <StatCard label="快照时间" value={snap?.as_of ? new Date(snap.as_of).toLocaleString('zh-CN') : '--'} />
      </div>

      <div>
        <h3 className="mb-2 text-sm font-medium">持仓</h3>
        {data.positions.length === 0 ? (
          <div className="text-sm text-muted-foreground">暂无持仓</div>
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
                  <tr key={p.symbol} className="border-b border-border/50">
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

function StatCard({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-md border border-border bg-surface p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${valueClass ?? ''}`}>{value}</div>
    </div>
  )
}