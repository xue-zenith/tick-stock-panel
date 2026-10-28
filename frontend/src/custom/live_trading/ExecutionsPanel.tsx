/**
 * 成交明细面板 — 查询 /api/live/executions, 支持 account 过滤 + 虚拟滚动。
 *
 * 复用 format.ts 的 fmtPrice/priceColorClass/cnDateTimeFromUtc;
 * 行数 > VIRTUAL_LIST_THRESHOLD (100) 时启用 @tanstack/react-virtual,
 * 镜像 StockDataTable.tsx 的虚拟化模式。
 */
import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useVirtualizer } from '@tanstack/react-virtual'
import { VIRTUAL_LIST_THRESHOLD, useParentScroll } from '@/components/virtual-list/useParentScroll'
import { api, type LiveExecution } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPrice, cnDateTimeFromUtc } from '@/lib/format'
import { cn } from '@/lib/cn'

const COLS = ['时间', '证券', '方向', '数量', '价格', '手续费', '成交号'] as const

export function ExecutionsPanel({ account_id }: { account_id: string | null }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const { data, isLoading, error } = useQuery({
    enabled: !!account_id,
    queryKey: QK.liveExecutions(account_id ?? ''),
    queryFn: () => api.liveExecutions(account_id ?? undefined),
  })
  const rows: LiveExecution[] = data?.executions ?? []

  const virtualized = rows.length > VIRTUAL_LIST_THRESHOLD
  const { getScrollElement, scrollMargin } = useParentScroll(containerRef, virtualized)
  const rowVirtualizer = useVirtualizer({
    count: virtualized ? rows.length : 0,
    getScrollElement,
    estimateSize: () => 36,
    overscan: 10,
    scrollMargin,
  })
  const virtualRows = virtualized ? rowVirtualizer.getVirtualItems() : []

  if (!account_id) return <div className="text-sm text-muted-foreground">请先选择账户</div>
  if (isLoading) return <div className="text-sm text-muted-foreground">加载中...</div>
  if (error) return <div className="text-sm text-danger">加载失败: {String(error)}</div>
  if (rows.length === 0) return <div className="text-sm text-muted-foreground">暂无成交</div>

  const renderRow = (ex: LiveExecution) => (
    <tr key={ex.exec_id} className="border-b border-border/50">
      <td className="px-3 py-1.5 text-xs text-muted-foreground tabular-nums">
        {cnDateTimeFromUtc(ex.ts) || ex.ts}
      </td>
      <td className="px-3 py-1.5 font-mono">{ex.symbol}</td>
      <td className={cn('px-3 py-1.5', ex.side === 'buy' ? 'text-bull' : 'text-bear')}>
        {ex.side === 'buy' ? '买入' : '卖出'}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums">{ex.qty}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{fmtPrice(ex.price)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">{fmtPrice(ex.fee)}</td>
      <td className="px-3 py-1.5 text-xs text-muted-foreground">{ex.broker_trade_id}</td>
    </tr>
  )

  return (
    <div ref={containerRef} className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            {COLS.map((c) => (
              <th key={c} className="px-3 py-1.5 text-left first:text-left last:text-left">
                {c === '数量' || c === '价格' || c === '手续费' ? (
                  <span className="block text-right">{c}</span>
                ) : c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {virtualized ? (
            virtualRows.map((vr) => (
              <tr
                key={rows[vr.index].exec_id}
                className="border-b border-border/50"
                style={{ height: `${vr.size}px`, transform: `translateY(${vr.start - scrollMargin}px)` }}
              >
                {virtualized && (
                  <>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground tabular-nums">
                      {cnDateTimeFromUtc(rows[vr.index].ts) || rows[vr.index].ts}
                    </td>
                    <td className="px-3 py-1.5 font-mono">{rows[vr.index].symbol}</td>
                    <td className={cn('px-3 py-1.5', rows[vr.index].side === 'buy' ? 'text-bull' : 'text-bear')}>
                      {rows[vr.index].side === 'buy' ? '买入' : '卖出'}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{rows[vr.index].qty}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{fmtPrice(rows[vr.index].price)}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                      {fmtPrice(rows[vr.index].fee)}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">
                      {rows[vr.index].broker_trade_id}
                    </td>
                  </>
                )}
              </tr>
            ))
          ) : (
            rows.map(renderRow)
          )}
        </tbody>
      </table>
      {virtualized && (
        <div style={{ height: `${rowVirtualizer.getTotalSize()}px` }} />
      )}
    </div>
  )
}
