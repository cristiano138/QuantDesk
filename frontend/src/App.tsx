import { useEffect, useState } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { getHealth, type Health } from './api/client'
import BacktestPage from './pages/BacktestPage'
import CompliancePage from './pages/CompliancePage'
import DataPage from './pages/DataPage'
import MarketPage from './pages/MarketPage'
import MonitorPage from './pages/MonitorPage'
import SettingsPage from './pages/SettingsPage'
import StrategyPage from './pages/StrategyPage'

const NAV = [
  { to: '/market', label: '行情面板' },
  { to: '/strategy', label: '策略工作台' },
  { to: '/backtest', label: '回测报告' },
  { to: '/monitor', label: '实盘监控' },
  { to: '/data', label: '数据管理' },
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
        <Routes>
          <Route path="/" element={<MarketPage />} />
          <Route path="/market" element={<MarketPage />} />
          <Route path="/strategy" element={<StrategyPage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/monitor" element={<MonitorPage />} />
          <Route path="/data" element={<DataPage />} />
          <Route path="/compliance" element={<CompliancePage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  )
}
