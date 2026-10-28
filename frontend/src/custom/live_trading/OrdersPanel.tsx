/**
 * 订单面板 — 列表 + 提交表单 + 撤单 + 行展开 (事件 + 成交明细)。
 */
import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Send, ChevronRight } from 'lucide-react'
import { api, type LiveOrder, type LiveOrderEvent, type LiveExecution } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { fmtPrice, cnDateTimeFromUtc } from '@/lib/format'
import { cn } from '@/lib/cn'

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
    enabled: !!account_id,
    queryKey: QK.liveOrders(account_id ?? ''),
    queryFn: () => api.liveOrders(undefined, account_id ?? undefined),
  })
  const orders: LiveOrder[] = data?.orders ?? []
  const [submitOpen, setSubmitOpen] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)

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

      {cancelM.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">撤单失败：{String(cancelM.error)}</div>
      )}

      {orders.length === 0 ? (
        <div className="text-sm text-muted-foreground">暂无订单</div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="px-2 py-1.5 w-8"></th>
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
                const expanded = expandedId === o.order_id
                return (
                  <OrderRow
                    key={o.order_id}
                    order={o}
                    cancellable={cancellable}
                    expanded={expanded}
                    onToggle={() => setExpandedId(expanded ? null : o.order_id)}
                    onCancel={() => cancelM.mutate(o.order_id)}
                  />
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
          onSubmitted={() => {
            qc.invalidateQueries({ queryKey: QK.liveOrders(account_id) })
            qc.invalidateQueries({ queryKey: QK.liveApprovals })
          }}
        />
      )}
    </div>
  )
}

function OrderRow({
  order: o,
  cancellable,
  expanded,
  onToggle,
  onCancel,
}: {
  order: LiveOrder
  cancellable: boolean
  expanded: boolean
  onToggle: () => void
  onCancel: () => void
}) {
  return (
    <>
      <tr className="border-b border-border/50">
        <td className="px-2 py-1.5">
          <button
            onClick={onToggle}
            className="text-muted-foreground transition-colors hover:text-foreground"
          >
            <ChevronRight size={14} className={cn('transition-transform', expanded && 'rotate-90')} />
          </button>
        </td>
        <td className="px-3 py-1.5 font-mono">{o.symbol}</td>
        <td className={cn('px-3 py-1.5', o.side === 'buy' ? 'text-bull' : 'text-bear')}>
          {o.side === 'buy' ? '买入' : '卖出'}
        </td>
        <td className="px-3 py-1.5 text-right tabular-nums">{o.qty}</td>
        <td className="px-3 py-1.5 text-right tabular-nums">{fmtPrice(o.limit_price)}</td>
        <td className={cn('px-3 py-1.5', STATE_COLOR[o.state] || '')}>{STATE_LABEL[o.state] || o.state}</td>
        <td className="px-3 py-1.5 text-right tabular-nums">{o.filled_qty}</td>
        <td className="px-3 py-1.5 text-xs text-muted-foreground">{o.broker_order_id ?? '--'}</td>
        <td className="px-3 py-1.5 text-right">
          {cancellable && (
            <button
              onClick={onCancel}
              className="rounded-btn border border-border px-2 py-0.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
            >
              撤单
            </button>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-border/50 bg-surface/50">
          <td colSpan={9} className="px-4 py-2">
            <OrderDetail order_id={o.order_id} />
          </td>
        </tr>
      )}
    </>
  )
}

