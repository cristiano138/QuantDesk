/** API 客户端：axios 实例 + 统一事件流 WS 封装（自动重连，指数退避）。 */

import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 5000 })

/**
 * FastAPI 自己生成的英文兜底文案（不是后端作者写的 detail）。
 * 只认这几个精确串：服务端作者给的中文 detail 一律原样回显，不做改写。
 */
const BOILERPLATE_CN: Record<string, string> = {
  'Not Found': '没有找到该功能',
  'Method Not Allowed': '当前操作方式不支持',
  'Internal Server Error': '系统处理出错（详情见运行日志）',
  'Bad Gateway': '网络无响应',
  'Service Unavailable': '系统尚未就绪',
  'Unauthorized': '未授权',
  'Forbidden': '没有权限',
}

/**
 * 统一取服务端错误原文：后端把可读原因放在 HTTPException 的 detail 里
 * （如「修改 simulation_only 到危险值需输入确认文本…」），前端必须原样回显，
 * 不做任何二次判定（宪法第 2 条：安全判定在服务端）。
 *
 * 没有 detail 时不再回落 axios 英文原文（"Network Error" 这类对个人投资者
 * 等于没说）：连接层失败一律归因到「后端没起来 / 超时」，HTTP 状态码单独表述。
 */
export function apiError(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const status = e.response?.status
    const detail = (e.response?.data as { detail?: unknown } | undefined)?.detail
    const mapped = (s: string): string =>
      BOILERPLATE_CN[s] ? `${BOILERPLATE_CN[s]}（错误码 ${status ?? '—'}）` : s
    if (typeof detail === 'string' && detail) return mapped(detail)
    if (detail !== undefined && detail !== null) return JSON.stringify(detail)
    if (typeof e.response?.data === 'string' && e.response.data) return mapped(e.response.data)
    if (!e.response) {
      // 请求没拿到响应：软件未启动、连接不通、或被超时掐断
      if (e.code === 'ECONNABORTED' || e.code === 'ETIMEDOUT' || /timeout/i.test(e.message))
        return '连不上系统（请求超时）——请确认软件已启动，或点右上角重试'
      return '连不上系统（请确认软件已启动，或点右上角重试）'
    }
    return `请求失败（错误码 ${status}）——没有给出原因`
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
  /**
   * queued / running / completed / failed / cancelled / interrupted
   * + cancelling（取消已受理，引擎还没停在 bar 边界，前端继续轮询）
   * + unknown（本进程与库里都查不到）。
   */
  status: string
  done?: number
  total?: number
  percent?: number
  error?: string
  message?: string
  /** 状态取自库里的终态而非本进程内存（服务重启后回看历史运行）。 */
  source?: string
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

/** 单条运行详情：状态/指标/净值来自库，成交明细优先本进程缓存、否则回读 backtest_detail。 */
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
  /** 取消/中断的运行：明细是截至停止点的部分结果，指标一律为空。 */
  partial?: boolean
  /** 日志截断前的总条数（logs 只留最后 200 条）。 */
  logs_total?: number
  /** memory=本进程缓存 / db=已落库可回看 / none=这次没有明细。 */
  detail_source?: string
}

export async function getBacktestRun(runId: number): Promise<BacktestRunDetail> {
  const r = await api.get<BacktestRunDetail>(`/backtest/${runId}`)
  return r.data
}

/** POST /api/backtest/{id}/cancel —— 语义在 body 里，终态运行幂等返回 changed=false。 */
export interface BacktestCancelAck {
  run_id: number
  ok: boolean
  changed: boolean
  status: string
  cancelling?: boolean
  message: string
}

export async function cancelBacktest(runId: number): Promise<BacktestCancelAck> {
  const r = await api.post<BacktestCancelAck>(`/backtest/${runId}/cancel`, {})
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
  dt: string | null
  last: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  bids: [number, number][]
  asks: [number, number][]
  /** 这份盘口来自哪：realtime_pump（订阅推送）/ tx_quote（只读公开源补齐）/ none */
  source?: string
  /** source=none 时后端给的原因原文——界面要能说清"为什么没有"，不许只留空白 */
  reason?: string
}

export async function getSnapshot(symbol: string): Promise<TickSnapshot | null> {
  const r = await api.get<TickSnapshot | null>('/market/snapshot', { params: { symbol } })
  return r.data
}

/** 财务（GET /api/market/financial）：摘要=东财比率，三表=新浪逐科目；后端已转成分组结构。 */
export interface FinancialDoc {
  symbol: string
  report_type: 'indicator' | 'income' | 'balance' | 'cashflow'
  periods: string[]
  groups: { name: string; items: { indicator: string; values: Record<string, number | null> }[] }[]
  source: string
  cached: boolean
  rows: number
  note: string
}

