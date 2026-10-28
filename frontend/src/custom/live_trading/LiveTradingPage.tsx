/**
 * 实盘交易主页面 — 账户创建/选择/模式 + 订单/成交/审批/健康 Tab。
 *
 * 顶部固定 LIVE 安全栏: 模式徽标 + 未解决对账差异 + arm/disarm + SSE 连接状态。
 * SSE: useLiveTradingSSE 监听 live_trading 事件, 自动失效所有 live 查询。
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown, Plus, ShieldCheck, ShieldAlert, Wallet, Wifi, WifiOff, Radio } from 'lucide-react'
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
    queryKey: ['live', 'scheduler'],
    queryFn: () => api.liveScheduler(),
    staleTime: 15_000,
  })
  const schedulerRunning = schedQ.data?.running ?? false
  const registeredBrokers: string[] = schedQ.data?.registered_brokers ?? []

  // ── 空状态: 无账户时显示创建引导 ──
  if (liveAccounts.length === 0 && !accountsQ.isLoading) {
    return (
      <div className="px-4 py-6 sm:px-6">
        <PageHeader title="实盘交易" subtitle="真实账户下单 · 人工逐笔审批 · 不开放自动交易" />
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border bg-surface/50 py-16">
          <ShieldCheck size={40} className="mb-4 text-muted-foreground" />
          <h2 className="mb-2 text-lg font-medium">尚未创建实盘账户</h2>
          <p className="mb-6 max-w-md text-center text-sm text-muted-foreground">
            创建一个实盘账户后, 可以连接券商适配器、执行对账、提交交易意图并逐笔审批。
            本版默认使用 MockBroker (内存测试适配器), 真实 XtQuant/QMT 适配器在券商开通后接入。
          </p>
          <button
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:bg-accent/90"
          >
            <Plus size={16} />
            创建实盘账户
          </button>
          <div className="mt-6 flex items-center gap-2 text-xs text-muted-foreground">
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
        subtitle="真实账户下单 · 人工逐笔审批 · 不开放自动交易"
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

      {/* LIVE 安全栏 (蓝图 §12.3: 顶部固定, 显示模式/差异/SSE状态) */}
      {effAccount && (
        <div className={cn(
          'mb-4 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2',
          (healthQ.data?.unresolved_diffs ?? 0) > 0
            ? 'border-amber-500/40 bg-amber-500/10'
            : 'border-border bg-surface',
        )}>
          <span className="inline-flex items-center gap-1 text-sm font-semibold">
            <ShieldAlert size={14} className="text-accent" />
            LIVE
          </span>
          <ModeBadge mode={healthQ.data?.mode ?? 'disarmed'} />
          {(healthQ.data?.unresolved_diffs ?? 0) > 0 && (
            <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
              <ShieldAlert size={12} />
              {healthQ.data?.unresolved_diffs} 处对账差异
            </span>
          )}
          <ArmDisarmButtons account_id={effAccount} mode={healthQ.data?.mode} />
          {/* SSE 连接状态 */}
          <span className={cn(
            'inline-flex items-center gap-1 text-xs',
            sseStatus === 'connected' ? 'text-emerald-500' :
            sseStatus === 'error' ? 'text-danger' : 'text-muted-foreground',
          )}>
            <Radio size={12} className={sseStatus === 'connected' ? '' : 'animate-pulse'} />
            {sseStatus === 'connected' ? '实时' : sseStatus === 'connecting' ? '连接中' : sseStatus === 'reconnecting' ? '重连' : sseStatus === 'error' ? '断线' : '未连接'}
          </span>
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
            broker: {liveAccounts.find(a => a.account_id === effAccount)?.broker ?? '--'}
            {registeredBrokers.includes(liveAccounts.find(a => a.account_id === effAccount)?.broker ?? '')
              ? ' (已连接)' : ' (未注册适配器)'}
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="mb-4 flex gap-1 border-b border-border">
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
