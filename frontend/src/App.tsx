import React, { useCallback, useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { api, apiError, getHealth, openEvents, type Health } from './api/client'
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
// 手册正文是后端 JSON，组件本身很轻，但仍分包：不进首屏
const ManualPage = React.lazy(() => import('./pages/ManualPage'))

const NAV = [
  { to: '/watch', label: '实时盯盘' },
  { to: '/market', label: '行情面板' },
  { to: '/strategy', label: '策略工作台' },
  { to: '/backtest', label: '回测报告' },
  { to: '/monitor', label: '实盘监控' },
  { to: '/channels', label: '行情与交易' },
  { to: '/interop', label: '软件互操作' },
  { to: '/data', label: '数据管理' },
  { to: '/guide', label: '新手指引' },
  { to: '/manual', label: '使用手册' },
  { to: '/compliance', label: '报备清单' },
  { to: '/settings', label: '设置' },
]

function HealthBadge() {
  const [health, setHealth] = useState<Health | null>(null)
  const [err, setErr] = useState('')
  const [nonce, setNonce] = useState(0)
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
          if (alive) setErr(apiError(e))
        })
    load()
    const t = setInterval(load, 5000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [nonce])
  if (err)
    return (
      <>
        <button className="mini" onClick={() => { setHealth(null); setNonce((v) => v + 1) }}>
          重试
        </button>
        <span className="badge badge-down" style={{ marginLeft: 8 }}>
          系统未连接
        </span>
      </>
    )
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
  // 确认/拒绝指令送不出服务端时必须让用户看见——这直接关系真实资金
  const [gateErr, setGateErr] = useState('')
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
        setGateErr('')
      } catch (e) {
        setGateErr(
          `「${decision === 'confirm' ? '确认' : '拒绝'}」指令没送达系统（委托 ${orderId}）：${apiError(e)}` +
            '——该单仍未生效，可在「实盘监控」页核对状态；10 秒内未确认的待确认单会自动作废。',
        )
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
        {gateErr && (
          <div style={{ padding: '0 20px' }}>
            <div className="alert-banner">
              <p>
                <b>二次确认未完成：</b>
                {gateErr}
              </p>
              <p>
                <button className="mini" onClick={() => setGateErr('')}>
                  知道了
                </button>
              </p>
            </div>
          </div>
        )}
        <React.Suspense fallback={<section><p className="placeholder">页面加载中…</p></section>}>
          <Routes>
            <Route path="/" element={<MarketPage />} />
            <Route path="/market" element={<MarketPage />} />
            <Route path="/watch" element={<WatchPage />} />
            <Route path="/guide" element={<GuidePage />} />
            <Route path="/manual" element={<ManualPage />} />
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
