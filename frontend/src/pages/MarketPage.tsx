import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  addWatch,
  addWatchPool,
  apiError,
  FIN_TYPE_LABELS,
  getFinancial,
  getFreqs,
  getHistory,
  getSnapshot,
  getWatchlist,
  removeWatch,
  type BarRow,
  type FinReportType,
  type FinancialDoc,
  type TickSnapshot,
  type WatchItem,
} from '../api/client'
import HelpLink from '../components/HelpLink'
import TradePanel from '../components/TradePanel'

/** 行情来源在界面上只出现产品名，不出现内部代号。 */
const FIN_SRC_CN: Record<string, string> = {
  akshare: '免费公开数据',
  tushare: 'Tushare 专业行情',
  ths_ifind: '同花顺 iFinD',
  qmt_xtdata: 'miniQMT 行情',
  tdx_aidata: '通达信行情',
  federated: '自动选择的来源',
}

const FREQ_LABELS: Record<string, string> = {
  '1d': '日',
  '1w': '周',
  '1M': '月',
  '60m': '60分',
  '30m': '30分',
  '15m': '15分',
  '5m': '5分',
  '1m': '1分',
}

/** K 线 + 成交量副图（lightweight-charts，十字光标内置）。 */
function KLine({ bars }: { bars: BarRow[] }) {
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
      timeScale: { timeVisible: true, borderColor: '#e5e7eb' },
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
    }
  }, [])

  useEffect(() => {
    const c = candleRef.current
    const v = volRef.current
    if (!c || !v) return
    const data = bars.map((b) => ({
      time: (new Date(b.dt.slice(0, 19)).getTime() / 1000) as UTCTimestamp,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }))
    const vols = bars.map((b) => ({
      time: (new Date(b.dt.slice(0, 19)).getTime() / 1000) as UTCTimestamp,
      value: b.volume,
      color: b.close >= b.open ? '#fca5a5' : '#86efac',
    }))
    c.setData(data)
    v.setData(vols)
    chartRef.current?.timeScale().fitContent()
  }, [bars])

  return <div ref={ref} style={{ width: '100%' }} />
}

/** 分时图（area series；分钟线本地存在时可用）。 */
function IntradayChart({ bars }: { bars: BarRow[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const lineRef = useRef<ISeriesApi<'Line'> | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = createChart(ref.current, {
      height: 260,
      layout: { background: { color: '#ffffff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f1f3f6' }, horzLines: { color: '#f1f3f6' } },
      timeScale: { timeVisible: true, borderColor: '#e5e7eb' },
    })
    chartRef.current = chart
    lineRef.current = chart.addSeries(LineSeries, { color: '#2563eb', lineWidth: 1 })
    return () => {
      chart.remove()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!lineRef.current) return
    lineRef.current.setData(
      bars.map((b) => ({
        time: (new Date(b.dt.slice(0, 19)).getTime() / 1000) as UTCTimestamp,
        value: b.close,
      })),
    )
    chartRef.current?.timeScale().fitContent()
  }, [bars])

  return <div ref={ref} style={{ width: '100%' }} />
}

/** 五档盘口 + 买卖压力条。note 是后端给的空态原因（没原因就别猜，宁可只说"等待"）。 */
function OrderBook({ tick, note }: { tick: TickSnapshot | null; note?: string }) {
  if (!tick || (tick.bids.length === 0 && tick.asks.length === 0)) {
    return (
      <p className="placeholder">
        {note
          ? `五档盘口：${note}`
          : '五档盘口：等待实时行情（实时泵只推送已订阅的标的）。'}
      </p>
    )
  }
  const maxVol = Math.max(
    1,
    ...tick.bids.map((b) => b[1]),
    ...tick.asks.map((a) => a[1]),
  )
  const bar = (vol: number, side: 'bid' | 'ask') => (
    <div className="ob-bar">
      <div
        className={side === 'bid' ? 'ob-fill bid' : 'ob-fill ask'}
        style={{ width: `${(vol / maxVol) * 100}%` }}
      />
    </div>
  )
  const rows = []
  for (let i = 4; i >= 0; i--) {
    const a = tick.asks[i]
    rows.push(
      <tr key={`a${i}`} className="ask-row">
        <td>卖{i + 1}</td>
        <td className="down">{a ? a[0].toFixed(2) : '—'}</td>
        <td>{a ? a[1] : '—'}</td>
        <td>{bar(a?.[1] ?? 0, 'ask')}</td>
      </tr>,
    )
  }
  for (let i = 0; i < 5; i++) {
    const b = tick.bids[i]
    rows.push(
      <tr key={`b${i}`}>
        <td>买{i + 1}</td>
        <td className="up">{b ? b[0].toFixed(2) : '—'}</td>
        <td>{b ? b[1] : '—'}</td>
        <td>{bar(b?.[1] ?? 0, 'bid')}</td>
      </tr>,
    )
  }
  return (
    <table className="ob-table">
      <thead>
        <tr>
          <th>档位</th>
          <th>价格</th>
          <th>量</th>
          <th>压力</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  )
}

