/** API 客户端：axios 实例 + 统一事件流 WS 封装（自动重连，指数退避）。 */

import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 5000 })

/**
 * 统一取服务端错误原文：后端把可读原因放在 HTTPException 的 detail 里
 * （如「修改 simulation_only 到危险值需输入确认文本…」），前端必须原样回显，
 * 不做任何二次判定（宪法第 2 条：安全判定在服务端）。
 */
export function apiError(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const detail = (e.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string' && detail) return detail
    if (detail !== undefined && detail !== null) return JSON.stringify(detail)
    if (typeof e.response?.data === 'string' && e.response.data) return e.response.data
    return e.message
  }
  return e instanceof Error ? e.message : String(e)
}

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
  /** 缺省时后端按 settings.backtest_async 决定（默认 true 走异步队列）。 */
  async_run?: boolean
}

export async function runBacktest(req: BacktestRequest): Promise<BacktestResult> {
  const r = await api.post<BacktestResult>('/backtest', req, { timeout: 120000 })
  return r.data
}

export async function getBacktest(runId: number): Promise<BacktestResult> {
  const r = await api.get<BacktestResult>(`/backtest/${runId}`)
  return r.data
}

/** 异步提交的即时应答（后端只回 run_id + queued，结果需再查）。 */
export interface BacktestSubmitAck {
  run_id: number
  status: string
  async: boolean
  progress?: BacktestProgress
}

/** GET /api/backtest/{id}/progress 与 WS backtest_progress 事件的共同结构。 */
export interface BacktestProgress {
  run_id?: number
  status: string        // queued / running / completed / failed / unknown
  done?: number
  total?: number
  percent?: number
  error?: string
  message?: string
}

export async function submitBacktest(req: BacktestRequest): Promise<BacktestSubmitAck> {
  const r = await api.post<BacktestSubmitAck>('/backtest', { ...req, async_run: true },
    { timeout: 30000 })
  return r.data
}

export async function getBacktestProgress(runId: number): Promise<BacktestProgress> {
  const r = await api.get<BacktestProgress>(`/backtest/${runId}/progress`)
  return r.data
}

/** 运行清单行（GET /api/backtest）。 */
export interface BacktestRunRow {
  run_id: number
  strategy: string
  symbol: string
  freq: string
  start: string
  end: string
  status: string
  metrics: Partial<Metrics> & Record<string, unknown>
  created_at: number
  finished_at: number | null
}

export async function listBacktestRuns(limit = 50): Promise<BacktestRunRow[]> {
  const r = await api.get<BacktestRunRow[]>('/backtest', { params: { limit } })
  return r.data
}

/** 单条运行详情：完成后才带 metrics/equity，成交明细仅在进程内缓存命中时存在。 */
export interface BacktestRunDetail {
  run_id: number
  strategy: string
  symbol: string
  freq: string
  start: string
  end: string
  params: Record<string, unknown>
  status: string
  metrics: Partial<Metrics>
  equity: EquityPoint[]
  remark: string
  created_at: number
  finished_at: number | null
  progress?: BacktestProgress
  trades?: TradeRow[]
  orders?: unknown[]
  logs?: string[]
  bars?: number
  config?: Record<string, unknown>
}

export async function getBacktestRun(runId: number): Promise<BacktestRunDetail> {
  const r = await api.get<BacktestRunDetail>(`/backtest/${runId}`)
  return r.data
}


// ---------- 策略管理（D12） ----------

export async function listStrategyFiles(): Promise<string[]> {
  const r = await api.get<string[]>('/strategies/files')
  return r.data
}

export async function readStrategyFile(path: string): Promise<{ path: string; code: string }> {
  const r = await api.get('/strategies/file', { params: { path } })
  return r.data
}

export async function saveStrategyFile(path: string, code: string): Promise<{ saved: string }> {
  const r = await api.post('/strategies/file', { path, code })
  return r.data
}

export interface StrategyStatus {
  strategy_id: string
  status: string
  last_heartbeat_age_s: number | null
  restarts_last_hour: number
  recent_logs: string[]
}

