/**
 * 实时盯盘（只读看盘：A股 + 港股）。
 *
 * 纪律：
 * - 本页**没有任何交易入口**（后端 /api/watch 也不含下单路径），只有"看"；
 * - 所有数字都带来源与口径（unit / source / caveat 原样回显），取不到就显示 —；
 * - 请求失败把后端原文写在错误条里，绝不用假数据填充；
 * - 列表级 5s 轮询走 /watch/refresh（一次批量外呼），
 *   详情级（分时/逐笔/K线/历史）走更慢的 18–20s 循环，切标的即清 timer。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CandlestickSeries,
  HistogramSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from 'lightweight-charts'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, AxisPointerComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import {
  addWatchPool,
  apiError,
  getWatchDetail,
  getWatchHistory,
  getWatchKline,
  getWatchList,
  getWatchStatus,
  getWatchTicks,
  openEvents,
  refreshWatch,
  removeWatchPool,
  type FlowHistoryRow,
  type KlineRow,
  type TickPage,
  type WatchDetail,
  type WatchFlow,
  type WatchRow,
  type WatchStatus,
} from '../api/client'

// 按需注册（分时折线 + 买卖量柱）——全量引入使构建产物超 500KB。
// AxisPointerComponent 必须显式注册：option 里用了 tooltip.axisPointer 与顶层 axisPointer.link，
// 按需构建不会自动带进来，漏掉就在 setOption 时抛 "reading 'type'"（tsc 与 build 都不报，只有浏览器会炸）。
echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent,
  AxisPointerComponent, CanvasRenderer])

/** 列表轮询间隔可选值（ms），默认 5s。 */
const INTERVALS = [3000, 5000, 10000, 30000]
const DETAIL_MS = 18000   // 详情（外呼单标的：快照+逐笔+分时）
const TICKS_MS = 20000    // 逐笔明细分页（每次重取全量逐笔，别打太密）
const KLINE_MS = 60000    // K 线：历史日线盘中变化慢，60s 足够
const HISTORY_MS = 60000  // 收盘定格（本地 SQLite）
const STATUS_MS = 15000   // 盯盘状态（服务端本地读，不外呼行情）
const PAGE = 100

const SIDE_CN: Record<string, string> = {
  buy: '主动买',
  sell: '主动卖',
  neutral: '中性/竞价',
  unknown: '未定',
}

/** 分层阈值见 services/watch.py（单笔成交额），阈值写死在后端，这里只复述。 */
const BUCKETS: { key: string; label: string; range: string }[] = [
  { key: 'small', label: '小单', range: '成交额 < 4 万' },
  { key: 'medium', label: '中单', range: '4 万 ~ 20 万' },
  { key: 'big', label: '大单', range: '20 万 ~ 100 万' },
  { key: 'huge', label: '特大单', range: '≥ 100 万' },
]

const STATE_CN: Record<string, string> = {
  open: '盘中',
  pre_open: '开盘前',
  lunch: '午间休市',
  post_close: '盘后',
  closed: '未开盘',
  weekend: '周末休市',
}

const MARKET_CN: Record<string, string> = { SH: '沪', SZ: '深', BJ: '北', HK: '港' }

/** 后端两个源都把 volume_unit 写成 lot/share（不是中文），这里统一成中文单位。 */
const UNIT_CN: Record<string, string> = { lot: '手', share: '股', 手: '手', 股: '股' }
const CUR_CN: Record<string, string> = { CNY: '元', HKD: '港元', 元: '元', 港元: '港元' }
const ADJUST_CN: Record<string, string> = { forward: '前复权', none: '不复权' }

type ErrKey = 'status' | 'list' | 'detail' | 'ticks' | 'kline' | 'history' | 'pool'
const ERR_LABEL: Record<ErrKey, string> = {
  status: '盯盘状态',
  list: '盯盘列表',
  detail: '标的详情',
  ticks: '逐笔明细',
  kline: 'K 线',
  history: '历史定格',
  pool: '标的池',
}

function unitCn(u: string | null | undefined): string {
  if (!u) return ''
  return UNIT_CN[u] ?? u
}

function curCn(c: string | null | undefined): string {
  if (!c) return '元'
  return CUR_CN[c] ?? c
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits)
}

function fmtPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

/** 量：万/亿 + 单位（手/股），小白可读，原始值放 title 里。 */
function fmtQty(v: number | null | undefined, unit: string): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿${unit}`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万${unit}`
  return `${v.toFixed(0)}${unit}`
}

function fmtMoney(v: number | null | undefined, cur: string): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿${cur}`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万${cur}`
  return `${v.toFixed(2)}${cur}`
}

function fmtTsMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return '—'
  return new Date(ms).toLocaleTimeString('zh-CN')
}

/** trade_date 是 YYYYMMDD 整数。 */
function fmtDate8(v: number | null | undefined): string {
  const s = String(v ?? '')
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : '—'
}

/** 快照与个股字段都是 Record<string, unknown>：按键顺序取第一个有限数，取不到给 null。 */
function pickNum(objs: (Record<string, unknown> | null | undefined)[], keys: string[]): number | null {
  for (const o of objs) {
    if (!o) continue
    for (const k of keys) {
      const v = o[k]
      if (typeof v === 'number' && Number.isFinite(v)) return v
    }
  }
  return null
}

/** 后端 400 时把逐条失败放在 detail 数组里，apiError 会 JSON.stringify——还原成一行话。 */
function poolErrText(e: unknown): string {
  const t = apiError(e)
  try {
    const parsed: unknown = JSON.parse(t)
    if (Array.isArray(parsed)) {
      return parsed
        .map((x) => {
          const o = x as { symbol?: string; error?: string }
          return typeof o?.symbol === 'string' ? `${o.symbol}：${o.error ?? '失败'}` : String(x)
        })
        .join('；')
    }
  } catch {
    /* 不是 JSON 就原样返回 */
  }
  return t
}

function pctCls(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v === 0) return ''
  return v > 0 ? 'up' : 'down'
}

// ---------------- 分时图（ECharts：价格 + 均价 + 分钟买卖量柱） ----------------

