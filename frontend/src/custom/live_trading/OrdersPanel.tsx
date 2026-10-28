/**
 * 订单面板 — 列表 + 提交表单 + 撤单。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Send } from 'lucide-react'
import { api, type LiveOrder } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'

const STATE_LABEL: Record<string, string> = {
  created: '已创建',
  risk_passed: '风控通过',
  approved: '已审批',
  submitted: '已提交',
  partially_filled: '部分成交',
  filled: '已成交',
  cancelled: '已撤单',
  rejected: '已拒单',
  expired: '已过期',
  unknown: '未知',
}
const STATE_COLOR: Record<string, string> = {
  created: 'text-zinc-500',
  risk_passed: 'text-blue-500',
  approved: 'text-amber-500',
  submitted: 'text-blue-500',
  partially_filled: 'text-amber-500',
  filled: 'text-emerald-500',
  cancelled: 'text-zinc-500',
  rejected: 'text-red-500',
  expired: 'text-zinc-500',
  unknown: 'text-red-500',
}

export function OrdersPanel({ account_id }: { account_id: string | null }) {
  const qc = useQueryClient()
  const { data } = useQuery({
    queryKey: QK.liveOrders(account_id ?? ''),
    queryFn: () => api.liveOrders(undefined, account_id ?? undefined),
  })
  const orders: LiveOrder[] = data?.orders ?? []
  const [submitOpen, setSubmitOpen] = useState(false)

  const cancelM = useMutation({
    mutationFn: (id: string) => api.liveCancelOrder(id),
    onSettled: () => qc.invalidateQueries({ queryKey: QK.liveOrders(account_id ?? '') }),
  })

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button
          onClick={() => setSubmitOpen(true)}
          disabled={!account_id}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
        >
          <Send size={14} />
          提交交易意图
        </button>
      </div>

      {orders.length === 0 ? (
        <div className="text-sm text-muted-foreground">暂无订单</div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="px-3 py-1.5 text-left">标的</th>
                <th className="px-3 py-1.5 text-left">方向</th>
                <th className="px-3 py-1.5 text-right">数量</th>
                <th className="px-3 py-1.5 text-right">限价</th>
                <th className="px-3 py-1.5 text-left">状态</th>
                <th className="px-3 py-1.5 text-right">已成</th>
                <th className="px-3 py-1.5 text-left">券商订单号</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const cancellable = ['approved', 'submitted', 'partially_filled'].includes(o.state)
                return (
                  <tr key={o.order_id} className="border-b border-border/50">
                    <td className="px-3 py-1.5">{o.symbol}</td>
                    <td className={`px-3 py-1.5 ${o.side === 'buy' ? 'text-red-500' : 'text-emerald-500'}`}>{o.side === 'buy' ? '买入' : '卖出'}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{o.qty}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{o.limit_price != null ? o.limit_price.toFixed(2) : '--'}</td>
                    <td className={`px-3 py-1.5 ${STATE_COLOR[o.state] || ''}`}>{STATE_LABEL[o.state] || o.state}</td>
                    <td className="px-3 py-1.5 text-right tabular-nums">{o.filled_qty}</td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">{o.broker_order_id ?? '--'}</td>
                    <td className="px-3 py-1.5 text-right">
                      {cancellable && (
                        <button
                          onClick={() => cancelM.mutate(o.order_id)}
                          className="rounded-btn border border-border px-2 py-0.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                        >
                          撤单
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {submitOpen && account_id && (
        <SubmitIntentModal
          account_id={account_id}
          onClose={() => setSubmitOpen(false)}
          onSubmitted={() => qc.invalidateQueries({ queryKey: QK.liveOrders(account_id) })}
        />
      )}
    </div>
  )
}

function SubmitIntentModal({
  account_id,
  onClose,
  onSubmitted,
}: {
  account_id: string
  onClose: () => void
  onSubmitted: () => void
}) {
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState(100)
  const [limitPrice, setLimitPrice] = useState<string>('')
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const submitM = useMutation({
    mutationFn: () =>
      api.liveSubmitOrder({
        account_id,
        symbol,
        side,
        qty,
        price_policy: 'limit',
        limit_price: limitPrice ? Number(limitPrice) : null,
        source: 'manual',
      }),
    onSuccess: (r) => {
      setResult(`意图 ${r.intent_id} | 状态: ${r.status}${r.reasons?.length ? ' | 原因: ' + r.reasons.join('; ') : ''}`)
      onSubmitted()
    },
    onError: (e: unknown) => setError(String(e)),
  })

  const INPUT_CLS = 'mt-1 w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm outline-none transition-colors focus:border-accent/50'
  const LABEL_CLS = 'block text-[11px] text-muted'

  return (
    <Modal onClose={onClose} labelledBy="submit-intent-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <Send className="h-4 w-4 text-accent" />
          <h3 id="submit-intent-title" className="text-sm font-semibold text-foreground">提交交易意图</h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          提交后进入待审批队列, 需人工逐笔确认才创建订单。AI 仅到此 (L1 建议)。
        </p>
        <div className="mt-4 space-y-3">
          <div>
            <label className={LABEL_CLS}>证券代码</label>
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="600519.SH"
              className={`${INPUT_CLS} font-mono`}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className={LABEL_CLS}>方向</label>
              <select value={side} onChange={(e) => setSide(e.target.value as 'buy' | 'sell')} className={INPUT_CLS}>
                <option value="buy">买入</option>
                <option value="sell">卖出</option>
              </select>
            </div>
            <div>
              <label className={LABEL_CLS}>数量</label>
              <input type="number" value={qty} onChange={(e) => setQty(Number(e.target.value))} className={`${INPUT_CLS} font-mono`} />
            </div>
          </div>
          <div>
            <label className={LABEL_CLS}>限价</label>
            <input value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} placeholder="1500.00" className={`${INPUT_CLS} font-mono`} />
          </div>
        </div>
        {result && <div className="mt-3 rounded-btn bg-accent/15 px-2.5 py-1.5 text-[11px] text-secondary">{result}</div>}
        {error && <div className="mt-3 rounded-btn bg-danger/10 px-2.5 py-1.5 text-[11px] text-danger">{error}</div>}
        <div className="mt-5 flex gap-2">
          <button
            onClick={() => submitM.mutate()}
            disabled={submitM.isPending || !symbol}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {submitM.isPending ? '提交中…' : '提交'}
          </button>
          <button
            onClick={onClose}
            className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            关闭
          </button>
        </div>
      </div>
    </Modal>
  )
}