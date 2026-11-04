import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  addWatch,
  getFreqs,
  getHistory,
  getSnapshot,
  getWatchlist,
  removeWatch,
  type BarRow,
  type TickSnapshot,
  type WatchItem,
} from '../api/client'

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

/** 五档盘口 + 买卖压力条。 */
function OrderBook({ tick }: { tick: TickSnapshot | null }) {
  if (!tick || (tick.bids.length === 0 && tick.asks.length === 0)) {
    return <p className="placeholder">五档盘口：等待实时行情（D14 接入实时源后显示）。</p>
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

export default function MarketPage() {
  const [watch, setWatch] = useState<WatchItem[]>([])
  const [symbol, setSymbol] = useState('600000.SH')
  const [freq, setFreq] = useState('1d')
  const [freqs, setFreqs] = useState<string[]>([])
  const [bars, setBars] = useState<BarRow[]>([])
  const [intraday, setIntraday] = useState<BarRow[]>([])
  const [tab, setTab] = useState<'kline' | 'intraday'>('kline')
  const [tick, setTick] = useState<TickSnapshot | null>(null)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

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
        setErr(e instanceof Error ? e.message : String(e))
        setBars([])
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    getWatchlist().then(setWatch).catch(() => setWatch([]))
  }, [])

  useEffect(() => {
    if (!symbol) return
    loadBars(symbol, freq)
    getFreqs(symbol).then(setFreqs).catch(() => setFreqs([]))
  }, [symbol, freq, loadBars])

  // 快照轮询（1s；实时源接入后可切 WS）。后端无行情返回 null。
  useEffect(() => {
    if (!symbol) return
    const load = () =>
      getSnapshot(symbol)
        .then((tk) => setTick(tk && tk.last != null ? tk : null))
        .catch(() => setTick(null))
    load()
    const t = setInterval(load, 1000)
    return () => clearInterval(t)
  }, [symbol])

  const last = bars.length > 0 ? bars[bars.length - 1] : null

  return (
    <section>
      <h2>行情面板</h2>
      <div className="market-layout">
        <div className="card market-watch">
          <h3>自选股</h3>
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
                  onClick={async () => setWatch(await removeWatch(w.symbol))}
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
                setWatch(await addWatch(input.trim().toUpperCase()))
                setInput('')
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
                {(['kline', 'intraday'] as const).map((t) => (
                  <button
                    key={t}
                    className={tab === t ? 'active' : ''}
                    onClick={() => setTab(t)}
                  >
                    {t === 'kline' ? 'K线' : '分时'}
                  </button>
                ))}
              </span>
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
            {loading && <p className="placeholder">加载中…</p>}
            {!loading && bars.length === 0 && (
              <p className="placeholder">
                本地无 {symbol} {freq} 数据——请在「数据管理」页下载（K 线全部走 Parquet 本地缓存）。
              </p>
            )}
            {!loading && bars.length > 0 && tab === 'kline' && <KLine bars={bars} />}
            {!loading && tab === 'intraday' && (intraday.length > 0 ? (
              <IntradayChart bars={intraday} />
            ) : (
              <p className="placeholder">暂无分钟线数据——数据管理页下载 1m 后可用。</p>
            ))}
            {err && <p className="badge badge-down">{err}</p>}
          </div>
        </div>

        <div className="card market-book">
          <h3>五档盘口</h3>
          <OrderBook tick={tick} />
        </div>
      </div>
    </section>
  )
}
