/**
 * 实盘交易扩展入口 — 在主入口注册路由 + 导航 (镜像 paper extension)。
 */
import type { FrontendExtension } from '@/extensions/types'
import { ShieldCheck } from 'lucide-react'
import { LiveTradingPage } from './LiveTradingPage'

const extension: FrontendExtension = {
  id: 'official.live_trading',
  apiVersion: 1,
  routes: [{ id: 'live-trading', path: '/live-trading', component: LiveTradingPage }],
  navigation: [{ id: 'live-trading', routeId: 'live-trading', label: '实盘', icon: ShieldCheck, order: 550 }],
}

export default extension