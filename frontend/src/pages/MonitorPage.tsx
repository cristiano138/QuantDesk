import { useEffect, useState } from 'react'
import {
  api,
  getHealth,
  openEvents,
  type Health,
} from '../api/client'

interface OrderRow {
  order_id: string
  symbol: string
  side: string
  price: number
  volume: number
  order_type: string
  status: string
  filled_volume: number
  avg_filled_price: number
  message: string
  strategy_id: string
  created_at: number
  updated_at: number
}

interface PositionRow {
  symbol: string
  volume: number
  available: number
  avg_cost: number
  market_value: number
}

interface AccountRow {
  total_asset: number
  cash: number
  market_value: number
  frozen: number
}

interface TradeRow {
  trade_id: string
  order_id: string
  symbol: string
  side: string
  price: number
  volume: number
  amount: number
  traded_at: number
}

const STATUS_CN: Record<string, string> = {
  pending_confirm: '待确认',
  submitted: '已提交',
  part_filled: '部分成交',
  filled: '已成交',
  cancelled: '已撤单',
  rejected: '已拒绝',
  failed: '失败',
  expired_confirm: '确认超时',
}

function usePoll<T>(fn: () => Promise<T>, ms: number, trigger = 0): T | null {
  const [v, setV] = useState<T | null>(null)
  useEffect(() => {
    const load = () => fn().then(setV).catch(() => undefined)
    load()
    const t = setInterval(load, ms)
    return () => clearInterval(t)
  }, [fn, ms, trigger])
  return v
}

export default function MonitorPage() {
  const [health, setHealth] = useState<Health | null>(null)
  // WS 事件（order/trade/position/account）到达即刻触发一轮拉取——
  // 成交/状态变化即时上屏；轮询保留为断线兜底
  const [wsTick, setWsTick] = useState(0)
  useEffect(
    () =>
      openEvents((data) => {
        const t = (data as { type?: string })?.type
        if (t === 'order' || t === 'trade' || t === 'position' || t === 'account') {
          setWsTick((v) => v + 1)
        }
      }),
    [],
  )
  const orders = usePoll<OrderRow[]>(
    () => api.get<OrderRow[]>('/trading/orders').then((r) => r.data), 2000, wsTick)
  const positions = usePoll<PositionRow[]>(
    () => api.get<PositionRow[]>('/trading/positions').then((r) => r.data), 2000, wsTick)
  const account = usePoll<AccountRow>(
    () => api.get<AccountRow>('/trading/account').then((r) => r.data), 2000, wsTick)
  const trades = usePoll<TradeRow[]>(
    () => api.get<TradeRow[]>('/trading/trades').then((r) => r.data), 3000, wsTick)

  useEffect(() => {
    const load = () => getHealth().then(setHealth).catch(() => setHealth(null))
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [])

  const cancel = async (orderId: string) => {
    try {
      await api.post(`/trading/orders/${encodeURIComponent(orderId)}/cancel`)
    } catch {
      /* 状态以轮询为准 */
    }
  }

  const liveMode = health ? !health.simulation_only : false
  const channelLabel = liveMode ? '实盘通道' : '模拟撮合'

  return (
    <section>
      <h2>实盘监控</h2>

      <div className="monitor-cards">
        <div className="card metric-card">
          <span className="metric-label">总资产</span>
          <span className="metric-value">{account ? account.total_asset.toFixed(2) : '—'}</span>
        </div>
        <div className="card metric-card">
          <span className="metric-label">可用资金</span>
          <span className="metric-value">{account ? account.cash.toFixed(2) : '—'}</span>
        </div>
        <div className="card metric-card">
          <span className="metric-label">持仓市值</span>
          <span className="metric-value">{account ? account.market_value.toFixed(2) : '—'}</span>
        </div>
        <div className="card metric-card">
          <span className="metric-label">通道状态</span>
          <span className="metric-value">
            <span className={health ? (liveMode ? 'lamp lamp-live' : 'lamp lamp-paper') : 'lamp lamp-down'} />
            {health ? channelLabel : '后端离线'}
          </span>
        </div>
      </div>

      <div className="card">
        <h3>持仓（{positions?.length ?? 0}）</h3>
        {positions && positions.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>代码</th><th>持仓</th><th>可用（T+1）</th><th>成本</th><th>市值</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td>
                  <td>{p.volume}</td>
                  <td>{p.available}</td>
                  <td>{p.avg_cost.toFixed(3)}</td>
                  <td>{p.market_value.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="placeholder">无持仓。</p>
        )}
      </div>

      <div className="card">
        <h3>当日委托（{orders?.length ?? 0}）</h3>
        {orders && orders.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>时间</th><th>代码</th><th>方向</th><th>价格</th><th>数量</th>
                <th>成交</th><th>状态</th><th>说明</th><th></th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.order_id}>
                  <td>{new Date(o.created_at).toLocaleTimeString('zh-CN')}</td>
                  <td>{o.symbol}</td>
                  <td className={o.side === 'buy' ? 'up' : 'down'}>
                    {o.side === 'buy' ? '买入' : '卖出'}
                  </td>
                  <td>{o.price.toFixed(2)}</td>
                  <td>{o.volume}</td>
                  <td>{o.filled_volume}</td>
                  <td>{STATUS_CN[o.status] ?? o.status}</td>
                  <td className="msg">{o.message}</td>
                  <td>
                    {o.status === 'submitted' && (
                      <button className="mini" onClick={() => cancel(o.order_id)}>撤单</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="placeholder">无委托。下单经服务端三道闸（模拟盘开关 → 规则校验 → 二次确认）。</p>
        )}
      </div>

      <div className="card">
        <h3>成交记录（{trades?.length ?? 0}）</h3>
        {trades && trades.length > 0 ? (
          <table>
            <thead>
              <tr><th>时间</th><th>代码</th><th>方向</th><th>价格</th><th>数量</th><th>金额</th></tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.trade_id}>
                  <td>{new Date(t.traded_at).toLocaleTimeString('zh-CN')}</td>
                  <td>{t.symbol}</td>
                  <td className={t.side === 'buy' ? 'up' : 'down'}>
                    {t.side === 'buy' ? '买入' : '卖出'}
                  </td>
                  <td>{t.price.toFixed(2)}</td>
                  <td>{t.volume}</td>
                  <td>{t.amount.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="placeholder">无成交。</p>
        )}
      </div>
    </section>
  )
}
