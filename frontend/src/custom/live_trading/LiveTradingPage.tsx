/**
 * 实盘交易主页面 — 账户创建/选择/模式 + 订单/成交/审批/健康 Tab。
 *
 * 顶部固定 LIVE 安全栏: 模式徽标 + 未解决对账差异 + arm/disarm + SSE 连接状态。
 * SSE: useLiveTradingSSE 监听 live_trading 事件, 自动失效所有 live 查询。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown, Database, Link2, LoaderCircle, Plus, Radio, ShieldAlert, ShieldCheck, Wallet, Wifi, WifiOff, type LucideIcon } from 'lucide-react'
import { api, type LiveAccount } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { AccountOverview } from './AccountOverview'
import { OrdersPanel } from './OrdersPanel'
import { ExecutionsPanel } from './ExecutionsPanel'
import { ApprovalsPanel } from './ApprovalsPanel'
import { HealthPanel } from './HealthPanel'
import { ArmDisarmButtons, ModeBadge } from './controls'
import { CreateAccountModal } from './CreateAccountModal'
import { useLiveTradingSSE } from './useLiveTradingSSE'
import { GatewaySetupPanel } from './GatewaySetupPanel'

/** 账户选择器 — 自定义下拉 (不依赖原生 <select>, 完全 CSS 接管, dark/light 通用)。 */
function AccountSelector({
  accounts,
  value,
  onChange,
}: {
  accounts: LiveAccount[]
  value: string | null
  onChange: (v: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const cur = accounts.find((a) => a.account_id === value) ?? accounts[0]
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        className="inline-flex h-8 items-center gap-2 rounded-btn border border-border bg-surface px-2.5 text-sm text-foreground outline-none transition-colors hover:border-accent/40 focus:border-accent/60"
      >
        <Wallet size={14} className="text-muted-foreground" />
        <span className="max-w-[180px] truncate">
          {cur ? cur.display_name + ' (' + cur.broker + ')' : '无账户'}
        </span>
        <ChevronDown size={14} className="text-muted-foreground" />
      </button>
      {open && (
        <div className="absolute left-0 top-9 z-50 min-w-full rounded-btn border border-border bg-elevated py-1 shadow-lg">
          {accounts.map((a) => (
            <button
              key={a.account_id}
              type="button"
              onMouseDown={(e) => { e.preventDefault(); onChange(a.account_id); setOpen(false) }}
              className={cn(
                'flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm transition-colors hover:bg-accent/10',
                a.account_id === value ? 'text-accent' : 'text-foreground',
              )}
            >
              <Wallet size={12} className="text-muted-foreground" />
              <span className="truncate">{a.display_name} ({a.broker})</span>
            </button>
          ))}
          {accounts.length === 0 && (
            <div className="px-2.5 py-1.5 text-sm text-muted-foreground">无账户</div>
          )}
        </div>
      )}
    </div>
  )
}

function StatusCell({
  icon: Icon,
  label,
  value,
  ok,
  pulse = false,
}: {
  icon: LucideIcon
  label: string
  value: string
  ok: boolean
  pulse?: boolean
}) {
  return (
    <div className="flex items-center gap-3 bg-surface px-4 py-3">
      <span className={cn('grid h-8 w-8 shrink-0 place-items-center rounded-lg', ok ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning')}>
        <Icon size={15} className={pulse ? 'animate-pulse' : ''} />
      </span>
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-[0.16em] text-muted">{label}</div>
        <div className={cn('mt-0.5 truncate text-xs font-medium', ok ? 'text-foreground' : 'text-warning')}>{value}</div>
      </div>
    </div>
  )
}

export function LiveTradingPage() {
  const qc = useQueryClient()
  const accountsQ = useQuery({ queryKey: QK.liveAccounts, queryFn: api.liveAccounts })
  const liveAccounts: LiveAccount[] = accountsQ.data?.accounts ?? []
  const [selectedAccount, setSelectedAccount] = useState<string | null>(null)
  const effAccount = selectedAccount ?? liveAccounts[0]?.account_id ?? null
  const [tab, setTab] = useState<'overview' | 'orders' | 'executions' | 'approvals' | 'health'>('overview')
  const [createOpen, setCreateOpen] = useState(false)

  // SSE: 监听 live_trading 事件, 自动失效所有 live 查询 (有账户时才连接)
  const sseStatus = useLiveTradingSSE(!!effAccount)

  const healthQ = useQuery({
    queryKey: QK.liveHealth(effAccount ?? ''),
    enabled: !!effAccount,
    queryFn: () => api.liveHealth(effAccount!),
  })

  // 调度器状态 (broker 连接指示)
  const schedQ = useQuery({
    queryKey: QK.liveScheduler,
    queryFn: () => api.liveScheduler(),
    staleTime: 15_000,
  })
  const schedulerRunning = schedQ.data?.running ?? false
  const registeredBrokers: string[] = schedQ.data?.registered_brokers ?? []
  const currentAccount = liveAccounts.find(a => a.account_id === effAccount)
  const brokerStatus = healthQ.data?.broker_status
  const connectM = useMutation({
    mutationFn: () => api.liveConnectAccount(effAccount!),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.liveAll }),
  })

  if (accountsQ.isLoading) {
    return (
      <div className="grid min-h-[55vh] place-items-center px-4 py-6">
        <div className="flex items-center gap-2 text-sm text-muted"><LoaderCircle className="h-4 w-4 animate-spin text-accent" />正在读取实盘账本…</div>
      </div>
    )
  }
  if (accountsQ.isError) {
    return (
      <div className="px-4 py-6 sm:px-6">
        <PageHeader title="实盘交易" subtitle="真实账户下单 · 人工逐笔审批 · 全链路审计" />
        <GatewaySetupPanel />
        <div className="rounded-xl border border-danger/30 bg-danger/5 p-5 text-sm text-danger">账户列表加载失败：{String(accountsQ.error)}</div>
      </div>
    )
  }

  // ── 空状态: 无账户时显示创建引导 ──
  if (liveAccounts.length === 0 && !accountsQ.isLoading) {
    return (
      <div className="px-4 py-6 sm:px-6">
        <PageHeader title="实盘交易" subtitle="真实账户下单 · 人工逐笔审批 · 全链路审计" />
        <div className="relative overflow-hidden rounded-2xl border border-border bg-gradient-to-br from-surface via-surface to-accent/5 px-6 py-16 text-center shadow-sm">
          <div className="pointer-events-none absolute -right-16 -top-16 h-48 w-48 rounded-full bg-accent/10 blur-3xl" />
          <ShieldCheck size={40} className="mx-auto mb-4 text-accent" />
          <h2 className="mb-2 text-lg font-medium">尚未创建实盘账户</h2>
          <p className="mx-auto mb-6 max-w-lg text-sm leading-6 text-muted-foreground">
            选择后端已经注册的券商适配器，连接真实账户并完成首次对账。只有连接健康、账本一致且人工启用后，订单才可进入审批与报单链路。
          </p>
          <button
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:bg-accent/90"
          >
            <Plus size={16} />
            创建实盘账户
          </button>
          <div className="mt-6 flex items-center justify-center gap-2 text-xs text-muted-foreground">
            {schedulerRunning ? (
              <><Wifi size={12} className="text-emerald-500" /> 调度器运行中 · 已注册 broker: {registeredBrokers.join(', ') || '无'}</>
            ) : (
              <><WifiOff size={12} /> 调度器未启动</>
            )}
          </div>
        </div>
        {createOpen && (
          <CreateAccountModal
            onClose={() => setCreateOpen(false)}
            onCreated={() => qc.invalidateQueries({ queryKey: QK.liveAccounts })}
          />
        )}
      </div>
    )
  }

  return (
    <div className="px-4 py-6 sm:px-6">
      <PageHeader
        title="实盘交易"
        subtitle="真实账户下单 · 人工逐笔审批 · 全链路审计"
        right={
          <div className="flex items-center gap-2">
            {schedulerRunning ? (
              <span className="inline-flex items-center gap-1 text-xs text-emerald-500">
                <Wifi size={12} /> 对账调度器
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <WifiOff size={12} /> 调度器未启动
              </span>
            )}
            <button
              onClick={() => setCreateOpen(true)}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-base px-2.5 py-1 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
            >
              <Plus size={12} /> 创建账户
            </button>
          </div>
        }
      />

      <GatewaySetupPanel />

      {/* LIVE 安全控制台：连接、账本、事件流三条链路明确分离。 */}
      {effAccount && (
        <div className={cn(
          'mb-5 overflow-hidden rounded-xl border shadow-sm',
          (healthQ.data?.unresolved_diffs ?? 0) > 0
            ? 'border-amber-500/40 bg-amber-500/10'
            : 'border-border bg-surface',
        )}>
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/70 px-4 py-3">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/15 text-accent"><ShieldAlert size={16} /></span>
              <div className="min-w-0">
                <div className="flex items-center gap-2"><span className="text-sm font-semibold tracking-wide">LIVE CONTROL</span><ModeBadge mode={healthQ.data?.mode ?? 'disarmed'} /></div>
                <div className="mt-0.5 truncate text-[11px] text-muted">{currentAccount?.display_name} · {currentAccount?.broker}</div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {!brokerStatus?.connected && (
                <button
                  onClick={() => connectM.mutate()}
                  disabled={connectM.isPending}
                  className="inline-flex items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/15 disabled:opacity-50"
                >
                  {connectM.isPending ? <LoaderCircle size={13} className="animate-spin" /> : <Link2 size={13} />}
                  {connectM.isPending ? '连接中' : '连接券商'}
                </button>
              )}
              <ArmDisarmButtons account_id={effAccount} mode={healthQ.data?.mode} disabled={!brokerStatus?.connected || (healthQ.data?.unresolved_diffs ?? 0) > 0} />
            </div>
          </div>
          <div className="grid gap-px bg-border/70 sm:grid-cols-3">
            <StatusCell icon={Link2} label="券商连接" ok={!!brokerStatus?.connected} value={brokerStatus?.connected ? `已连接${brokerStatus.latency_ms != null ? ` · ${brokerStatus.latency_ms}ms` : ''}` : brokerStatus?.error || '未连接'} />
            <StatusCell icon={Database} label="账本一致性" ok={(healthQ.data?.unresolved_diffs ?? 0) === 0} value={(healthQ.data?.unresolved_diffs ?? 0) > 0 ? `${healthQ.data?.unresolved_diffs} 处差异` : '无未解决差异'} />
            <StatusCell icon={Radio} label="事件流" ok={sseStatus === 'connected'} value={sseStatus === 'connected' ? '实时推送' : sseStatus === 'connecting' ? '连接中' : sseStatus === 'reconnecting' ? '正在重连' : sseStatus === 'error' ? '连接异常' : '未连接'} pulse={sseStatus !== 'connected'} />
          </div>
          {connectM.isError && <div className="border-t border-danger/20 bg-danger/5 px-4 py-2 text-xs text-danger">连接失败：{String(connectM.error)}</div>}
        </div>
      )}

      {/* 账户选择 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <AccountSelector
          accounts={liveAccounts}
          value={effAccount}
          onChange={setSelectedAccount}
        />
        {effAccount && (
          <span className="text-xs text-muted-foreground">
            {registeredBrokers.includes(currentAccount?.broker ?? '') ? '适配器已加载' : '适配器未加载'}
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="mb-4 flex gap-1 overflow-x-auto border-b border-border">
        {(['overview', 'orders', 'executions', 'approvals', 'health'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cn(
              'border-b-2 px-3 py-1.5 text-sm transition-colors',
              tab === t ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            {{ overview: '账户概览', orders: '订单', executions: '成交', approvals: '待审批', health: '健康/对账' }[t]}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.15 }}
        >
          {tab === 'overview' && <AccountOverview account_id={effAccount} />}
          {tab === 'orders' && <OrdersPanel account_id={effAccount} />}
          {tab === 'executions' && <ExecutionsPanel account_id={effAccount} />}
          {tab === 'approvals' && <ApprovalsPanel />}
          {tab === 'health' && <HealthPanel account_id={effAccount} />}
        </motion.div>
      </AnimatePresence>

      {createOpen && (
        <CreateAccountModal
          onClose={() => setCreateOpen(false)}
          onCreated={() => qc.invalidateQueries({ queryKey: QK.liveAccounts })}
        />
      )}
    </div>
  )
}