interface IntradayPoint {
  dt: string
  price: number
  avg_price: number
  volume: number
}
interface FlowMinute {
  dt: string
  buy: number
  sell: number
  neutral: number
}

function IntradayChart({
  points, minutes, unit,
}: {
  points: IntradayPoint[]
  minutes: FlowMinute[]
  unit: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<ReturnType<typeof echarts.init> | null>(null)

  useEffect(() => {
    if (!ref.current) return
    instRef.current = echarts.init(ref.current)
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    // 容器宽度随详情区变化（切标的/折叠窗口），只监听 window 会漏
    let ro: ResizeObserver | null = null
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(onResize)
      ro.observe(ref.current)
    }
    return () => {
      window.removeEventListener('resize', onResize)
      ro?.disconnect()
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = instRef.current
    if (!chart) return
    const set = new Set<string>()
    points.forEach((p) => set.add(p.dt))
    minutes.forEach((m) => set.add(m.dt))
    const cats = Array.from(set).sort()
    const price = new Map(points.map((p) => [p.dt, p.price]))
    const avg = new Map(points.map((p) => [p.dt, p.avg_price]))
    const buy = new Map(minutes.map((m) => [m.dt, m.buy]))
    const sell = new Map(minutes.map((m) => [m.dt, m.sell]))
    const neutral = new Map(minutes.map((m) => [m.dt, m.neutral]))
    const at = (m: Map<string, number>) => cats.map((c) => (m.has(c) ? m.get(c) as number : null))

    chart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: ['价格', '均价', '主动买量', '主动卖量'], top: 0 },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: 62, right: 16, top: 26, height: '52%' },
        { left: 62, right: 16, top: '70%', height: '20%' },
      ],
      xAxis: [
        { type: 'category', data: cats, gridIndex: 0, boundaryGap: false, axisLabel: { show: false } },
        { type: 'category', data: cats, gridIndex: 1, boundaryGap: true },
      ],
      // 每个轴都要显式 gridIndex：默认全落 grid 0，柱系列挂在 grid 1 上就会取到不属于它的轴
      yAxis: [
        { type: 'value', gridIndex: 0, scale: true, name: '价格' },
        { type: 'value', gridIndex: 1, name: `量(${unit})`, splitNumber: 2 },
      ],
      series: [
        { name: '价格', type: 'line', data: at(price), showSymbol: false, lineStyle: { width: 1.4, color: '#2563eb' } },
        { name: '均价', type: 'line', data: at(avg), showSymbol: false, lineStyle: { width: 1.2, type: 'dashed', color: '#f59e0b' } },
        { name: '主动买量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, stack: 'f', data: at(buy), itemStyle: { color: '#f87171' } },
        { name: '主动卖量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, stack: 'f', data: at(sell), itemStyle: { color: '#4ade80' } },
        { name: '中性量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, stack: 'f', data: at(neutral), itemStyle: { color: '#cbd5e1' } },
      ],
    } as echarts.EChartsCoreOption, true)
  }, [points, minutes, unit])

  return <div ref={ref} style={{ width: '100%', height: 340 }} />
}

// ---------------- K 线（lightweight-charts：蜡烛 + 成交量副图，同 MarketPage 用法） ----------------

function KLineChart({ rows }: { rows: KlineRow[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = createChart(ref.current, {
      height: 420,
      layout: { background: { color: '#ffffff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f1f3f6' }, horzLines: { color: '#f1f3f6' } },
      crosshair: { mode: 0 },
      timeScale: { borderColor: '#e5e7eb' },
      rightPriceScale: { borderColor: '#e5e7eb' },
    })
    chartRef.current = chart
    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor: '#dc2626', downColor: '#16a34a',
      borderUpColor: '#dc2626', borderDownColor: '#16a34a',
      wickUpColor: '#dc2626', wickDownColor: '#16a34a',
    })
    volRef.current = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' }, priceScaleId: 'vol',
    })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    const onResize = () => chart.applyOptions({ width: ref.current?.clientWidth ?? 600 })
    window.addEventListener('resize', onResize)
    onResize()
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      volRef.current = null
    }
  }, [])

  useEffect(() => {
    const c = candleRef.current
    const v = volRef.current
    if (!c || !v) return
    // 后端按日期升序返回；同日重复（本地 + 源拼接）留最后一条，否则 lightweight-charts 报错
    const byDay = new Map<string, KlineRow>()
    rows.forEach((r) => byDay.set(r.dt.slice(0, 10), r))
    const data = Array.from(byDay.values()).sort((a, b) => a.dt.localeCompare(b.dt))
    c.setData(data.map((r) => ({ time: r.dt.slice(0, 10), open: r.open, high: r.high, low: r.low, close: r.close })))
    v.setData(data.map((r) => ({
      time: r.dt.slice(0, 10),
      value: r.volume,
      color: r.close >= r.open ? '#fca5a5' : '#86efac',
    })))
    chartRef.current?.timeScale().fitContent()
  }, [rows])

  return <div ref={ref} style={{ width: '100%' }} />
}

// ---------------- 五档盘口（卖上买下，量条按该档最大量归一化） ----------------

