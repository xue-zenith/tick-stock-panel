import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, KeyRound, LoaderCircle, Network, PlugZap, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const input = 'mt-1 w-full rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs outline-none focus:border-accent/60'

export function GatewaySetupPanel() {
  const qc = useQueryClient()
  const [broker, setBroker] = useState<'futu' | 'ibkr' | 'qmt'>('futu')
  const [gatewayId, setGatewayId] = useState('futu-local-01')
  const [host, setHost] = useState('127.0.0.1')
  const [port, setPort] = useState('11111')
  const [environment, setEnvironment] = useState<'SIMULATE' | 'PAPER' | 'REAL'>('PAPER')
  const [accountId, setAccountId] = useState('')
  const [clientId, setClientId] = useState('17')
  const [authToken, setAuthToken] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const gatewaysQ = useQuery({ queryKey: QK.liveGateways, queryFn: api.liveGateways })
  const saveM = useMutation({
    mutationFn: () => api.liveSaveGateway({
      gateway_id: gatewayId.trim(), broker, host: host.trim(), port: Number(port), environment,
      account_id: accountId.trim() || undefined,
      client_id: broker === 'ibkr' ? Number(clientId) : undefined,
      read_only: true,
      auth_token: authToken.trim() || undefined,
    }),
    onSuccess: (result) => {
      setAuthToken('')
      setMessage(result.secret_stored ? '配置已保存，凭据已写入系统密钥环。' : '配置已保存；当前未写入 Gateway Token。')
      qc.invalidateQueries({ queryKey: QK.liveGateways })
    },
    onError: (error) => setMessage(String(error)),
  })
  const testM = useMutation({
    mutationFn: (id: string) => api.liveTestGateway(id),
    onSuccess: (result) => setMessage(`${result.read_capability} · ${result.checks.map((item) => `${item.id}:${item.status}`).join(' · ')}`),
    onError: (error) => setMessage(String(error)),
  })
  const discoverM = useMutation({
    mutationFn: (id: string) => api.liveDiscoverGatewayAccounts(id),
    onSuccess: (result) => setMessage(result.accounts.length ? `发现账户：${result.accounts.map((item) => item.account_id).join('、')}` : (result.error || `账户发现状态：${result.status}`)),
    onError: (error) => setMessage(String(error)),
  })
  const gateways = gatewaysQ.data?.gateways ?? []

  const switchBroker = (value: 'futu' | 'ibkr' | 'qmt') => {
    setBroker(value)
    if (value === 'futu') { setGatewayId('futu-local-01'); setPort('11111') }
    if (value === 'ibkr') { setGatewayId('ibkr-paper-01'); setPort('4002') }
    if (value === 'qmt') { setGatewayId('qmt-local-01'); setPort('19001') }
  }

  return (
    <details className="mb-5 overflow-hidden rounded-xl border border-border bg-surface shadow-sm">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3">
        <span className="flex items-center gap-2 text-sm font-semibold"><PlugZap size={15} className="text-accent" />券商 Gateway 接入与环境检测</span>
        <span className="text-[11px] text-muted">SDK / 终端 / 账户只读验证</span>
      </summary>
      <div className="border-t border-border/70 p-4">
        <div className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="text-[11px] text-muted">券商<select value={broker} onChange={(e) => switchBroker(e.target.value as typeof broker)} className={input}><option value="futu">富途 OpenD</option><option value="ibkr">IBKR TWS / Gateway</option><option value="qmt">QMT / XtQuant</option></select></label>
              <label className="text-[11px] text-muted">环境<select value={environment} onChange={(e) => setEnvironment(e.target.value as typeof environment)} className={input}><option value="SIMULATE">SIMULATE</option><option value="PAPER">PAPER</option><option value="REAL">REAL</option></select></label>
              <label className="text-[11px] text-muted">Gateway ID<input value={gatewayId} onChange={(e) => setGatewayId(e.target.value)} className={input} /></label>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="text-[11px] text-muted">Host<input value={host} onChange={(e) => setHost(e.target.value)} className={input} /></label>
              <label className="text-[11px] text-muted">Port<input inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} className={input} /></label>
              <label className="text-[11px] text-muted">账户/acc_id<input value={accountId} onChange={(e) => setAccountId(e.target.value)} placeholder="由厂商终端发现" className={input} /></label>
            </div>
            {broker === 'ibkr' && <label className="block text-[11px] text-muted">IBKR clientId<input inputMode="numeric" value={clientId} onChange={(e) => setClientId(e.target.value)} className={input} /></label>}
            <label className="block text-[11px] text-muted"><span className="inline-flex items-center gap-1"><KeyRound size={12} />Gateway Token（可选，写入系统密钥环）</span><input type="password" value={authToken} onChange={(e) => setAuthToken(e.target.value)} placeholder="至少 32 位，不写入数据库" className={input} /></label>
            <div className="flex flex-wrap gap-2">
              <button onClick={() => saveM.mutate()} disabled={saveM.isPending || !gatewayId.trim()} className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">{saveM.isPending ? <LoaderCircle size={13} className="animate-spin" /> : <KeyRound size={13} />}保存安全配置</button>
              <span className="inline-flex items-center gap-1.5 text-[10px] text-muted"><ShieldAlert size={12} className="text-warning" />首次只读检测，不会发测试单</span>
            </div>
            {message && <div className="rounded-btn border border-border bg-base px-2.5 py-2 text-[11px] text-secondary">{message}</div>}
          </div>
          <div className="rounded-lg border border-border/70 bg-base/60 p-3">
            <div className="flex items-center gap-2 text-xs font-medium"><Network size={13} className="text-accent" />已保存 Gateway</div>
            <div className="mt-2 space-y-2">
              {gateways.length === 0 && <p className="text-[11px] text-muted">尚未配置。请先启动 OpenD、TWS 或 IB Gateway，再保存配置。</p>}
              {gateways.map((item) => (
                <div key={item.gateway_id} className="rounded-md border border-border/70 bg-surface p-2.5">
                  <div className="flex items-center justify-between gap-2"><span className="text-xs font-medium">{item.gateway_id}</span><span className="text-[10px] text-muted">{item.broker} · {item.environment}</span></div>
                  <div className="mt-1 text-[10px] text-muted">{item.host}:{item.port} {item.account_id ? `· ${item.account_id}` : ''}</div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    <button onClick={() => testM.mutate(item.gateway_id)} disabled={testM.isPending} className="inline-flex items-center gap-1 rounded-btn border border-accent/30 px-2 py-1 text-[10px] text-accent disabled:opacity-50"><CheckCircle2 size={11} />只读连接测试</button>
                    <button onClick={() => discoverM.mutate(item.gateway_id)} disabled={discoverM.isPending} className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[10px] text-secondary disabled:opacity-50"><Network size={11} />发现账户</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </details>
  )
}
