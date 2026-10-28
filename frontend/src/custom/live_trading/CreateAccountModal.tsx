/**
 * 创建实盘账户弹窗 — 账户 ID / broker / 显示名 / 初始资金。
 *
 * 使用项目共享 Modal 组件 (ESC 关闭 / 焦点陷阱 / 遮罩模糊 / 可访问性),
 * 样式镜像 paper 扩展的 FeeSettingsModal (同款 input/button 设计 token)。
 *
 * 账户 ID: 自动生成或手动指定 (与 paper 扩展 acc_xxx 同风格)。
 * broker: 默认 mock (内存测试), 真实 XtQuant/QMT 在券商开通后选择。
 */
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api } from '@/lib/api'

const BROKERS = [
  { value: 'mock', label: 'MockBroker (内存测试, 推荐)' },
  { value: 'xtquant', label: 'XtQuant / QMT (需券商开通, 暂未实现)' },
  { value: 'futu', label: 'Futu OpenAPI (暂未实现)' },
  { value: 'ibkr', label: 'IBKR TWS (暂未实现)' },
]

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
  const [broker, setBroker] = useState('mock')
  const [displayName, setDisplayName] = useState('')
  const [initialCash, setInitialCash] = useState('1000000')
  const [error, setError] = useState<string | null>(null)
  const [autoId, setAutoId] = useState(true)

  const m = useMutation({
    mutationFn: async () => {
      const id = autoId || !accountId.trim()
        ? `live_${Date.now().toString(36)}${Math.floor(Math.random() * 36).toString(36)}`
        : accountId.trim()
      const name = displayName.trim() || `实盘账户-${id.slice(-4)}`
      // 创建账户; 初始资金快照在首次对账时由 broker adapter 提供 (mock 默认 1_000_000)
      return api.liveCreateAccount({ account_id: id, broker, display_name: name })
    },
    onSuccess: () => {
      onCreated()
      onClose()
    },
    onError: (e: unknown) => setError(String((e as Error)?.message || e)),
  })

  return (
    <Modal onClose={onClose} labelledBy="create-live-acc-title">
      <div className="p-5">
        {/* 标题 */}
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-accent" />
          <h3 id="create-live-acc-title" className="text-sm font-semibold text-foreground">
            创建实盘账户
          </h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          账户创建后, 首次对账会从 broker 适配器拉取资金/持仓快照。MockBroker 默认 100 万初始资金。
        </p>

        {/* 表单 */}
        <div className="mt-4 space-y-3">
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
            <label className={LABEL_CLS}>券商适配器</label>
            <select
              value={broker}
              onChange={(e) => setBroker(e.target.value)}
              className={INPUT_CLS}
            >
              {BROKERS.map((b) => (
                <option key={b.value} value={b.value} disabled={b.value !== 'mock'}>
                  {b.label}
                </option>
              ))}
            </select>
            {broker !== 'mock' && (
              <p className="mt-1 text-[10px] text-amber-500">
                该适配器暂未实现, 仅 mock 可用。真实适配器需券商开通后开发接入。
              </p>
            )}
          </div>

          {/* 初始资金 */}
          <div>
            <label className={LABEL_CLS}>初始资金 (仅参考, 实际由 broker 快照决定)</label>
            <input
              value={initialCash}
              onChange={(e) => setInitialCash(e.target.value)}
              type="number"
              className={`${INPUT_CLS} font-mono`}
            />
          </div>
        </div>

        {/* 错误 */}
        {error && (
          <div className="mt-3 rounded-btn bg-danger/10 px-2.5 py-1.5 text-[11px] text-danger">
            {error}
          </div>
        )}

        {/* 按钮区 */}
        <div className="mt-5 flex gap-2">
          <button
            onClick={() => m.mutate()}
            disabled={m.isPending}
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