function OrderBook({ detail }: { detail: WatchDetail | null }) {
  const bids = detail?.book?.bids ?? []
  const asks = detail?.book?.asks ?? []
  const note = detail?.session?.note ?? ''
  if (bids.length === 0 && asks.length === 0) {
    return (
      <p className="placeholder">
        五档盘口：本次详情未返回档位（{note || '时段未知'}）。
        港股收盘后接口把档位给 0 属正常；A股收盘后通常仍保留收盘时的挂单档位，
        整池都是空档才需要怀疑通道。
      </p>
    )
  }
  const maxVol = Math.max(1, ...bids.map((b) => b[1] ?? 0), ...asks.map((a) => a[1] ?? 0))
  const unit = unitCn(
    (detail?.snapshot?.volume_unit as string | undefined)
    ?? (detail?.quote_extra?.volume_unit as string | undefined)
    ?? detail?.flow?.unit,
  )
  const bar = (vol: number, side: 'bid' | 'ask') => (
    <div className="ob-bar">
      <div className={side === 'bid' ? 'ob-fill bid' : 'ob-fill ask'} style={{ width: `${(vol / maxVol) * 100}%` }} />
    </div>
  )
  const rows = []
  for (let i = 4; i >= 0; i--) {
    const a = asks[i]
    rows.push(
      <tr key={`a${i}`}>
        <td>卖{i + 1}</td>
        <td className="down">{a ? fmtNum(a[0]) : '—'}</td>
        <td>{a ? `${a[1]}${unit}` : '—'}</td>
        <td>{bar(a?.[1] ?? 0, 'ask')}</td>
      </tr>,
    )
  }
  for (let i = 0; i < 5; i++) {
    const b = bids[i]
    rows.push(
      <tr key={`b${i}`}>
        <td>买{i + 1}</td>
        <td className="up">{b ? fmtNum(b[0]) : '—'}</td>
        <td>{b ? `${b[1]}${unit}` : '—'}</td>
        <td>{bar(b?.[1] ?? 0, 'bid')}</td>
      </tr>,
    )
  }
  return (
    <>
      <table className="ob-table">
        <thead>
          <tr><th>档位</th><th>价格</th><th>量({unit || '—'})</th><th>相对压力</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p className="sub-note">{note}——价位单位随标的计价币种（A股=元，港股=港元），量单位 {unit || '—'}。
        港股收盘后档位给 0 属正常；A股收盘后仍会保留收盘时的挂单，判断新旧看「抓取时间」而不是看档位空不空。</p>
    </>
  )
}

// ---------------- 当日买卖量（主口径=官方内外盘，分层来自逐笔含盘后） ----------------

