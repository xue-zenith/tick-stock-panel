/**
 * 待审批队列 — AI/策略产生的 TradeIntent 需人工逐笔确认 (L1 建议模式)。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, X } from 'lucide-react'
import { api, type LiveApprovalRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function ApprovalsPanel() {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: QK.liveApprovals, queryFn: api.liveApprovals })
  const approvals: LiveApprovalRow[] = data?.approvals ?? []

  const approveM = useMutation({
    mutationFn: (id: string) => api.liveApprove(id),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.liveApprovals })
      qc.invalidateQueries({ queryKey: ['live'] })
    },
  })
  const rejectM = useMutation({
    mutationFn: (id: string) => api.liveReject(id),
    onSettled: () => qc.invalidateQueries({ queryKey: QK.liveApprovals }),
  })

  if (approvals.length === 0) {
    return <div className="text-sm text-muted-foreground">无待审批意图</div>
  }

  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            <th className="px-3 py-1.5 text-left">标的</th>
            <th className="px-3 py-1.5 text-left">方向</th>
            <th className="px-3 py-1.5 text-right">数量</th>
            <th className="px-3 py-1.5 text-right">限价</th>
            <th className="px-3 py-1.5 text-left">来源</th>
            <th className="px-3 py-1.5 text-left">理由</th>
            <th className="px-3 py-1.5 text-left">到期</th>
            <th className="px-3 py-1.5"></th>
          </tr>
        </thead>
        <tbody>
          {approvals.map((a) => (
            <tr key={a.approval_id} className="border-b border-border/50">
              <td className="px-3 py-1.5">{a.symbol}</td>
              <td className={`px-3 py-1.5 ${a.side === 'buy' ? 'text-red-500' : 'text-emerald-500'}`}>
                {a.side === 'buy' ? '买入' : '卖出'}
              </td>
              <td className="px-3 py-1.5 text-right tabular-nums">{a.qty}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">
                {a.limit_price != null ? a.limit_price.toFixed(2) : '--'}
              </td>
              <td className="px-3 py-1.5 text-xs">{a.source}</td>
              <td className="px-3 py-1.5 text-xs text-muted-foreground">{a.rationale ?? '--'}</td>
              <td className="px-3 py-1.5 text-xs text-muted-foreground">{a.expires_at}</td>
              <td className="px-3 py-1.5">
                <div className="flex gap-1">
                  <button
                    onClick={() => approveM.mutate(a.approval_id)}
                    disabled={approveM.isPending}
                    className="rounded-btn bg-emerald-500 p-1.5 text-white transition-opacity hover:bg-emerald-600 disabled:opacity-50"
                    title="审批通过 → 创建订单"
                  >
                    <Check size={12} />
                  </button>
                  <button
                    onClick={() => rejectM.mutate(a.approval_id)}
                    disabled={rejectM.isPending}
                    className="rounded-btn bg-danger p-1.5 text-white transition-opacity hover:bg-red-600 disabled:opacity-50"
                    title="拒绝"
                  >
                    <X size={12} />
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}