export type FinReportType = FinancialDoc['report_type']

export const FIN_TYPE_LABELS: Record<FinReportType, string> = {
  indicator: '财务摘要',
  income: '利润表',
  balance: '资产负债表',
  cashflow: '现金流量表',
}

/** 首次是真实外呼（3~10 秒），后端缓存 5 分钟；超时给足，别让界面报"连不上后端"。 */
export async function getFinancial(symbol: string, reportType: FinReportType): Promise<FinancialDoc> {
  const r = await api.get<FinancialDoc>('/market/financial', {
    params: { symbol, report_type: reportType },
    timeout: 40000,
  })
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
  /** 运行噪音保留期（天）：只作用于运行日志与对账无差异心跳，业务数据永不自动删。 */
  log_retention_days: number
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
  /** 还没处置的差异条数（红条按这个数报警，处置完就该安静） */
  open?: number
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
  resolved: boolean
  resolve_note: string
  resolved_at: number
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

/**
 * 对账差异。diffsOnly 默认 true：每轮无差异也会落一条 kind='clean' 的审计行，
 * 它不是差异，混进列表会让「待处置」按 30s 心跳增长（一天 2880 条）而看不见真事故。
 */
export async function getReconcile(
  limit = 100,
  unresolvedOnly = false,
  diffsOnly = true,
): Promise<ReconcileState> {
  const r = await api.get<ReconcileState>('/channels/reconcile', {
    params: { limit, unresolved_only: unresolvedOnly, diffs_only: diffsOnly },
  })
  return r.data
}

/** 差异处置：标"已核实"并留一句话结论；resolve=false 退回未处置。 */
export async function resolveReconcileDiff(
  id: number,
  note = '',
  resolve = true,
): Promise<ReconcileDiff> {
  const r = await api.post<ReconcileDiff>(`/channels/reconcile/${id}/resolve`, { note, resolve })
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

// ---------------- 盯盘（只读看盘：A股 + 港股）----------------

export interface WatchSession {
  market: string
  state: 'open' | 'pre_open' | 'lunch' | 'post_close' | 'closed' | 'weekend'
  note: string
  local_time: string
  weekday: number
  weekend: boolean
}

export interface WatchRow {
  symbol: string
  name: string
  market: string
  last: number
  pct_chg: number | null
  volume: number | null
  volume_unit: string | null       // A股=手，港股=股
  amount: number | null
  outer_volume: number | null      // 官方主动买入量（外盘）
  inner_volume: number | null      // 官方主动卖出量（内盘）
  outer_pct: number | null         // 主动买入占比 %
  bids: number
  asks: number
  stale: boolean
  net_amount: number | null
  flow_source: string | null
  fetched_at: number | null
}

export interface FlowBuckets {
  [k: string]: {
    buy_volume: number
    sell_volume: number
    buy_amount: number
    sell_amount: number
    ticks: number
  }
}

export interface WatchFlow {
  symbol: string
  name: string
  market: string
  trade_date: number
  unit: string
  currency: string
  last: number
  pct_chg: number
  volume: number
  amount: number
  outer_volume: number | null
  inner_volume: number | null
  outer_pct: number | null
  tick_buy_volume: number
  tick_sell_volume: number
  tick_neutral_volume: number
  buy_amount: number
  sell_amount: number
  net_amount: number
  buckets: FlowBuckets
  minutes: { dt: string; buy: number; sell: number; neutral: number }[]
  side_map: Record<string, string>
  calibrated: boolean
  tick_vs_official_pct: number | null
  flow_source: string
  caveat: string
  ticks_total: number
  updated_at: number
}

export interface WatchStatus {
  pool: string[]
  pool_size: number
  feeds: Record<string, { ready: boolean; connected: boolean }>
  session: Record<string, WatchSession>
  trading_day: Record<string, WatchSession>
  server_time: string
  last_refresh_at: number
  last_error: string
  stats: Record<string, number>
  calibration: Record<string, Record<string, string>>
  calibrated: boolean
  flow_ready: boolean
  poll_interval: number | null
  note: string
}

export interface OrderLevel {
  0: number
  1: number
}

export interface WatchDetail {
  symbol: string
  board: string
  session: WatchSession
  snapshot: Record<string, unknown>
  book: { bids: number[][]; asks: number[][] }
  flow: WatchFlow
  feeds: Record<string, { ready: boolean; connected: boolean }>
  quote_extra?: Record<string, unknown>
  quote_extra_error?: string
  intraday?: { dt: string; price: number; avg_price: number; volume: number }[]
  intraday_error?: string
}

export interface TickRow {
  time: string
  price: number
  volume: number
  deals: number
  amount: number
  direction: string
  side_code: string
}

export interface TickPage {
  rows: TickRow[]
  total: number
  side_map: Record<string, string>
  calibrated?: boolean
  error?: string
}

export interface KlineRow {
  dt: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface KlineResult {
  symbol: string
  freq: string
  adjust: string
  source: string
  unit: string
  note: string
  rows: KlineRow[]
}

export interface FlowHistoryRow {
  id?: number | null
  trade_date: number
  symbol: string
  name: string
  market: string
  close: number
  pct_chg: number
  volume: number
  amount: number
  outer_volume: number | null
  inner_volume: number | null
  tick_buy_volume: number
  tick_sell_volume: number
  tick_neutral_volume: number
  buy_amount: number
  sell_amount: number
  big_buy_volume: number
  big_sell_volume: number
  side_map: string
  flow_source: string
  staled: boolean
}

export async function getWatchStatus(): Promise<WatchStatus> {
  const r = await api.get<WatchStatus>('/watch/status', { timeout: 20000 })
  return r.data
}

export interface WatchRefreshResult {
  count: number
  polled: boolean
  skipped: boolean
  reason: string
  list: WatchRow[]
  status: WatchStatus
}

/** 刷一整池。默认带服务端时段闸；force=true 时才绕过（手动点「立即刷新」用）。 */
export async function refreshWatch(force = false): Promise<WatchRefreshResult> {
  const r = await api.post<WatchRefreshResult>('/watch/refresh', {}, { params: { force }, timeout: 30000 })
  return r.data
}

export async function getWatchList(): Promise<{ rows: WatchRow[]; status: WatchStatus }> {
  const r = await api.get<{ rows: WatchRow[]; status: WatchStatus }>('/watch/list', { timeout: 20000 })
  return r.data
}

export async function addWatchPool(symbols: string[]): Promise<{ added: { symbol: string; name: string; board: string }[]; errors: { symbol: string; error: string }[]; pool: string[] }> {
  const r = await api.post('/watch/pool', { symbols }, { timeout: 30000 })
  return r.data
}

export async function removeWatchPool(symbol: string): Promise<{ removed: boolean; symbol: string; pool: string[] }> {
  const r = await api.delete<{ removed: boolean; symbol: string; pool: string[] }>('/watch/pool', { data: { symbol }, timeout: 20000 })
  return r.data
}

export async function getWatchDetail(symbol: string, force = false): Promise<WatchDetail> {
  // force=true 只有用户点「立即刷新」时才传：休市闸靠它区分"自动轮询"和"我真的要看最新"
  const r = await api.get<WatchDetail>(`/watch/${encodeURIComponent(symbol)}`, {
    params: force ? { force: true } : undefined,
    timeout: 60000,
  })
  return r.data
}

export async function getWatchTicks(symbol: string, q: { limit?: number; offset?: number; side?: string } = {}): Promise<TickPage> {
  const r = await api.get<TickPage>(`/watch/${encodeURIComponent(symbol)}/ticks`, { params: q, timeout: 60000 })
  return r.data
}

export async function getWatchKline(symbol: string, q: { freq?: string; days?: number; adjust?: string } = {}): Promise<KlineResult> {
  const r = await api.get<KlineResult>(`/watch/${encodeURIComponent(symbol)}/kline`, { params: q, timeout: 60000 })
  return r.data
}

export async function getWatchHistory(q: { symbol?: string; trade_date?: number; limit?: number } = {}): Promise<{ rows: FlowHistoryRow[] }> {
  const r = await api.get<{ rows: FlowHistoryRow[] }>('/watch/history', { params: q, timeout: 30000 })
  return r.data
}

/** 按名称/拼音首字母/代码搜股票（选股器；只留股票，指数/基金/窝轮已由后端剔除）。 */
export interface SymbolSearchRow {
  symbol: string
  name: string
  type: string
}

export async function searchWatchSymbols(q: string, limit = 12): Promise<SymbolSearchRow[]> {
  const r = await api.get<{ rows: SymbolSearchRow[]; error: string }>('/watch/symbols', {
    params: { q, limit },
    timeout: 15000,
  })
  return r.data.rows
}

// ---------------- 新手指引（金融术语库）----------------

export interface GuideCategory {
  id: string
  title: string
  summary: string
  count: number
}

export interface GuideTerm {
  slug: string
  term: string
  aliases: string[]
  category: string
  level: string
  one_liner: string
  explain: string
  example: string
  pitfalls: string[]
  where_to_see: string
  related: string[]
  related_terms?: { slug: string; term: string }[]
}

export async function getGuideCategories(): Promise<GuideCategory[]> {
  const r = await api.get<GuideCategory[]>('/guide/categories')
  return r.data
}

export async function getGuideTerms(q: { category?: string; q?: string } = {}): Promise<GuideTerm[]> {
  const r = await api.get<GuideTerm[]>('/guide/terms', { params: q, timeout: 20000 })
  return r.data
}

export async function getGuideTerm(slug: string): Promise<GuideTerm> {
  const r = await api.get<GuideTerm>(`/guide/terms/${encodeURIComponent(slug)}`)
  return r.data
}

// ---------------- 历史数据下载任务（数据管理页）----------------

export interface DataDownloadRequest {
  symbols: string[]
  freq?: string
  start?: string
  end?: string
  adjust?: string
}

/** 任务状态：queued → running → done / error。前端 1s 轮询直到终态。 */
export interface DataTask {
  state: 'queued' | 'running' | 'done' | 'error'
  done: number
  total: number
  current: string
  /** 成功标的 → 写入行数 */
  ok: Record<string, number>
  /** 失败标的 → 后端给的中文原因（限流/口径/依赖缺失），前端逐条上屏不吞 */
  failed: Record<string, string>
  error?: string
}

export async function startDataDownload(
  body: DataDownloadRequest,
): Promise<{ task_id: string }> {
  const r = await api.post<{ task_id: string }>('/data/download', body, { timeout: 30000 })
  return r.data
}

export async function getDataTask(taskId: string): Promise<DataTask> {
  const r = await api.get<DataTask>(`/data/task/${encodeURIComponent(taskId)}`, { timeout: 20000 })
  return r.data
}

/** 本地库清单：这台机器攒了哪些 标的×周期×复权、覆盖到哪年、成交额是不是缺失。 */
export interface InventoryRow {
  market: string
  symbol: string
  freq: string
  adjust: string
  rows: number
  files: number
  bad_files: number
  start: string
  end: string
  amount_zero_pct: number
}

export interface Inventory {
  root: string
  items: InventoryRow[]
  symbols: number
  entries: number
  rows: number
  bad_files: number
  rows_by_freq: Record<string, number>
}

export async function getDataInventory(): Promise<Inventory> {
  const r = await api.get<Inventory>('/data/inventory', { timeout: 30000 })
  return r.data
}

// ---------------- 应用内使用手册 ----------------

/** GET /api/manual/sections 的一行：目录条目（group 是左侧分组的依据）。 */
export interface ManualSection {
  slug: string
  title: string
  route: string
  nav: string
  summary: string
  group: string
}

/** GET /api/manual/{slug} 的正文：每块都是「读完能自己做」的颗粒度。 */
export interface ManualDoc {
  slug: string
  title: string
  route: string
  nav: string
  intro: string
  /** 内容可信度标记，如「已实测」/「部分待实测」——界面原样显示，不做美化 */
  status: string
  prerequisites: { what: string; why: string; where: string; ready: string }[]
  steps: { do: string; expect: string }[]
  readouts: { field: string; meaning: string }[]
  gotchas: { myth: string; truth: string }[]
  errors: { text: string; meaning: string; fix: string }[]
  faq: { q: string; a: string }[]
  links: { label: string; to: string }[]
}

/** GET /api/manual/search 的一条命中：field 是命中的分区名，snippet 是上下文。 */
export interface ManualHit {
  slug: string
  title: string
  field: string
  snippet: string
}

export async function getManualSections(): Promise<ManualSection[]> {
  const r = await api.get<ManualSection[]>('/manual/sections')
  return r.data
}

export async function getManualDoc(slug: string): Promise<ManualDoc> {
  const r = await api.get<ManualDoc>(`/manual/${encodeURIComponent(slug)}`)
  return r.data
}

export async function searchManual(q: string): Promise<{ q: string; hits: ManualHit[] }> {
  // 契约给的是 { q, hits }，但服务层 search() 直接返回数组——两种都接下来归一，
  // 免得路由写法一变，搜索就静默显示「没有命中」（口径漂移比报错更难查）。
  const r = await api.get<{ q: string; hits: ManualHit[] } | ManualHit[]>(
    '/manual/search', { params: { q }, timeout: 20000 },
  )
  const d = r.data
  const hits = Array.isArray(d) ? d : Array.isArray(d?.hits) ? d.hits : []
  return { q, hits }
}