function OrderDetail({ order_id }: { order_id: string }) {
  const { data: eventsData } = useQuery({
    queryKey: ['live', 'order-events', order_id],
    queryFn: () => api.liveOrderEvents(order_id),
  })
  const { data: execsData } = useQuery({
    queryKey: ['live', 'order-executions', order_id],
    queryFn: () => api.liveOrderExecutions(order_id),
  })
  const events: LiveOrderEvent[] = eventsData?.events ?? []
  const execs: LiveExecution[] = execsData?.executions ?? []
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div>
        <h4 className="mb-1 text-xs font-medium text-muted-foreground">状态事件</h4>
        {events.length === 0 ? (
          <div className="text-xs text-muted-foreground">无</div>
        ) : (
          <div className="max-h-40 overflow-y-auto">
            {events.map((e) => (
              <div key={e.event_id} className="flex gap-2 py-0.5 text-xs">
                <span className="text-muted-foreground tabular-nums">{cnDateTimeFromUtc(e.ts) || e.ts}</span>
                <span className={cn(e.from_state && STATE_COLOR[e.from_state])}>{STATE_LABEL[e.from_state ?? ''] ?? e.from_state}</span>
                <span className="text-muted-foreground">→</span>
                <span className={cn(STATE_COLOR[e.to_state])}>{STATE_LABEL[e.to_state] ?? e.to_state}</span>
                {e.reason && <span className="text-muted-foreground">{e.reason}</span>}
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <h4 className="mb-1 text-xs font-medium text-muted-foreground">成交明细</h4>
        {execs.length === 0 ? (
          <div className="text-xs text-muted-foreground">无</div>
        ) : (
          <div className="max-h-40 overflow-y-auto">
            {execs.map((ex) => (
              <div key={ex.exec_id} className="flex gap-2 py-0.5 text-xs tabular-nums">
                <span className="text-muted-foreground">{cnDateTimeFromUtc(ex.ts) || ex.ts}</span>
                <span>{ex.qty}@{fmtPrice(ex.price)}</span>
                <span className="text-muted-foreground">{ex.broker_trade_id}</span>
              </div>
            ))}
          </div>
        )}
      </div>
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
  const [pricePolicy, setPricePolicy] = useState<'limit' | 'next_open' | 'close'>('next_open')
  const [limitPrice, setLimitPrice] = useState<string>('')
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const dedupeKey = useRef(`ui_${crypto.randomUUID()}`)

  const submitM = useMutation({
    mutationFn: () =>
      api.liveSubmitOrder({
        account_id,
        symbol: symbol.trim().toUpperCase(),
        side,
        qty,
        price_policy: pricePolicy,
        limit_price: limitPrice ? Number(limitPrice) : null,
        source: 'manual',
        dedupe_key: dedupeKey.current,
      }),
    onSuccess: (r) => {
      setResult(`意图 ${r.intent_id} | 状态: ${r.status}${r.reasons?.length ? ' | 原因: ' + r.reasons.join('; ') : ''}`)
      onSubmitted()
    },
    onError: (e: unknown) => setError(String((e as Error)?.message || e)),
  })

  // 价格偏离提示: limit 策略下, 限价偏离参考价 > 2% 时警告
  const needsPrice = pricePolicy === 'limit'

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
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
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
            <label className={LABEL_CLS}>价格策略</label>
            <select value={pricePolicy} onChange={(e) => setPricePolicy(e.target.value as 'limit' | 'next_open' | 'close')} className={INPUT_CLS}>
              <option value="next_open">次日开盘价 (盘后, 不需实时行情)</option>
              <option value="close">当日收盘价 (盘后)</option>
              <option value="limit">限价 (需实时行情)</option>
            </select>
          </div>
          {needsPrice && (
            <div>
              <label className={LABEL_CLS}>限价</label>
              <input value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} placeholder="1500.00" className={`${INPUT_CLS} font-mono`} />
            </div>
          )}
        </div>
        {result && <div className="mt-3 rounded-btn bg-accent/15 px-2.5 py-1.5 text-[11px] text-secondary">{result}</div>}
        {error && <div className="mt-3 rounded-btn bg-danger/10 px-2.5 py-1.5 text-[11px] text-danger">{error}</div>}
        <div className="mt-5 flex gap-2">
          <button
            onClick={() => submitM.mutate()}
            disabled={submitM.isPending || !symbol.trim() || qty <= 0 || qty % 100 !== 0 || (needsPrice && (!limitPrice || Number(limitPrice) <= 0))}
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