function FlowPanel({ flow }: { flow: WatchFlow | null }) {
  if (!flow) return <p className="placeholder">买卖量：等待标的详情。</p>
  const unit = unitCn(flow.unit)
  const cur = curCn(flow.currency)
  const outer = flow.outer_volume
  const inner = flow.inner_volume
  const hasOfficial = outer != null || inner != null
  const maxBar = Math.max(1, outer ?? 0, inner ?? 0)
  const buckets = flow.buckets ?? {}
  const sideMap = flow.side_map ?? {}
  const tickTotal = flow.tick_buy_volume + flow.tick_sell_volume + flow.tick_neutral_volume

  return (
    <div>
      <div className="row-btns">
        <h3 style={{ margin: 0 }}>当日买卖量</h3>
        <span className="tag">主口径=官方外盘/内盘</span>
        <span className="tag">量单位：{unit || flow.unit || '—'}</span>
        <span className="tag">金额单位：{cur}</span>
        <span className="tag">来源：{flow.flow_source || '—'}</span>
        <span className={flow.calibrated ? 'badge badge-paper' : 'badge badge-live'}>
          方向码{flow.calibrated ? '已与官方校准' : '未与官方对齐'}
        </span>
      </div>

      {hasOfficial ? (
        <table className="ob-table">
          <tbody>
            <tr>
              <td style={{ width: 96 }}>外盘（主动买）</td>
              <td className="up" style={{ width: 120 }}>{fmtQty(outer, unit)}</td>
              <td>{barRow(maxBar, outer ?? 0, 'bid')}</td>
            </tr>
            <tr>
              <td>内盘（主动卖）</td>
              <td className="down" style={{ width: 120 }}>{fmtQty(inner, unit)}</td>
              <td>{barRow(maxBar, inner ?? 0, 'ask')}</td>
            </tr>
          </tbody>
        </table>
      ) : (
        <p className="warn sub-note">
          本标的暂未取到官方外盘/内盘（买卖量仅有逐笔聚合，含盘后差异）——不给占比假象。
        </p>
      )}

      <div className="metric-cards" style={{ marginTop: 10 }}>
        <div className="card metric-card">
          <span className="metric-label">主动买占比（外盘 / 总量）</span>
          <span className="metric-value">{flow.outer_pct == null ? '—' : `${flow.outer_pct.toFixed(2)}%`}</span>
        </div>
        <div className="card metric-card">
          <span className="metric-label">总成交量</span>
          <span className="metric-value">{fmtQty(flow.volume, unit)}</span>
        </div>
        <div className="card metric-card">
          <span className="metric-label">总成交额</span>
          <span className="metric-value">{fmtMoney(flow.amount, cur)}</span>
        </div>
        <div className="card metric-card">
          <span className="metric-label">逐笔净额的差（买额−卖额）</span>
          <span className={`metric-value ${pctCls(flow.net_amount)}`}>{fmtMoney(flow.net_amount, cur)}</span>
        </div>
      </div>

      <h4 style={{ margin: '10px 0 4px', fontSize: 13 }}>
        逐笔分层明细（小/中/大/特大 × 买/卖，按单笔成交额分档；下列合计含盘后成交）
      </h4>
      {Object.keys(buckets).length === 0 ? (
        <p className="placeholder">无逐笔分层数据（东财逐笔通道未就绪，或当日无逐笔）。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>分层</th><th>阈值</th>
              <th>买量({unit})</th><th>买额({cur})</th>
              <th>卖量({unit})</th><th>卖额({cur})</th><th>笔数</th>
            </tr>
          </thead>
          <tbody>
            {BUCKETS.map((b) => {
              const x = buckets[b.key]
              if (!x) return null
              return (
                <tr key={b.key}>
                  <td>{b.label}</td>
                  <td className="sub-note" style={{ margin: 0 }}>{b.range}</td>
                  <td className="up">{fmtQty(x.buy_volume, unit)}</td>
                  <td className="up">{fmtMoney(x.buy_amount, cur)}</td>
                  <td className="down">{fmtQty(x.sell_volume, unit)}</td>
                  <td className="down">{fmtMoney(x.sell_amount, cur)}</td>
                  <td>{x.ticks}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      <p className="sub-note">
        逐笔合计（买+卖+中性）= {fmtQty(tickTotal, unit)}，买 {fmtQty(flow.tick_buy_volume, unit)} /
        卖 {fmtQty(flow.tick_sell_volume, unit)} / 中性 {fmtQty(flow.tick_neutral_volume, unit)}；
        逐笔共 {flow.ticks_total} 笔，更新于 {fmtTsMs(flow.updated_at)}。
      </p>
      <p className="sub-note">
        逐笔合计相对官方总量差 {flow.tick_vs_official_pct == null ? '—' : `${flow.tick_vs_official_pct > 0 ? '+' : ''}${flow.tick_vs_official_pct.toFixed(2)}%`}
        ——这是预期内的口径差（逐笔包含盘后成交，官方内外盘不含），不是数据错误；所以买卖量以官方内外盘为准，逐笔只做明细与分层。
      </p>
      <p className="sub-note">
        方向码映射：
        {Object.keys(sideMap).length === 0
          ? '（暂无，未取到逐笔）'
          : Object.entries(sideMap).map(([code, side]) => ` ${code}=${SIDE_CN[side] ?? side}`).join('，')}
      </p>
      {!flow.calibrated && (
        <p className="warn sub-note">
          逐笔分层未与官方内外盘对齐（误差过大或样本不足）——分层数字仅按逐笔口径给出，不能当作权威买卖力量。
        </p>
      )}
      <p className="sub-note">{flow.caveat}</p>
    </div>
  )
}

function barRow(max: number, v: number, side: 'bid' | 'ask') {
  return (
    <div className="ob-bar">
      <div className={side === 'bid' ? 'ob-fill bid' : 'ob-fill ask'} style={{ width: `${(v / max) * 100}%` }} />
    </div>
  )
}

// ---------------- 页面 ----------------

export default function WatchPage() {
  const [status, setStatus] = useState<WatchStatus | null>(null)
  const [rows, setRows] = useState<WatchRow[]>([])
  const [symbol, setSymbol] = useState('')
  const [detail, setDetail] = useState<WatchDetail | null>(null)
  const [tickPage, setTickPage] = useState<TickPage | null>(null)
  const [tickSide, setTickSide] = useState('')
  const [tickOffset, setTickOffset] = useState(0)
  const [klineRows, setKlineRows] = useState<KlineRow[]>([])
  const [klineMeta, setKlineMeta] = useState<{ source: string; note: string; unit: string; adjust: string; freq: string } | null>(null)
  const [klineFreq, setKlineFreq] = useState('1d')
  const [klineDays, setKlineDays] = useState(250)
  const [klineReload, setKlineReload] = useState(0)
  const [hist, setHist] = useState<FlowHistoryRow[]>([])
  const [auto, setAuto] = useState(true)
  const [intervalMs, setIntervalMs] = useState(5000)
  const [input, setInput] = useState('')
  const [poolMsg, setPoolMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [idleNote, setIdleNote] = useState('')
  const [errors, setErrors] = useState<Record<ErrKey, string>>({
    status: '', list: '', detail: '', ticks: '', kline: '', history: '', pool: '',
  })

  const setErr = useCallback((k: ErrKey, msg: string) => {
    setErrors((cur) => (cur[k] === msg ? cur : { ...cur, [k]: msg }))
  }, [])

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const listBusy = useRef(false)
  const listAt = useRef(0)

  /** 列表级刷新：一次批量外呼拿整池快照 + 服务端状态；失败退回本地快照并如实报原因。
   *  force=false 时服务端自带时段闸（休市不外呼），页面通宵开着也不打公开接口。 */
  const loadList = useCallback(async (force = false) => {
    if (listBusy.current) return
    listBusy.current = true
    listAt.current = Date.now()
    try {
      const r = await refreshWatch(force)
      if (!mounted.current) return
      setRows(r.list)
      setStatus(r.status)
      setIdleNote(r.skipped ? `休市中：${r.reason || '未外呼，显示当日最后一次快照'}` : '')
      setErr('list', '')
      setSymbol((cur) => cur || (r.list[0]?.symbol ?? ''))
    } catch (e) {
      const first = apiError(e)
      try {
        const b = await getWatchList()
        if (!mounted.current) return
        setRows(b.rows)
        setStatus(b.status)
        setErr('list', `实时刷新失败，以下列表是本地最近一次快照（非实时）：${first}`)
      } catch (e2) {
        if (mounted.current) setErr('list', `${first}；本地快照兜底也失败：${apiError(e2)}`)
      }
    } finally {
      listBusy.current = false
    }
  }, [setErr])

  const loadStatus = useCallback(async () => {
    try {
      const st = await getWatchStatus()
      if (mounted.current) { setStatus(st); setErr('status', '') }
    } catch (e) {
      if (mounted.current) setErr('status', apiError(e))
    }
  }, [setErr])

  const manualRefresh = useCallback(async () => {
    setBusy(true)
    try {
      await loadList()
    } finally {
      if (mounted.current) setBusy(false)
    }
  }, [loadList])

  // ---- 进页面先拿状态（含时段/通道灯），再刷一次列表 ----
  useEffect(() => {
    let alive = true
    getWatchStatus()
      .then((st) => { if (alive) { setStatus(st); setErr('status', '') } })
      .catch((e: unknown) => { if (alive) setErr('status', apiError(e)) })
    void loadList()
    return () => { alive = false }
  }, [loadList])

  // ---- 状态轮询（服务端本地读，不打外部行情）----
  useEffect(() => {
    const t = setInterval(loadStatus, STATUS_MS)
    return () => clearInterval(t)
  }, [loadStatus])

  // ---- 列表轮询（自动开关 + 间隔可调）----
  useEffect(() => {
    if (!auto) return
    const t = setInterval(loadList, intervalMs)
    return () => clearInterval(t)
  }, [auto, intervalMs, loadList])

  // ---- WS：收到 watch_update 立刻拉一次（保留轮询兜底，不重复建连接）----
  useEffect(() => {
    const close = openEvents((data) => {
      const d = data as { type?: string; data?: { action?: string } }
      if (d?.type !== 'log' || !d.data?.action) return
      const action = d.data.action
      if (action === 'watch_update' && Date.now() - listAt.current > 2000) void loadList()
      else if (action === 'flow_frozen') void loadList()
    })
    return () => close()
  }, [loadList])

  // ---- 详情轮询（外呼单标的：快照 + 逐笔买卖量 + 分时 + 个股字段）----
  useEffect(() => {
    if (!symbol) return
    let alive = true
    let inFlight = false
    setDetail(null)
    setErr('detail', '')
    const run = () => {
      if (inFlight) return
      inFlight = true
      getWatchDetail(symbol)
        .then((d) => { if (alive) { setDetail(d); setErr('detail', '') } })
        .catch((e: unknown) => { if (alive) setErr('detail', apiError(e)) })
        .finally(() => { inFlight = false })
    }
    run()
    const t = auto ? setInterval(run, DETAIL_MS) : undefined
    return () => { alive = false; if (t !== undefined) clearInterval(t) }
  }, [symbol, auto, setErr])

  // ---- 逐笔明细分页（最新在前）----
  useEffect(() => {
    if (!symbol) return
    let alive = true
    let inFlight = false
    setTickPage(null)
    setErr('ticks', '')
    const run = () => {
      if (inFlight) return
      inFlight = true
      getWatchTicks(symbol, { limit: PAGE, offset: tickOffset, side: tickSide || undefined })
        .then((p) => {
          if (!alive) return
          setTickPage(p)
          setErr('ticks', p.error ? `逐笔明细：${p.error}` : '')
        })
        .catch((e: unknown) => { if (alive) setErr('ticks', apiError(e)) })
        .finally(() => { inFlight = false })
    }
    run()
    const t = auto ? setInterval(run, TICKS_MS) : undefined
    return () => { alive = false; if (t !== undefined) clearInterval(t) }
  }, [symbol, tickSide, tickOffset, auto, setErr])

  // ---- K 线（历史：A股优先本地 Parquet，缺数据回落腾讯）----
  useEffect(() => {
    if (!symbol) return
    let alive = true
    let inFlight = false
    setKlineRows([])
    setKlineMeta(null)
    setErr('kline', '')
    const run = () => {
      if (inFlight) return
      inFlight = true
      getWatchKline(symbol, { freq: klineFreq, days: klineDays })
        .then((k) => {
          if (!alive) return
          setKlineRows(k.rows)
          setKlineMeta({ source: k.source, note: k.note, unit: k.unit, adjust: k.adjust, freq: k.freq })
          setErr('kline', '')
        })
        .catch((e: unknown) => { if (alive) setErr('kline', apiError(e)) })
        .finally(() => { inFlight = false })
    }
    run()
    const t = setInterval(run, KLINE_MS)
    return () => { alive = false; clearInterval(t) }
  }, [symbol, klineFreq, klineDays, klineReload, setErr])

  // ---- 当日定格 / 历史（本地 SQLite，收盘后仍可回看）----
  useEffect(() => {
    if (!symbol) return
    let alive = true
    getWatchHistory({ symbol, limit: 30 })
      .then((r) => { if (alive) { setHist(r.rows); setErr('history', '') } })
      .catch((e: unknown) => { if (alive) setErr('history', apiError(e)) })
    const t = setInterval(() => {
      getWatchHistory({ symbol, limit: 30 })
        .then((r) => { if (alive) setHist(r.rows) })
        .catch(() => undefined)
    }, HISTORY_MS)
    return () => { alive = false; clearInterval(t) }
  }, [symbol, setErr])

  const addPool = async () => {
    const parts = input.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    if (parts.length === 0) return
    setBusy(true)
    setPoolMsg('')
    setErr('pool', '')
    try {
      const r = await addWatchPool(parts)
      const errs = r.errors ?? []
      setPoolMsg(
        `已加入 ${r.added.length} 只${errs.length ? `；失败 ${errs.length} 条：` + errs.map((x) => `${x.symbol}：${x.error}`).join('；') : ''}`,
      )
      setInput('')
      await loadList()
    } catch (e) {
      setErr('pool', poolErrText(e))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const removePool = async (s: string) => {
    setBusy(true)
    setErr('pool', '')
    try {
      await removeWatchPool(s)
      if (s === symbol) setSymbol('')
      setDetail(null)
      await loadList()
    } catch (e) {
      setErr('pool', `移除 ${s} 失败：${poolErrText(e)}`)
      await loadList()
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  // ---- 渲染 ----
  const errList = (Object.keys(errors) as ErrKey[])
    .filter((k) => errors[k])
    .map((k) => ({ k, msg: errors[k] }))
  const liveErr = status?.last_error ?? ''
  const sess = status?.session
  const tday = status?.trading_day
  const feeds = status?.feeds ?? {}

  const snap = detail?.snapshot ?? {}
  const extra = detail?.quote_extra ?? {}
  const srcs = [extra, snap]
  const isHk = (detail?.symbol ?? (snap.symbol as string | undefined) ?? '').endsWith('.HK')
  const flowUnit = unitCn(detail?.flow?.unit ?? (snap.volume_unit as string | undefined))
  const flowCur = curCn(detail?.flow?.currency)
  const last = pickNum(srcs, ['last'])
  const prevClose = pickNum(srcs, ['prev_close'])
  const pct = pickNum(srcs, ['pct_chg'])

  const metrics: { label: string; value: string; cls?: string }[] = [
    { label: '最新价', value: fmtNum(last, 3), cls: pctCls(pct) },
    { label: '涨跌幅', value: fmtPct(pct), cls: pctCls(pct) },
    { label: '涨跌额', value: last != null && prevClose != null ? fmtNum(last - prevClose, 3) : '—', cls: pctCls(pct) },
    { label: '今开', value: fmtNum(pickNum(srcs, ['open']), 3) },
    { label: '昨收', value: fmtNum(prevClose, 3) },
    { label: '最高', value: fmtNum(pickNum(srcs, ['high']), 3) },
    { label: '最低', value: fmtNum(pickNum(srcs, ['low']), 3) },
    { label: `成交量(${flowUnit || '—'})`, value: fmtQty(pickNum(srcs, ['volume']), flowUnit) },
    { label: `成交额(${flowCur})`, value: fmtMoney(pickNum(srcs, ['amount']), flowCur) },
    { label: '换手率', value: pickNum(srcs, ['turnover_rate']) == null ? '—' : `${fmtNum(pickNum(srcs, ['turnover_rate']))}%` },
    { label: '振幅', value: pickNum(srcs, ['amplitude']) == null ? '—' : `${fmtNum(pickNum(srcs, ['amplitude']))}%` },
    { label: '均价', value: fmtNum(pickNum(srcs, ['avg_price']), 3) },
    { label: '市盈率(动/TTM)', value: fmtNum(pickNum(srcs, ['pe_dynamic', 'pe_ttm'])) },
    { label: '市净率', value: fmtNum(pickNum(srcs, ['pb'])) },
    { label: '量比', value: fmtNum(pickNum(srcs, ['vol_ratio'])) },
    // 港股没有涨跌停；东财对 .HK 复用同一对下标给 52 周高/低，后端已按市场分键输出，
    // 这里跟着换标签——把 52 周区间显示成「涨停/跌停」是会把小白吓到的
    isHk
      ? { label: '52周最高 / 最低', value: `${fmtNum(pickNum(srcs, ['wk52_high']), 2)} / ${fmtNum(pickNum(srcs, ['wk52_low']), 2)}` }
      : { label: '涨停 / 跌停', value: `${fmtNum(pickNum(srcs, ['limit_up']), 2)} / ${fmtNum(pickNum(srcs, ['limit_down']), 2)}` },
  ]

  const intraday = detail?.intraday ?? []
  const minutes = detail?.flow?.minutes ?? []
  const staleRows = rows.filter((r) => r.stale).length

  return (
    <section>
      <h2>实时盯盘（只读看盘：A股 + 港股，本页不含任何交易功能）</h2>

      {(errList.length > 0 || liveErr) && (
        <div className="alert-banner">
          {liveErr && <p><b>服务端最近一次刷新失败原文：</b>{liveErr}</p>}
          {errList.map(({ k, msg }) => (
            <p key={k}><b>{ERR_LABEL[k]}：</b>{msg}</p>
          ))}
        </div>
      )}

      {/* ---------- 顶部状态条 ---------- */}
      <div className="card">
        <div className="filter-row">
          <span className="badge">服务器时间 {status?.server_time ?? '—'}</span>
          {sess?.CN && (
            <span className={sess.CN.state === 'open' ? 'badge badge-paper' : 'badge'}>
              A股：{STATE_CN[sess.CN.state] ?? sess.CN.state}
              {sess.CN.note && sess.CN.note !== (STATE_CN[sess.CN.state] ?? sess.CN.state)
                ? `｜${sess.CN.note}` : ''}
            </span>
          )}
          {sess?.HK && (
            <span className={sess.HK.state === 'open' ? 'badge badge-paper' : 'badge'}>
              港股：{STATE_CN[sess.HK.state] ?? sess.HK.state}
              {sess.HK.note && sess.HK.note !== (STATE_CN[sess.HK.state] ?? sess.HK.state)
                ? `｜${sess.HK.note}` : ''}
            </span>
          )}
          {tday?.CN && sess?.CN && tday.CN.state !== sess.CN.state && (
            <span className="badge badge-live">交易日口径 A股：{tday.CN.note}</span>
          )}
          {tday?.HK && sess?.HK && tday.HK.state !== sess.HK.state && (
            <span className="badge badge-live">交易日口径 港股：{tday.HK.note}</span>
          )}
          {Object.entries(feeds).map(([name, f]) => (
            <span key={name} className="badge">
              <span className={f?.ready ? (f.connected ? 'lamp lamp-ready' : 'lamp lamp-unready') : 'lamp lamp-down'} />
              {name}：{f?.ready ? (f.connected ? '就绪·已连接' : '就绪·未连接') : '未就绪'}
            </span>
          ))}
          <span className={status?.calibrated ? 'badge badge-paper' : 'badge badge-live'}>
            方向码{status?.calibrated ? '已校准' : '未校准'}
          </span>
          <label className="check-line" style={{ flexDirection: 'row' }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            自动刷新
          </label>
          <label className="pick-item">
            间隔
            <select value={intervalMs} onChange={(e) => setIntervalMs(Number(e.target.value))}>
              {INTERVALS.map((ms) => <option key={ms} value={ms}>{ms / 1000} 秒</option>)}
            </select>
          </label>
          <button className="mini" disabled={busy} onClick={() => void manualRefresh()}>
            {busy ? '刷新中…' : '立即刷新'}
          </button>
        </div>
        <p className="sub-note">
          盯盘池 {status?.pool_size ?? 0} 只 · 列表刷新 {status?.last_refresh_at ? fmtTsMs(status.last_refresh_at) : '尚未刷新'}
          · 陈旧行 {staleRows}（陈旧=最近一次没取到实时快照，显示的是上一次值）
          {detail?.snapshot.stale ? ' · 当前标的是旧快照' : ''}
        </p>
        {idleNote ? <p className="warn">{idleNote}</p> : null}
        <p className="sub-note">
          服务端累计：快照 {status?.stats?.snapshots ?? '—'} 行 · 逐笔调用 {status?.stats?.flow_calls ?? '—'} 次 ·
          失败 {status?.stats?.errors ?? '—'} 次 · 陈旧 {status?.stats?.stale ?? '—'} 次 ·
          {status?.note ?? ''}
        </p>
      </div>

      {/* ---------- 标的池管理 ---------- */}
      <div className="card">
        <div className="row-btns">
          <h3>盯盘标的池</h3>
          <div className="watch-add" style={{ flex: 1, minWidth: 260 }}>
            <input
              placeholder="代码逗号分隔批量添加，如 600000.SH,00700.HK（也支持 600519、hk00700，后端会归一化）"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void addPool() }}
            />
            <button disabled={busy} onClick={() => void addPool()}>添加</button>
          </div>
        </div>
        {poolMsg && <p className="sub-note">{poolMsg}</p>}
        {status?.pool && status.pool.length > 0 && (
          <div className="watch-pick">
            {status.pool.map((s) => (
              <button key={s} className="pick-item mini" onClick={() => setSymbol(s)}>{s}</button>
            ))}
          </div>
        )}
        {status && status.pool.length === 0 && (
          <p className="placeholder">盯盘池为空：添加代码后才会外呼行情（空池不外呼）。</p>
        )}
      </div>

      {/* ---------- 盯盘列表 ---------- */}
      <div className="card">
        <div className="row-btns">
          <h3>盯盘列表（按成交额降序）</h3>
          <span className="sub-note">点击行切换标的 · 红涨绿跌 · 灰底行=陈旧数据</span>
        </div>
        {rows.length === 0 ? (
          <p className="placeholder">列表为空：池子为空，或行情通道未就绪（原因见上方错误条原文）。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>代码</th><th>名称</th><th>市场</th><th>最新价</th><th>涨跌幅</th>
                <th>成交量</th><th>成交额</th><th>外盘(主动买)</th><th>内盘(主动卖)</th>
                <th>主动买占比</th><th>五档买/卖</th><th>抓取时间</th><th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const u = unitCn(r.volume_unit)
                const cur = r.symbol.endsWith('.HK') ? '港元' : '元'
                return (
                  <tr
                    key={r.symbol}
                    className={r.symbol === symbol ? 'row-active' : undefined}
                    style={r.stale ? { opacity: 0.6 } : undefined}
                    onClick={() => setSymbol(r.symbol)}
                  >
                    <td className="mono">{r.symbol}</td>
                    <td>{r.name || '—'}</td>
                    <td><span className="tag">{MARKET_CN[r.market] ?? r.market ?? '—'}</span></td>
                    <td className={pctCls(r.pct_chg)}>{fmtNum(r.last, 3)}</td>
                    <td className={pctCls(r.pct_chg)}>{fmtPct(r.pct_chg)}</td>
                    <td>{fmtQty(r.volume, u)}</td>
                    <td>{fmtMoney(r.amount, cur)}</td>
                    <td className="up">{fmtQty(r.outer_volume, u)}</td>
                    <td className="down">{fmtQty(r.inner_volume, u)}</td>
                    <td>{r.outer_pct == null ? '—' : `${r.outer_pct.toFixed(2)}%`}</td>
                    <td>{r.bids}/{r.asks}{r.stale ? ' 陈旧' : ''}</td>
                    <td>{fmtTsMs(r.fetched_at)}</td>
                    <td>
                      <button className="mini" disabled={busy} onClick={(e) => { e.stopPropagation(); void removePool(r.symbol) }}>
                        移除
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        <p className="sub-note">
          口径：外盘/内盘=主动买入/卖出累计量（A股两源实测逐位一致：腾讯 outer 与东财 f49；
          港股腾讯不给，取东财 f49 单源）；A股量单位=手（1 手=100 股），
          港股量单位=股；金额单位 A股=元、港股=港元。取不到该字段的标的显示 —，不是 0。
        </p>
      </div>

      {/* ---------- 详情 ---------- */}
      {!symbol && <div className="card"><p className="placeholder">请选择标的（点击上表任意一行）。</p></div>}
      {symbol && (
        <>
          <div className="card">
            <div className="market-head">
              <b className="mono">{symbol}</b>
              <span>{(snap.name as string) ?? (extra.name as string) ?? '—'}</span>
              <span className="tag">板块 {detail?.board ?? '—'}</span>
              {detail?.session && (
                <span className={detail.session?.state === 'open' ? 'badge badge-paper' : 'badge badge-live'}>
                  {STATE_CN[detail.session?.state ?? ''] ?? detail.session?.state ?? '—'}｜{detail.session?.note ?? ''}
                </span>
              )}
              {snap.stale ? <span className="badge badge-down">陈旧快照（本轮未取到实时数据）</span> : <span className="badge">实时快照</span>}
              <span className="sub-note">
                详情刷新 {fmtTsMs(snap.fetched_at as number | undefined)} · 逐笔更新 {fmtTsMs(detail?.flow?.updated_at)}
                {detail?.quote_extra_error ? ` · 个股字段取数失败：${detail.quote_extra_error}` : ''}
                {detail?.intraday_error ? ` · 分时取数失败：${detail.intraday_error}` : ''}
              </span>
            </div>
            {!detail && <p className="placeholder">详情加载中（该接口会实打实调用外部行情，最长十几秒）…</p>}
            <div className="metric-cards">
              {metrics.map((m) => (
                <div key={m.label} className="card metric-card">
                  <span className="metric-label">{m.label}</span>
                  <span className={`metric-value ${m.cls ?? ''}`}>{m.value}</span>
                </div>
              ))}
            </div>
            <p className="sub-note">
              「—」表示该字段本次没返回（不同市场/通道字段集不一样），不是 0。
              字段来源：腾讯批量快照 + 东财个股快照（{detail ? Object.entries(detail.feeds ?? {}).map(([n, f]) => `${n}=${f?.ready ? '就绪' : '未就绪'}`).join('，') : '—'}）。
            </p>
          </div>

          <div className="backtest-layout">
            <div className="card">
              <h3>五档盘口</h3>
              <OrderBook detail={detail} />
            </div>
            <FlowCard detail={detail} />
          </div>

          <div className="card">
            <div className="row-btns">
              <h3>分时（价格 / 均价 + 分钟主动买卖量）</h3>
              <span className="tag">量单位：{flowUnit || '—'}</span>
              <span className="sub-note">买卖量柱来自逐笔分钟聚合（含盘后），折线来自东财分时</span>
            </div>
            {intraday.length === 0 && minutes.length === 0 ? (
              <p className="placeholder">
                暂无分时数据{detail?.intraday_error ? `（后端原文：${detail.intraday_error}）` : '（休市、新上市或东财分时通道未就绪）'}。
              </p>
            ) : (
              <IntradayChart points={intraday} minutes={minutes} unit={flowUnit || '量'} />
            )}
          </div>

          <div className="card">
            <div className="row-btns">
              <h3>K 线</h3>
              <span className="tabs">
                {[['1d', '日K'], ['1w', '周K'], ['1M', '月K']].map(([f, label]) => (
                  <button key={f} className={klineFreq === f ? 'active' : ''} onClick={() => setKlineFreq(f)}>{label}</button>
                ))}
              </span>
              <span className="tabs">
                {[120, 250, 500].map((d) => (
                  <button key={d} className={klineDays === d ? 'active' : ''} onClick={() => setKlineDays(d)}>{d} 天</button>
                ))}
              </span>
              <button className="mini" onClick={() => setKlineReload((v) => v + 1)}>重新加载</button>
            </div>
            {klineRows.length === 0 ? (
              <p className="placeholder">
                暂无 K 线{errors.kline ? `（后端原文：${errors.kline}）` : ''}。
                A股优先读本地 Parquet，缺数据回落腾讯源；60 秒自动重试。
              </p>
            ) : (
              <>
                <KLineChart rows={klineRows} />
                <p className="sub-note">
                  来源 {klineMeta?.source || '—'} · 复权 {ADJUST_CN[klineMeta?.adjust ?? ''] ?? klineMeta?.adjust ?? '—'} ·
                  量单位 {klineMeta?.unit || '—'} · 共 {klineRows.length} 根（{klineRows[0]?.dt ?? '—'} ~ {klineRows[klineRows.length - 1]?.dt ?? '—'}）
                  {klineMeta?.note ? ` · ${klineMeta.note}` : ''}
                </p>
              </>
            )}
          </div>

          <div className="card">
            <div className="row-btns">
              <h3>逐笔明细（最新在前）</h3>
              <span className="tabs">
                {[['', '全部'], ['buy', '主动买'], ['sell', '主动卖'], ['neutral', '中性']].map(([v, label]) => (
                  <button key={v || 'all'} className={tickSide === v ? 'active' : ''}
                    onClick={() => { setTickSide(v); setTickOffset(0) }}>{label}</button>
                ))}
              </span>
              <span className="sub-note">
                量单位 {flowUnit || '—'} · 额单位 {flowCur} · 共 {tickPage?.total ?? 0} 笔
                {tickPage ? ` · 第 ${Math.floor(tickOffset / PAGE) + 1}/${Math.max(1, Math.ceil((tickPage.total ?? 0) / PAGE))} 页` : ''}
              </span>
              <button className="mini" disabled={tickOffset <= 0}
                onClick={() => setTickOffset((o) => Math.max(0, o - PAGE))}>上一页</button>
              <button className="mini" disabled={!tickPage || tickOffset + PAGE >= (tickPage.total ?? 0)}
                onClick={() => setTickOffset((o) => o + PAGE)}>下一页</button>
            </div>
            {tickPage?.side_map && Object.keys(tickPage.side_map).length > 0 && (
              <p className="sub-note">
                方向码：{Object.entries(tickPage.side_map).map(([c, s]) => `${c}=${SIDE_CN[s] ?? s}`).join('，')}
                {tickPage.calibrated === false && ' · 未与官方内外盘对齐，方向列仅供参考（unknown 表示该码语义未定）'}
              </p>
            )}
            {!tickPage ? (
              <p className="placeholder">{errors.ticks ? `逐笔明细未取到（${errors.ticks}）` : '逐笔明细加载中…'}</p>
            ) : tickPage.rows.length === 0 ? (
              <p className="placeholder">
                本筛选条件下无逐笔记录{tickPage.error ? `（后端原文：${tickPage.error}）` : '（休市/新上市/通道未就绪）'}。
              </p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>时间</th><th>价格</th><th>成交量({flowUnit})</th><th>笔数</th>
                    <th>成交额({flowCur})</th><th>方向</th><th>原始码</th>
                  </tr>
                </thead>
                <tbody>
                  {tickPage.rows.map((t, i) => (
                    <tr key={`${t.time}-${i}`}>
                      <td className="mono">{t.time}</td>
                      <td className={pctCls(t.price - (last ?? t.price))}>{fmtNum(t.price, 3)}</td>
                      <td>{fmtQty(t.volume, flowUnit)}</td>
                      <td>{t.deals}</td>
                      <td>{fmtMoney(t.amount, flowCur)}</td>
                      <td className={t.direction === 'buy' ? 'up' : t.direction === 'sell' ? 'down' : undefined}>
                        {SIDE_CN[t.direction] ?? t.direction}
                      </td>
                      <td className="mono">{t.side_code}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="card">
            <h3>当日定格与历史（收盘后落库，复盘用）</h3>
            {hist.length === 0 ? (
              <p className="placeholder">
                {errors.history ? `历史读取失败（${errors.history}）` : '还没有本标的的定格记录（每日收盘后由后端落库），已有则自动出现在此。'}
              </p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>交易日</th><th>收盘</th><th>涨跌</th><th>外盘(买)</th><th>内盘(卖)</th>
                    <th>大单买</th><th>大单卖</th><th>成交额</th><th>口径</th><th>方向码</th>
                  </tr>
                </thead>
                <tbody>
                  {hist.map((h) => {
                    const u = h.market === 'HK' ? '股' : '手'
                    const cur = h.market === 'HK' ? '港元' : '元'
                    return (
                      <tr key={`${h.trade_date}-${h.symbol}`} style={h.staled ? { opacity: 0.6 } : undefined}>
                        <td className="mono">{fmtDate8(h.trade_date)}</td>
                        <td>{fmtNum(h.close, 3)}</td>
                        <td className={pctCls(h.pct_chg)}>{fmtPct(h.pct_chg)}</td>
                        <td className="up">{fmtQty(h.outer_volume, u)}</td>
                        <td className="down">{fmtQty(h.inner_volume, u)}</td>
                        <td className="up">{fmtQty(h.big_buy_volume, u)}</td>
                        <td className="down">{fmtQty(h.big_sell_volume, u)}</td>
                        <td>{fmtMoney(h.amount, cur)}</td>
                        <td className="sub-note" style={{ margin: 0 }}>{h.flow_source || '—'}{h.staled ? ' · 陈旧' : ''}</td>
                        <td className="mono">{h.side_map || '—'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
            <p className="sub-note">
              大单买/卖=逐笔分层中「大单+特大单」的量（含盘后，与官方内外盘可有口径差）；
              「外盘/内盘」两列是当日官方口径定格。列单位随市场：A股=手、港股=股。
            </p>
          </div>
        </>
      )}
    </section>
  )
}

/** 详情右栏：当日买卖量（含口径、校准与差异说明）。 */
function FlowCard({ detail }: { detail: WatchDetail | null }) {
  return (
    <div className="card">
      <FlowPanel flow={detail?.flow ?? null} />
    </div>
  )
}