/** 报告期代码转人话：20260930 → 2026三季；认不出来的原样显示（不猜）。 */
function periodLabel(p: string): string {
  const suffix: Record<string, string> = { '0331': '一季', '0630': '中期', '0930': '三季', '1231': '年报' }
  return /^\d{8}$/.test(p) ? `${p.slice(0, 4)}${suffix[p.slice(4)] ?? p.slice(4)}` : p
}

/** 金额缩写按数量级判，不靠字段名猜；原值放 title，缩写不等于丢精度。 */
function fmtValue(v: number | null): { text: string; raw: string } {
  if (v === null || v === undefined || Number.isNaN(v)) return { text: '—', raw: '未披露（不是 0）' }
  const raw = v.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
  const a = Math.abs(v)
  if (a >= 1e8) return { text: `${(v / 1e8).toFixed(2)} 亿`, raw }
  if (a >= 1e4) return { text: `${(v / 1e4).toFixed(2)} 万`, raw }
  return { text: raw, raw }
}

/** 财务三表：后端已按分组转好，这里只做展示；比率类原值、金额类缩写、缺期显示「—」。 */
function FinTable({ doc }: { doc: FinancialDoc }) {
  return (
    <div className="fin-wrap">
      <p className="sub-note">
        {doc.symbol} · {FIN_TYPE_LABELS[doc.report_type as FinReportType] ?? doc.report_type} · 共 {doc.rows} 项指标 · 来源 {FIN_SRC_CN[doc.source] ?? doc.source}
        {doc.cached ? '（本机已保存）' : ''}｜{doc.note}
      </p>
      <table className="fin-table">
        <thead>
          <tr>
            <th>指标</th>
            {doc.periods.map((p) => (
              <th key={p} title={p}>{periodLabel(p)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {doc.groups.map((g) => (
            <Fragment key={g.name}>
              <tr className="fin-group">
                <td colSpan={doc.periods.length + 1}>{g.name}</td>
              </tr>
              {g.items.map((it) => (
                <tr key={`${g.name}-${it.indicator}`}>
                  <td>{it.indicator}</td>
                  {doc.periods.map((p) => {
                    const cell = fmtValue(it.values[p] ?? null)
                    return <td key={p} title={cell.raw}>{cell.text}</td>
                  })}
                </tr>
              ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function MarketPage() {
  const [watch, setWatch] = useState<WatchItem[]>([])
  const [symbol, setSymbol] = useState('600000.SH')
  const [freq, setFreq] = useState('1d')
  const [freqs, setFreqs] = useState<string[]>([])
  const [bars, setBars] = useState<BarRow[]>([])
  const [intraday, setIntraday] = useState<BarRow[]>([])
  const [tab, setTab] = useState<'kline' | 'intraday' | 'financial'>('kline')
  // 财务三表：只在点开页签时取，按 标的:表种 缓存；首次是真实外呼（几秒），不给加载态就会像卡死
  const [finType, setFinType] = useState<FinReportType>('indicator')
  const [finDocs, setFinDocs] = useState<Record<string, FinancialDoc>>({})
  const [finLoading, setFinLoading] = useState(false)
  const [finErr, setFinErr] = useState('')
  const [finNonce, setFinNonce] = useState(0)
  const [poolMsg, setPoolMsg] = useState('')
  const finInflight = useRef<Set<string>>(new Set())
  const [tick, setTick] = useState<TickSnapshot | null>(null)
  const [snapNote, setSnapNote] = useState('')
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [watchErr, setWatchErr] = useState('')

  const loadWatchlist = useCallback(() => {
    getWatchlist()
      .then((w) => {
        setWatch(w)
        setWatchErr('')
      })
      .catch((e: unknown) => setWatchErr(apiError(e))) // 连不上 ≠ 自选股为空
  }, [])

  const loadBars = useMemo(
    () => async (sym: string, f: string) => {
      setLoading(true)
      setErr('')
      try {
        const data = await getHistory(sym, f)
        setBars(data)
        if (f === '1d' && data.length > 0) {
          // 分时图数据：本地有 1m 则取最近一日，否则为空
          try {
            const m = await getHistory(sym, '1m')
            setIntraday(m.slice(-240))
          } catch {
            setIntraday([])
          }
        }
      } catch (e) {
        setErr(apiError(e))
        setBars([])
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    loadWatchlist()
  }, [loadWatchlist])

  useEffect(() => {
    if (!symbol) return
    loadBars(symbol, freq)
    getFreqs(symbol).then(setFreqs).catch(() => setFreqs([]))
  }, [symbol, freq, loadBars])

  // 快照轮询（1s；实时源接入后可切 WS）。后端无行情时带 reason，界面照原话说。
  useEffect(() => {
    if (!symbol) return
    const load = () =>
      getSnapshot(symbol)
        .then((tk) => {
          const live = tk && tk.last != null ? tk : null
          setTick(live)
          setSnapNote(live ? '' : tk && tk.reason ? tk.reason : '')
        })
        .catch(() => {
          setTick(null)
          setSnapNote('连不上系统（最新行情读取失败）')
        })
    load()
    const t = setInterval(load, 1000)
    return () => clearInterval(t)
  }, [symbol])

  // 财务取数：只在页签打开且本地没有该 (标的,表种) 时发一次；晚到的响应按自己的 key 落位，
  // 不会把上一家公司的报表画到当前标的上
  const finKey = `${symbol}:${finType}`
  useEffect(() => {
    if (tab !== 'financial' || finDocs[finKey] || finInflight.current.has(finKey)) return
    finInflight.current.add(finKey)
    setFinLoading(true)
    setFinErr('')
    getFinancial(symbol, finType)
      .then((doc) => setFinDocs((cur) => ({ ...cur, [finKey]: doc })))
      .catch((e: unknown) => setFinErr(apiError(e)))
      .finally(() => {
        finInflight.current.delete(finKey)
        setFinLoading(false)
      })
  }, [tab, symbol, finType, finKey, finDocs, finNonce])

  const last = bars.length > 0 ? bars[bars.length - 1] : null

  return (
    <section>
      <h2>行情面板<HelpLink slug="market" /></h2>
      <div className="market-layout">
        <div className="card market-watch">
          <h3>自选股</h3>
          {watchErr && <p className="badge badge-down">自选股读取失败：{watchErr}</p>}
          {watchErr && (
            <p className="sub-note">
              连不上系统时不会把这里显示成「没有自选股」——
              <button className="mini" onClick={loadWatchlist}>重试</button>
            </p>
          )}
          <ul>
            {watch.map((w) => (
              <li key={w.symbol}>
                <button
                  className={w.symbol === symbol ? 'active' : ''}
                  onClick={() => setSymbol(w.symbol)}
                >
                  {w.symbol}
                </button>
                <button
                  className="del"
                  title="移除"
                  onClick={async () => {
                    try {
                      setWatch(await removeWatch(w.symbol))
                      setWatchErr('')
                    } catch (e) {
                      setWatchErr(apiError(e))
                    }
                  }}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
          <div className="watch-add">
            <input
              placeholder="如 000001.SZ"
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <button
              onClick={async () => {
                if (!input.trim()) return
                try {
                  setWatch(await addWatch(input.trim().toUpperCase()))
                  setInput('')
                  setWatchErr('')
                } catch (e) {
                  setWatchErr(apiError(e)) // 代码格式/重复/后端离线都在此回显，不静默
                }
              }}
            >
              添加
            </button>
          </div>
        </div>

        <div className="market-main">
          <div className="card">
            <div className="market-head">
              <b>{symbol}</b>
              {tick && tick.last != null && (
                <span className="badge">最新 {tick.last.toFixed(2)}</span>
              )}
              {last && <span className="badge">收盘 {last.close.toFixed(2)}</span>}
              <span className="freq-tabs">
                <button
                  title={`把 ${symbol} 加进盯盘池（已在不重复加）`}
                  onClick={async () => {
                    try {
                      const r = await addWatchPool([symbol])
                      setPoolMsg(r.added.length ? `${symbol} 已加入盯盘池` : `${symbol} 本来就在盯盘池里`)
                    } catch (e) {
                      setPoolMsg(`加入盯盘池失败：${apiError(e)}`)
                    }
                  }}
                >
                  加进盯盘池
                </button>
                <Link className="freq-tab-link" to={`/watch?symbol=${encodeURIComponent(symbol)}`}>
                  在盯盘池看这只
                </Link>
              </span>
              <span className="freq-tabs">
                {(['kline', 'intraday', 'financial'] as const).map((t) => (
                  <button
                    key={t}
                    className={tab === t ? 'active' : ''}
                    onClick={() => setTab(t)}
                  >
                    {t === 'kline' ? 'K线' : t === 'intraday' ? '分时' : '财务'}
                  </button>
                ))}
              </span>
              {tab === 'financial' && (
                <span className="freq-tabs">
                  {(Object.keys(FIN_TYPE_LABELS) as FinReportType[]).map((id) => (
                    <button
                      key={id}
                      className={finType === id ? 'active' : ''}
                      onClick={() => setFinType(id)}
                    >
                      {FIN_TYPE_LABELS[id]}
                    </button>
                  ))}
                </span>
              )}
              <span className="freq-tabs">
                {Object.entries(FREQ_LABELS).map(([f, label]) => (
                  <button
                    key={f}
                    className={freq === f ? 'active' : ''}
                    disabled={tab === 'kline' && freqs.length > 0 && !freqs.includes(f)}
                    onClick={() => {
                      setFreq(f)
                      setTab('kline')
                    }}
                  >
                    {label}
                  </button>
                ))}
              </span>
            </div>
            {poolMsg && <p className="sub-note">{poolMsg}</p>}
            {loading && tab !== 'financial' && <p className="placeholder">加载中…</p>}
            {!loading && tab === 'kline' && bars.length === 0 && !err && (
              <p className="placeholder">
                本地还没有 {symbol} 的 {FREQ_LABELS[freq] ?? freq}数据——这不是行情丢了，是这台机器上
                还没下载过。K 线使用本机已下载的数据，请去
                <Link to="/data" style={{ color: '#1d4ed8', margin: '0 4px' }}>数据管理</Link>
                页把这只标的下载下来（下载完回到这里就能看到）。
                <br />
                不清楚下载要填什么、选哪个周期和复权，看
                <Link to="/manual?s=market" style={{ color: '#1d4ed8', margin: '0 4px' }}>行情面板怎么用</Link>
                与
                <Link to="/manual?s=data" style={{ color: '#1d4ed8', margin: '0 4px' }}>数据管理怎么用</Link>
                。
              </p>
            )}
            {!loading && bars.length > 0 && tab === 'kline' && <KLine bars={bars} />}
            {!loading && tab === 'intraday' && (intraday.length > 0 ? (
              <IntradayChart bars={intraday} />
            ) : (
              <p className="placeholder">
                暂无分钟线数据——到
                <Link to="/data" style={{ color: '#1d4ed8', margin: '0 4px' }}>数据管理</Link>
                页下载 {symbol} 的 1m 周期后可用。
              </p>
            ))}
            {tab === 'financial' && (
              finDocs[finKey] ? (
                <FinTable doc={finDocs[finKey]} />
              ) : finLoading ? (
                <p className="placeholder">
                  正在取 {symbol} 的{FIN_TYPE_LABELS[finType]}…
                  首次获取约需 3~10 秒（免费数据来源）；同一标的同一报表 5 分钟内不重复获取，
                  来回切换不会再等。港股没有新浪三表（会如实提示），财务摘要同样不适用。
                </p>
              ) : finErr ? (
                <p className="badge badge-down">
                  {finErr}
                  <button
                    style={{ marginLeft: 8 }}
                    onClick={() => {
                      setFinErr('')
                      setFinNonce((n) => n + 1)
                    }}
                  >
                    重试
                  </button>
                </p>
              ) : (
                <p className="placeholder">未取数。</p>
              )
            )}
            {err && tab !== 'financial' && <p className="badge badge-down">{err}</p>}
          </div>
        </div>

        <div className="card market-book">
          <h3>五档盘口</h3>
          <OrderBook tick={tick} note={snapNote} />
        </div>

        <TradePanel symbol={symbol} />
      </div>
    </section>
  )
}
