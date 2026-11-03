/** API 客户端：axios 实例 + 统一事件流 WS 封装（自动重连，指数退避）。 */

import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 5000 })

export interface Health {
  status: string
  version: string
  simulation_only: boolean
}

export async function getHealth(): Promise<Health> {
  const r = await api.get<Health>('/system/health')
  return r.data
}

/**
 * 订阅 /ws/events 统一事件流（宪法 API 层）。断线自动重连：
 * 退避 1s→2s→4s…上限 30s。返回清理函数。
 * W1 后端尚未提供该端点，调用方将处于"重连等待"状态；W2/D10 起接入。
 */
export function openEvents(
  onMessage: (data: unknown) => void,
  onStatus?: (up: boolean) => void,
): () => void {
  let ws: WebSocket | null = null
  let closed = false
  let delay = 1000
  let timer: number | undefined

  const connect = () => {
    if (closed) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${proto}://${location.host}/ws/events`)
    ws.onopen = () => {
      delay = 1000
      onStatus?.(true)
    }
    ws.onmessage = (ev) => {
      try {
        onMessage(JSON.parse(ev.data as string))
      } catch {
        /* 非 JSON 忽略 */
      }
    }
    ws.onclose = () => {
      onStatus?.(false)
      if (!closed) {
        timer = window.setTimeout(connect, delay)
        delay = Math.min(delay * 2, 30000)
      }
    }
    ws.onerror = () => ws?.close()
  }
  connect()

  return () => {
    closed = true
    if (timer !== undefined) clearTimeout(timer)
    ws?.close()
  }
}

// ---------- 回测（D10） ----------

export interface StrategyInfo {
  name: string
  module: string
  params: Record<string, string | number>
}

export async function getStrategies(): Promise<StrategyInfo[]> {
  const r = await api.get<StrategyInfo[]>('/backtest/strategies')
  return r.data
}

export interface EquityPoint {
  dt: string
  equity: number
  cash: number
  market_value: number
}

export interface TradeRow {
  trade_id: string
  order_id: string
  symbol: string
  side: string
  price: number
  volume: number
  amount: number
  traded_at: number
}

export interface Metrics {
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  win_rate: number
  profit_loss_ratio: number | null
  turnover: number
  trade_count: number
}

export interface BacktestResult {
  run_id: number
  metrics: Metrics
  equity: EquityPoint[]
  trades: TradeRow[]
  orders: unknown[]
  logs: string[]
  bars: number
  config: Record<string, unknown>
}

export interface BacktestRequest {
  strategy: string
  symbols: string[]
  params?: Record<string, string | number>
  freq?: string
  start?: string
  end?: string
  initial_cash?: number
  slippage?: number
  rf?: number
}

export async function runBacktest(req: BacktestRequest): Promise<BacktestResult> {
  const r = await api.post<BacktestResult>('/backtest', req, { timeout: 120000 })
  return r.data
}

export async function getBacktest(runId: number): Promise<BacktestResult> {
  const r = await api.get<BacktestResult>(`/backtest/${runId}`)
  return r.data
}
