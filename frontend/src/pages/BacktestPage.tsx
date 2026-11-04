import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useMemo, useRef, useState } from 'react'

// 按需注册（净值/现金折线即可）——全量引入使构建产物超 500KB
echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
import {
  getStrategies,
  runBacktest,
  type BacktestResult,
  type EquityPoint,
  type Metrics,
  type StrategyInfo,
} from '../api/client'

/** 参数表单值：全部以字符串编辑，提交时按默认值类型转换。 */
type ParamValues = Record<string, string>

const METRIC_CARDS: { key: keyof Metrics; label: string; fmt: (v: number | null) => string }[] = [
  { key: 'total_return', label: '总收益', fmt: pct },
  { key: 'annual_return', label: '年化收益', fmt: pct },
  { key: 'max_drawdown', label: '最大回撤', fmt: pct },
  { key: 'sharpe', label: 'Sharpe', fmt: (v) => (v == null ? '—' : v.toFixed(2)) },
  { key: 'win_rate', label: '胜率', fmt: pct },
  {
    key: 'profit_loss_ratio',
    label: '盈亏比',
    fmt: (v) => (v == null ? '—' : v.toFixed(2)),
  },
  { key: 'turnover', label: '年化换手', fmt: (v) => (v == null ? '—' : `${v.toFixed(2)}x`) },
  { key: 'trade_count', label: '成交笔数', fmt: (v) => (v == null ? '—' : String(v)) },
]

function pct(v: number | null): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function EquityChart({ data }: { data: EquityPoint[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<ReturnType<typeof echarts.init> | null>(null)

  useEffect(() => {
    if (!ref.current) return
    instRef.current = echarts.init(ref.current)
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = instRef.current
    if (!chart) return
    const dts = data.map((p) => p.dt.slice(0, 10))
    chart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: ['净值', '现金'], top: 0 },
      grid: { left: 70, right: 70, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: dts },
      yAxis: [
        { type: 'value', scale: true, name: '净值' },
        { type: 'value', scale: true, name: '现金' },
      ],
      series: [
        {
          name: '净值',
          type: 'line',
          showSymbol: false,
          data: data.map((p) => p.equity),
          lineStyle: { width: 1.6 },
        },
        {
          name: '现金',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: false,
          data: data.map((p) => p.cash),
          lineStyle: { width: 1, type: 'dashed', opacity: 0.6 },
        },
      ],
    } as echarts.EChartsCoreOption)
  }, [data])

  return <div ref={ref} style={{ width: '100%', height: 320 }} />
}

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([])
  const [selected, setSelected] = useState<string>('')
  const [symbol, setSymbol] = useState('600000.SH')
  const [start, setStart] = useState('2020-01-01')
  const [end, setEnd] = useState('2026-12-31')
  const [initialCash, setInitialCash] = useState('1000000')
  const [slippage, setSlippage] = useState('0')
  const [params, setParams] = useState<ParamValues>({})
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<BacktestResult | null>(null)

  useEffect(() => {
    getStrategies()
      .then((ss) => {
        setStrategies(ss)
        if (ss.length > 0) setSelected((cur) => cur || ss[0].name)
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const current = useMemo(() => strategies.find((s) => s.name === selected), [strategies, selected])

  useEffect(() => {
    // 切换策略 → 参数表单回填默认值
    if (!current) return
    const next: ParamValues = {}
    for (const [k, v] of Object.entries(current.params)) next[k] = String(v)
    setParams(next)
  }, [current])

  const submit = async () => {
    if (!selected || running) return
    setRunning(true)
    setError('')
    try {
      const typed: Record<string, string | number> = {}
      for (const [k, raw] of Object.entries(params)) {
        const def = current?.params[k]
        const num = Number(raw)
        typed[k] = typeof def === 'number' && Number.isFinite(num) ? num : raw
      }
      const out = await runBacktest({
        strategy: selected,
        symbols: symbol.split(/[,，\s]+/).filter(Boolean),
        params: typed,
        start: start || undefined,
        end: end || undefined,
        initial_cash: Number(initialCash) || 1000000,
        slippage: Number(slippage) || 0,
      })
      setResult(out)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
    } finally {
      setRunning(false)
    }
  }

  return (
    <section>
      <h2>回测报告</h2>
      <div className="backtest-layout">
        <div className="card backtest-form">
          <h3>运行回测</h3>
          <label>
            策略
            <select value={selected} onChange={(e) => setSelected(e.target.value)}>
              {strategies.length === 0 && <option value="">（加载中…）</option>}
              {strategies.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            标的（逗号分隔）
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)} />
          </label>
          <div className="row2">
            <label>
              开始
              <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label>
              结束
              <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
          </div>
          <div className="row2">
            <label>
              初始资金
              <input value={initialCash} onChange={(e) => setInitialCash(e.target.value)} />
            </label>
            <label>
              滑点
              <input value={slippage} onChange={(e) => setSlippage(e.target.value)} />
            </label>
          </div>
          {current && Object.keys(current.params).length > 0 && (
            <>
              <h4>策略参数</h4>
              {Object.entries(current.params).map(([k, def]) => (
                <label key={k}>
                  {k}
                  <input
                    value={params[k] ?? String(def)}
                    onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))}
                  />
                </label>
              ))}
            </>
          )}
          <button disabled={running || !selected} onClick={submit}>
            {running ? '回测运行中…' : '运行回测'}
          </button>
          {error && <p className="badge badge-down">{error}</p>}
        </div>

        <div className="backtest-output">
          {result ? (
            <>
              <div className="metric-cards">
                {METRIC_CARDS.map((m) => (
                  <div className="card metric-card" key={m.key}>
                    <span className="metric-label">{m.label}</span>
                    <span className="metric-value">{m.fmt(result.metrics[m.key] as number | null)}</span>
                  </div>
                ))}
              </div>
              <div className="card">
                <h3>
                  净值曲线（run #{result.run_id} · {result.bars} 根 bar）
                </h3>
                <EquityChart data={result.equity} />
              </div>
              <div className="card">
                <h3>成交明细（{result.trades.length} 笔）</h3>
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>代码</th>
                      <th>方向</th>
                      <th>价格</th>
                      <th>数量</th>
                      <th>金额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((t) => (
                      <tr key={t.trade_id}>
                        <td>{new Date(t.traded_at).toLocaleString('zh-CN')}</td>
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
                {result.logs.length > 0 && (
                  <details>
                    <summary>过程日志（{result.logs.length} 条）</summary>
                    <pre className="logs">{result.logs.join('\n')}</pre>
                  </details>
                )}
              </div>
            </>
          ) : (
            <div className="card placeholder">
              配置参数并点击「运行回测」；结果将展示净值曲线、指标卡片与成交明细。
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