export async function startStrategy(
  id: string, mode: 'paper' | 'live', params: Record<string, string | number> = {},
  freq?: string,
): Promise<{ strategy_id: string; status: string; mode?: string; freq?: string; already?: boolean }> {
  const r = await api.post(`/strategies/${encodeURIComponent(id)}/start`, { mode, params, freq })
  return r.data
}

export async function stopStrategy(id: string): Promise<{ stopped: boolean }> {
  const r = await api.post(`/strategies/${encodeURIComponent(id)}/stop`)
  return r.data
}

export async function strategyStatus(id: string): Promise<StrategyStatus> {
  const r = await api.get(`/strategies/${encodeURIComponent(id)}/status`)
  return r.data
}

// ---------- 行情（D18） ----------

export interface BarRow {
  dt: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export async function getHistory(
  symbol: string, freq: string, start?: string, end?: string,
): Promise<BarRow[]> {
  const r = await api.get<BarRow[]>('/market/history', {
    params: { symbol, freq, start, end }, timeout: 30000,
  })
  return r.data
}

export async function getFreqs(symbol: string): Promise<string[]> {
  const r = await api.get<string[]>('/market/freqs', { params: { symbol } })
  return r.data
}

export interface TickSnapshot {
  symbol: string
  dt: string
  last: number
  open: number
  high: number
  low: number
  volume: number
  amount: number
  bids: [number, number][]
  asks: [number, number][]
}

export async function getSnapshot(symbol: string): Promise<TickSnapshot | null> {
  const r = await api.get<TickSnapshot | null>('/market/snapshot', { params: { symbol } })
  return r.data
}

export interface WatchItem { symbol: string; name: string; note: string }

export async function getWatchlist(): Promise<WatchItem[]> {
  const r = await api.get<WatchItem[]>('/market/watchlist')
  return r.data
}

export async function addWatch(symbol: string, name = ''): Promise<WatchItem[]> {
  const r = await api.post<WatchItem[]>('/market/watchlist', { symbol, name })
  return r.data
}

export async function removeWatch(symbol: string): Promise<WatchItem[]> {
  const r = await api.delete<WatchItem[]>(`/market/watchlist/${encodeURIComponent(symbol)}`)
  return r.data
}

// ---------- 设置（D19） ----------

export interface AppSettings {
  simulation_only: boolean
  broker_channel: string
  commission_rate: number
  min_commission: number
  stamp_tax_sell: number
  transfer_fee: number
  akshare_min_interval: number
  tushare_min_interval: number
  // 风控热参
  max_position_value: number
  max_daily_orders: number
  // 通道韧性 / 对账
  watchdog_interval_s: number
  reconcile_interval_s: number
  backtest_async: boolean
  // 软件互操作目录
  tdx_install_dir: string
  ths_install_dir: string
  // 非官方通道启用位（服务端要求确认文本）
  enable_easytrader: boolean
  // 通道路由优先级（逗号分隔注册名，服务端按注册表校验）
  feed_priority_history: string
  feed_priority_realtime: string
  feed_priority_financial: string
}

export async function getSettings(): Promise<AppSettings> {
  const r = await api.get<AppSettings>('/settings')
  return r.data
}

/**
 * 服务端对危险字段强制校验 confirm（见 services/settings_svc.py）。
 * 前端只负责把用户输入原样带上，是否放行由服务端决定。
 */
export async function putSettings(
  patch: Partial<AppSettings>, confirm?: string,
): Promise<AppSettings> {
  const r = await api.put<AppSettings>('/settings', { ...patch, confirm })
  return r.data
}

// ---------- 合规 + 运维（D28） ----------

export interface ComplianceItem {
  id: number | null
  item: string
  category: string
  checked: boolean
  note: string
  updated_at: number
}

export async function getCompliance(): Promise<ComplianceItem[]> {
  const r = await api.get<ComplianceItem[]>('/compliance')
  return r.data
}

export async function putCompliance(
  item: string, category: string, checked: boolean, note = '',
): Promise<ComplianceItem[]> {
  const r = await api.put<ComplianceItem[]>('/compliance', { item, category, checked, note })
  return r.data
}

export function complianceExportUrl(): string {
  return '/api/compliance/export'
}

/**
 * GET /api/system/adapters 的通道面板数据。
 * 服务端已改为「通道全景」（channels 一列，含 kind/status），
 * 老契约的 feeds/brokers 两列不再返回——页面按 kind 自己分组。
 */
export interface AdaptersHealth {
  version: string
  simulation_only: boolean
  broker_channel: string
  channels: ChannelInfo[]
  priority: Record<string, string>
  limits: Record<string, number>
}

export async function getAdaptersHealth(): Promise<AdaptersHealth> {
  const r = await api.get<AdaptersHealth>('/system/adapters')
  return r.data
}

export interface LogFileInfo {
  file: string
  kind: string
  size: number
  mtime: string
}

export async function getLogFiles(): Promise<LogFileInfo[]> {
  const r = await api.get<LogFileInfo[]>('/system/logs')
  return r.data
}

// ---------- 通道全景（主流股票软件接入层） ----------

export interface ChannelHealthInfo {
  name: string
  connected: boolean
  state: string
  probes: number
  probe_failures: number
  reconnect_attempts: number
  reconnects: number
  last_ok_ms: number
  last_error: string
}

/** describe_adapters() 的一行 + ChannelHub.status() 附加的运行时观测。 */
export interface ChannelInfo {
  name: string
  kind: 'data' | 'broker'
  label: string
  prereq: string
  official: boolean
  requires: string[]
  requires_any: boolean
  missing: string[]
  deps_ready: boolean
  default_enabled: boolean
  enable_flag: string
  is_paper: boolean
  capabilities: string[]
  status: 'ready' | 'unready' | 'disabled'
  connected: boolean
  reason: string
  channel_health?: ChannelHealthInfo
}

export interface CredentialSpec {
  channel: string
  key: string
  arg: string
  required: boolean
  label: string
}

export interface MaskedCredential {
  key: string
  value_masked: string
  set: boolean
}

export interface ReconcileStatus {
  channel: string
  interval_s: number
  running: boolean
  ran_at?: number
  diffs?: number
  by_kind?: Record<string, number>
  skipped?: string
  kinds?: Record<string, string>
}

export interface ReconcileDiff {
  id: number
  ran_at: number
  channel: string
  kind: string
  ref: string
  detail: Record<string, unknown>
}

export interface ChannelsOverview {
  simulation_only: boolean
  broker_channel: string
  live_connected: boolean
  live_error: string
  history_chain: string[]
  financial_chain: string[]
  realtime_chain: string[]
  realtime_active: string
  channels: ChannelInfo[]
  reconcile: ReconcileStatus
  credentials_spec: CredentialSpec[]
}

export async function getChannels(): Promise<ChannelsOverview> {
  const r = await api.get<ChannelsOverview>('/channels', { timeout: 30000 })
  return r.data
}

export interface ProbeResult {
  name: string
  ok: boolean
  latency_ms: number
}

/** 显式探活：失败时后端抛 503，detail 是原因（用 apiError 原文展示）。 */
export async function probeChannel(name: string): Promise<ProbeResult> {
  const r = await api.post<ProbeResult>(`/channels/probe/${encodeURIComponent(name)}`, null,
    { timeout: 60000 })
  return r.data
}

export async function listCredentials(): Promise<MaskedCredential[]> {
  const r = await api.get<MaskedCredential[]>('/channels/credentials')
  return r.data
}

export async function putCredential(
  key: string, value: string,
): Promise<{ key: string; set: boolean }> {
  const r = await api.put('/channels/credentials', { key, value })
  return r.data
}

export async function deleteCredential(key: string): Promise<{ key: string; deleted: boolean }> {
  const r = await api.delete(`/channels/credentials/${encodeURIComponent(key)}`)
  return r.data
}

export interface ReconcileState {
  status: ReconcileStatus
  diffs: ReconcileDiff[]
}

export async function getReconcile(limit = 100): Promise<ReconcileState> {
  const r = await api.get<ReconcileState>('/channels/reconcile', { params: { limit } })
  return r.data
}

export async function runReconcile(): Promise<ReconcileStatus> {
  const r = await api.post<ReconcileStatus>('/channels/reconcile/run', null, { timeout: 60000 })
  return r.data
}

// ---------- 实时泵 ----------

export interface RealtimeStrategyState {
  symbols: string[]
  freq: string
}

export interface RealtimeState {
  watched: string[]
  ticks_seen: number
  bars_emitted: number
  strategies: Record<string, RealtimeStrategyState>
  realtime_source: string
}

export async function getRealtimeState(): Promise<RealtimeState> {
  const r = await api.get<RealtimeState>('/strategies/runtime/realtime')
  return r.data
}

// ---------- 软件互操作 ----------

export interface BlockTargetSpec {
  id: string
  label: string
  ext: string
  encoding: string
  format: string
  client_path: string
  import_hint: string
}

export interface RuleKindSpec {
  kind: string
  title: string
}

export interface InteropTargets {
  blocks: BlockTargetSpec[]
  rules: RuleKindSpec[]
  formula_dialects: string[]
  exports_dir: string
}

export async function getInteropTargets(): Promise<InteropTargets> {
  const r = await api.get<InteropTargets>('/interop/targets')
  return r.data
}

export interface BlockExportRequest {
  name: string
  symbols: string[]
  from_watchlist?: boolean
  targets: string[]
  write_to_client?: boolean
  bse_prefix?: string
}

export interface BlockExportFile {
  target: string
  name: string
  path: string
  bytes: number
  client_path: string
}

export interface BlockExportResult {
  count: number
  files: BlockExportFile[]
  skipped_bse: string[]
  note: string
}

export async function exportBlock(req: BlockExportRequest): Promise<BlockExportResult> {
  const r = await api.post<BlockExportResult>('/interop/export/block', req)
  return r.data
}

export interface BlockImportResult {
  parsed: number
  added: string[]
  already_present: string[]
}

export async function importBlock(target: string, content: string): Promise<BlockImportResult> {
  const r = await api.post<BlockImportResult>('/interop/import/block', { target, content })
  return r.data
}

export interface BlockDownload {
  name: string
  encoding: string
  content: string
  bytes: number
}

export async function downloadBlock(name: string): Promise<BlockDownload> {
  const r = await api.get<BlockDownload>('/interop/download', { params: { name } })
  return r.data
}

export interface FormulaResult {
  kind: string
  title: string
  dialect: string
  formula: string
}

export async function compileFormula(
  kind: string, params: Record<string, string | number>, dialect: string,
): Promise<FormulaResult> {
  const r = await api.post<FormulaResult>('/interop/formula', { kind, params, dialect })
  return r.data
}

export interface SignalDetail {
  hit: boolean
  as_of: string
  context: Record<string, unknown>
}

export interface SignalScanResult {
  rule: { kind: string; params: Record<string, unknown>; title: string }
  scanned: number
  hits: string[]
  no_local_data: string[]
  details: Record<string, SignalDetail>
}

export async function scanSignals(req: {
  kind: string
  params: Record<string, string | number>
  symbols: string[]
  from_watchlist?: boolean
}): Promise<SignalScanResult> {
  // 逐标的读 Parquet，标的多时远超默认 5s
  const r = await api.post<SignalScanResult>('/interop/signals', req, { timeout: 120000 })
  return r.data
}

// ---------- 历史日志检索 ----------

export interface LogRow {
  id: number
  ts: number
  level: string
  source: string
  strategy_id: string
  message: string
}

export interface LogQueryResult {
  total: number
  items: LogRow[]
}

export interface LogQuery {
  source?: string
  level?: string
  contains?: string
  strategy_id?: string
  since_ms?: number
  until_ms?: number
  limit?: number
  offset?: number
}

export async function queryLogs(q: LogQuery): Promise<LogQueryResult> {
  const r = await api.get<LogQueryResult>('/system/logs/query', { params: q, timeout: 20000 })
  return r.data
}

export interface LogSourceCount {
  source: string
  count: number
}

/** 历史日志「来源」过滤的事实源（不让用户手打字符串猜来源名）。 */
export async function getLogSources(): Promise<LogSourceCount[]> {
  const r = await api.get<LogSourceCount[]>('/system/logs/sources')
  return r.data
}

export interface LogStats {
  written: number
  dropped: number
  queued: number
  running: boolean
}

export async function getLogStats(): Promise<LogStats> {
  const r = await api.get<LogStats>('/system/logs/stats')
  return r.data
}
