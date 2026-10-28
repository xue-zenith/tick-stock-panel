/**
 * 创建实盘账户弹窗 — 账户 ID / broker / 显示名 / 初始资金。
 *
 * 使用项目共享 Modal 组件 (ESC 关闭 / 焦点陷阱 / 遮罩模糊 / 可访问性),
 * 样式镜像 paper 扩展的 FeeSettingsModal (同款 input/button 设计 token)。
 *
 * 账户 ID: 自动生成或手动指定 (与 paper 扩展 acc_xxx 同风格)。
 * broker: 默认 mock (内存测试), 真实 XtQuant/QMT 在券商开通后选择。
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, PlugZap, ShieldCheck } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS = 'mt-1 w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-sm outline-none transition-colors focus:border-accent/50'
const LABEL_CLS = 'block text-[11px] text-muted'

export function CreateAccountModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: () => void
}) {
  const [accountId, setAccountId] = useState('')
  const [broker, setBroker] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [autoId, setAutoId] = useState(true)
  const brokersQ = useQuery({ queryKey: QK.liveBrokers, queryFn: api.liveBrokers })
  const brokers = brokersQ.data?.brokers ?? []
  const registeredBrokers = brokers.filter((item) => item.registered)
  const installableBrokers = brokers.filter((item) => !item.registered && item.kind === 'real')
  const selectedBroker = brokers.find((item) => item.name === broker)

  useEffect(() => {
    if (!broker && registeredBrokers[0]) setBroker(registeredBrokers[0].name)
  }, [broker, registeredBrokers])

  const m = useMutation({
    mutationFn: async () => {
      const id = autoId || !accountId.trim()
        ? `live_${Date.now().toString(36)}${Math.floor(Math.random() * 36).toString(36)}`
        : accountId.trim()
      const name = displayName.trim() || `实盘账户-${id.slice(-4)}`
      if (!selectedBroker?.registered) throw new Error('请选择已加载的券商适配器；真实券商需先完成 SDK/账户环境接入')
      return api.liveCreateAccount({ account_id: id, broker, display_name: name })
    },
    onSuccess: () => {
      onCreated()
      onClose()
    },
    onError: (e: unknown) => setError(String((e as Error)?.message || e)),
  })

  return (
    <Modal
      onClose={onClose}
      labelledBy="create-live-acc-title"
      overlayClassName="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 p-3 backdrop-blur-sm sm:items-center sm:p-5"
      panelClassName="flex max-h-[calc(100dvh-1.5rem)] w-[min(92vw,42rem)] min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface shadow-xl sm:max-h-[calc(100dvh-2.5rem)]"
    >
      <div className="flex min-h-0 flex-1 flex-col p-5">
        {/* 标题 */}
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-accent" />
          <h3 id="create-live-acc-title" className="text-sm font-semibold text-foreground">
            创建实盘账户
          </h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          这里只创建本地账户映射，不保存券商密码。创建后请在实盘页连接适配器并完成一次干净对账，再启用交易。
        </p>

        {/* 可滚动内容区: 小屏时标题和底部操作始终可见 */}
        <div className="mt-4 min-h-0 flex-1 overflow-y-auto overscroll-contain pr-1">
          <div className="space-y-3">
          {/* 账户 ID */}
          <div>
            <label className={LABEL_CLS}>账户 ID</label>
            <div className="mt-1 flex items-center gap-2">
              <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs text-secondary">
                <input
                  type="checkbox"
                  checked={autoId}
                  onChange={(e) => setAutoId(e.target.checked)}
                  className="h-3.5 w-3.5"
                />
                自动生成
              </label>
              {!autoId && (
                <input
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                  placeholder="my_account"
                  className={`${INPUT_CLS} flex-1`}
                />
              )}
            </div>
          </div>

          {/* 显示名称 */}
          <div>
            <label className={LABEL_CLS}>显示名称</label>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="主账户 / 策略A / ..."
              className={INPUT_CLS}
            />
          </div>

          {/* broker 选择 */}
          <div>
            <div className="flex items-center justify-between">
              <label className={LABEL_CLS}>券商适配器</label>
              <span className="text-[10px] text-muted">已加载的适配器才可创建账户</span>
            </div>
            <select
              value={broker}
              onChange={(e) => setBroker(e.target.value)}
              className={INPUT_CLS}
              disabled={brokersQ.isLoading || registeredBrokers.length === 0}
            >
              {brokers.map((b) => (
                <option key={b.name} value={b.name} disabled={!b.registered}>
                  {b.label}{b.kind === 'simulation' ? ' · 演练' : ' · 实盘'}{!b.registered ? ' · 待安装适配器' : ''}
                </option>
              ))}
            </select>
            {brokersQ.isError && <p className="mt-1 text-[10px] text-danger">适配器清单加载失败</p>}
            {!brokersQ.isLoading && registeredBrokers.length === 0 && (
              <p className="mt-1 text-[10px] text-warning">后端尚未加载可交易适配器；请先完成下方真实券商或自定义适配器部署。</p>
            )}
            {selectedBroker?.capabilities && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {[
                  selectedBroker.capabilities.supports_partial_fills && '部分成交',
                  selectedBroker.capabilities.supports_cancel_confirmation && '撤单确认',
                  selectedBroker.capabilities.supports_push_callbacks && '实时回调',
                  selectedBroker.capabilities.callback_signature && `验签·${selectedBroker.capabilities.callback_signature}`,
                ].filter(Boolean).map((label) => (
                  <span key={String(label)} className="rounded-full border border-accent/20 bg-accent/5 px-2 py-0.5 text-[10px] text-accent">
                    {label}
                  </span>
                ))}
              </div>
            )}
          </div>

          <div className="flex gap-2 rounded-lg border border-border/70 bg-elevated/50 p-3 text-[11px] leading-5 text-muted">
            <PlugZap className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
            <span>真实适配器从部署环境或二开扩展读取凭据；密码、Token 和账户密钥不会写入平台数据库。</span>
          </div>

          {/* 厂商接入状态 */}
          {installableBrokers.length > 0 && (
            <div className="rounded-lg border border-warning/25 bg-warning/5 p-3">
              <div className="flex items-center gap-2 text-xs font-medium text-foreground">
                <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                真实券商接入中心
              </div>
              <p className="mt-1 text-[10px] leading-5 text-muted">
                下面是平台支持的接入位。当前部署未加载厂商适配器，因此不能直接下单；完成要求后重启后端即可变为“已就绪”。
              </p>
              <div className="mt-2 space-y-2">
                {installableBrokers.map((item) => (
                  <div key={item.name} className="rounded-md border border-border/70 bg-base/70 p-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-medium text-foreground">{item.label}</span>
                      <span className="rounded-full border border-warning/30 px-2 py-0.5 text-[10px] text-warning">需安装 SDK / 适配器</span>
                    </div>
                    <div className="mt-1.5 grid gap-1 text-[10px] leading-4 text-muted">
                      {(item.requirements ?? []).map((requirement) => (
                        <span key={requirement} className="flex gap-1.5"><span className="text-warning">•</span>{requirement}</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 自定义券商接入 */}
          {brokersQ.data?.custom_integration && (
            <details className="rounded-lg border border-accent/20 bg-accent/5 p-3">
              <summary className="cursor-pointer list-none text-xs font-medium text-foreground">
                <span className="inline-flex items-center gap-2"><CheckCircle2 className="h-3.5 w-3.5 text-accent" />自定义券商适配器接入</span>
              </summary>
              <div className="mt-2 space-y-1.5 text-[10px] leading-5 text-muted">
                {brokersQ.data.custom_integration.steps.map((step, index) => (
                  <div key={step} className="flex gap-2"><span className="shrink-0 text-accent">{index + 1}.</span><span>{step}</span></div>
                ))}
              </div>
              <pre className="mt-2 overflow-x-auto rounded-md border border-border/70 bg-base p-2 text-[10px] leading-4 text-secondary">{`register("your_broker", factory,\n  capabilities=BrokerCapabilities(...))`}</pre>
            </details>
          )}
          </div>

          {/* 错误 */}
          {error && (
            <div className="mt-3 rounded-btn bg-danger/10 px-2.5 py-1.5 text-[11px] text-danger">
              {error}
            </div>
          )}
        </div>

        {/* 固定在弹窗底部的操作区，不随内容被裁切 */}
        <div className="mt-4 flex shrink-0 gap-2 border-t border-border/60 pt-4">
          <button
            onClick={() => m.mutate()}
            disabled={m.isPending || !selectedBroker?.registered}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {m.isPending ? '创建中…' : '创建账户'}
          </button>
          <button
            onClick={onClose}
            className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}
