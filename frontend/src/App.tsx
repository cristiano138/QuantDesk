import React, { useCallback, useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { api, getHealth, openEvents, type Health } from './api/client'
import ConfirmDialog, { type ConfirmRequest } from './components/ConfirmDialog'
import ChannelsPage from './pages/ChannelsPage'
import CompliancePage from './pages/CompliancePage'
import DataPage from './pages/DataPage'
import InteropPage from './pages/InteropPage'
import MonitorPage from './pages/MonitorPage'
import SettingsPage from './pages/SettingsPage'

// 首屏不拉 Monaco 与图表库：这三页按需分包（宪法第 8 条栈纪律不变，只是切 chunk）
const MarketPage = React.lazy(() => import('./pages/MarketPage'))
const StrategyPage = React.lazy(() => import('./pages/StrategyPage'))
const BacktestPage = React.lazy(() => import('./pages/BacktestPage'))
const WatchPage = React.lazy(() => import('./pages/WatchPage'))
const GuidePage = React.lazy(() => import('./pages/GuidePage'))

const NAV = [
  { to: '/watch', label: '实时盯盘' },
  { to: '/market', label: '行情面板' },
  { to: '/strategy', label: '策略工作台' },
  { to: '/backtest', label: '回测报告' },
  { to: '/monitor', label: '实盘监控' },
  { to: '/channels', label: '通道' },
  { to: '/interop', label: '软件互操作' },
  { to: '/data', label: '数据管理' },
  { to: '/guide', label: '新手指引' },
  { to: '/compliance', label: '报备清单' },
  { to: '/settings', label: '设置' },
]

function HealthBadge() {
  const [health, setHealth] = useState<Health | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let alive = true
    const load = () =>
      getHealth()
        .then((h) => {
          if (alive) {
            setHealth(h)
            setErr('')
          }
        })
        .catch((e: unknown) => {
          if (alive) setErr(e instanceof Error ? e.message : String(e))
        })
    load()
    const t = setInterval(load, 5000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])
  if (err) return <span className="badge badge-down">后端离线</span>
  if (!health) return <span className="badge">连接中…</span>
  return (
    <span className={health.simulation_only ? 'badge badge-paper' : 'badge badge-live'}>
      {health.simulation_only ? '模拟盘模式' : '实盘模式'} · v{health.version}
    </span>
  )
}

export default function App() {
  // 实盘二次确认（D17）：全局监听 confirm_request，服务端强制、前端仅表现层
  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)
  useEffect(
    () =>
      openEvents((data) => {
        const d = data as {
          type: string
          data?: { action?: string; order?: Record<string, unknown> }
        }
        if (d?.type === 'risk' && d.data?.action === 'confirm_request' && d.data.order) {
          const o = d.data.order
          setConfirmReq({
            order_id: String(o.order_id ?? ''),
            symbol: String(o.symbol ?? ''),
            side: String(o.side ?? 'buy'),
            price: Number(o.price ?? 0),
            volume: Number(o.volume ?? 0),
            strategy_id: String(o.strategy_id ?? 'manual'),
          })
        }
      }),
    [],
  )
  const onConfirmDone = useCallback(
    async (orderId: string, decision: 'confirm' | 'reject' | 'expired') => {
      setConfirmReq(null)
      if (decision === 'expired') return // 服务端超时自会作废
      try {
        await api.post(`/trading/orders/${encodeURIComponent(orderId)}/${decision}`)
      } catch {
        /* 结果以订单状态为准 */
      }
    },
    [],
  )
  return (
    <div className="app">
      <aside>
        <h1>QuantDesk</h1>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? 'active' : '')}>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main>
        <header>
          <HealthBadge />
        </header>
        <React.Suspense fallback={<section><p className="placeholder">页面加载中…</p></section>}>
          <Routes>
            <Route path="/" element={<MarketPage />} />
            <Route path="/market" element={<MarketPage />} />
            <Route path="/watch" element={<WatchPage />} />
            <Route path="/guide" element={<GuidePage />} />
            <Route path="/strategy" element={<StrategyPage />} />
            <Route path="/backtest" element={<BacktestPage />} />
            <Route path="/monitor" element={<MonitorPage />} />
            <Route path="/channels" element={<ChannelsPage />} />
            <Route path="/interop" element={<InteropPage />} />
            <Route path="/data" element={<DataPage />} />
            <Route path="/compliance" element={<CompliancePage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </React.Suspense>
      </main>
      <ConfirmDialog request={confirmReq} onDone={onConfirmDone} />
    </div>
  )
}
