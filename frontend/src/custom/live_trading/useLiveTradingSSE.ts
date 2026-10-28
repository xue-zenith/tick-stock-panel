/**
 * 实盘交易 SSE hook — 监听 /api/events 总线的 live_trading 事件, 自动失效 live 查询。
 *
 * 复用已有票据制 SSE 端点 (POST /api/events/ticket 换票 → GET /api/events?ticket=…)。
 * live_trading 事件由后端 app.trading.events.publish 双发到 bus (scope=live:read)。
 * 收到事件后 invalidate QK.liveAll 前缀, 所有 live 查询自动刷新 — 订单/成交/审批/对账。
 *
 * 连接生命周期绑定实盘页面: 页面卸载时关闭 SSE, 不常驻全局。
 */
import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { QK } from '@/lib/queryKeys'

type SSEStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'

const MAX_RECONNECT = 5
const BACKOFF_BASE_MS = 2000
const BACKOFF_CAP_MS = 30_000

export function useLiveTradingSSE(enabled: boolean): SSEStatus {
  const qc = useQueryClient()
  const esRef = useRef<EventSource | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>()
  const [status, setStatus] = useState<SSEStatus>('idle')

  useEffect(() => {
    if (!enabled) {
      setStatus('idle')
      return
    }

    let cancelled = false
    let fails = 0

    const connect = async () => {
      if (cancelled) return
      setStatus(fails > 0 ? 'reconnecting' : 'connecting')
      try {
        const res = await fetch('/api/events/ticket', { method: 'POST' })
        if (!res.ok) throw new Error(`ticket ${res.status}`)
        const { ticket } = await res.json() as { ticket: string }
        if (cancelled) return
        const es = new EventSource(`/api/events?ticket=${encodeURIComponent(ticket)}`)
        esRef.current = es

        es.onopen = () => {
          fails = 0
          setStatus('connected')
        }

        es.addEventListener('live_trading', () => {
          // 失效所有 live 查询 (订单/成交/审批/健康/对账)
          qc.invalidateQueries({ queryKey: QK.liveAll })
        })

        es.onerror = () => {
          es.close()
          esRef.current = null
          if (cancelled) return
          fails += 1
          if (fails > MAX_RECONNECT) {
            setStatus('error')
            return
          }
          const delay = Math.min(BACKOFF_BASE_MS * 2 ** (fails - 1), BACKOFF_CAP_MS)
          setStatus('reconnecting')
          reconnectRef.current = setTimeout(connect, delay)
        }
      } catch {
        if (cancelled) return
        fails += 1
        if (fails > MAX_RECONNECT) {
          setStatus('error')
          return
        }
        const delay = Math.min(BACKOFF_BASE_MS * 2 ** (fails - 1), BACKOFF_CAP_MS)
        setStatus('reconnecting')
        reconnectRef.current = setTimeout(connect, delay)
      }
    }

    void connect()

    return () => {
      cancelled = true
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      esRef.current?.close()
      esRef.current = null
    }
  }, [enabled, qc])

  return status
}
