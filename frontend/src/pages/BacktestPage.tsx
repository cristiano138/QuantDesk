import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

// 按需注册（净值/现金折线即可）——全量引入使构建产物超 500KB
echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
import {
  apiError,
  getBacktestRun,
  getBacktestProgress,
  getStrategies,
  listBacktestRuns,
  openEvents,
  submitBacktest,
  type BacktestProgress,
  type BacktestRunDetail,
  type BacktestRunRow,
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

const RUN_STATUS_CN: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  completed: '完成',
  failed: '失败',
}

function pct(v: number | null): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function fmtTs(ms: number | null): string {
  if (!ms) return '—'
  return new Date(ms).toLocaleString('zh-CN')
}

/** 进度归一：WS 事件与 /progress 轮询共用同一套百分比口径。 */
function percentOf(p: BacktestProgress): number {
  if (typeof p.percent === 'number') return p.percent
  if (p.total && p.done != null) return Math.round((100 * p.done) / p.total * 10) / 10
  return p.status === 'completed' ? 100 : 0
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

function RunHistory({ onPick, activeId }: { onPick: (id: number) => void; activeId: number | null }) {
  const [runs, setRuns] = useState<BacktestRunRow[] | null>(null)
  const [err, setErr] = useState('')

  const refresh = useCallback(() => {
    listBacktestRuns()
      .then(setRuns)
      .catch((e: unknown) => setErr(apiError(e)))
  }, [])

  useEffect(() => {
    refresh()
    // 异步回测完成后清单状态会变，收到 done 事件时刷一次
    return openEvents((data) => {
      const d = data as { type?: string; data?: { action?: string } }
      if (d?.type === 'log' && (d.data?.action === 'backtest_done' || d.data?.action === 'backtest_progress')) {
        refresh()
      }
    })
  }, [refresh])

  return (
    <div className="card">
      <div className="row-btns">
        <h3>运行历史</h3>
        <button className="mini" onClick={refresh}>刷新</button>
      </div>
      {err && <p className="badge badge-down">{err}</p>}
      {runs && runs.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>策略</th>
              <th>标的</th>
              <th>周期</th>
              <th>区间</th>
              <th>状态</th>
              <th>总收益</th>
              <th>创建时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id} className={r.run_id === activeId ? 'row-active' : undefined}>
                <td>{r.run_id}</td>
                <td>{r.strategy}</td>
                <td className="mono">{r.symbol}</td>
                <td>{r.freq}</td>
                <td>{r.start}~{r.end}</td>
                <td>
                  <span className={`badge ${r.status === 'completed' ? 'badge-paper' : r.status === 'failed' ? 'badge-down' : ''}`}>
                    {RUN_STATUS_CN[r.status] ?? r.status}
                  </span>
                </td>
                <td>{typeof r.metrics.total_return === 'number' ? pct(r.metrics.total_return) : '—'}</td>
                <td>{fmtTs(r.created_at)}</td>
                <td>
                  <button className="mini" onClick={() => onPick(r.run_id)}>回看</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="placeholder">暂无运行记录。</p>
      )}
    </div>
  )
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
  const [error, setError] = useState('')
  const [detail, setDetail] = useState<BacktestRunDetail | null>(null)

  // 异步运行态：WS 事件与轮询谁先到用谁的进度
  const [activeId, setActiveId] = useState<number | null>(null)
  const [prog, setProg] = useState<BacktestProgress | null>(null)
  const activeRef = useRef<number | null>(null)
  activeRef.current = activeId

  const finished = prog?.status === 'completed' || prog?.status === 'failed'

  useEffect(() => {
    getStrategies()
      .then((ss) => {
        setStrategies(ss)
        setSelected((cur) => cur || ss[0].name)
      })
      .catch((e: unknown) => setError(apiError(e)))
  }, [])

  const current = useMemo(() => strategies.find((s) => s.name === selected), [strategies, selected])

  useEffect(() => {
    // 切换策略 → 参数表单回填默认值
    if (!current) return
    const next: ParamValues = {}
    for (const [k, v] of Object.entries(current.params)) next[k] = String(v)
    setParams(next)
  }, [current])

  const loadResult = useCallback((runId: number) => {
    getBacktestRun(runId)
      .then(setDetail)
      .catch((e: unknown) => setError(apiError(e)))
  }, [])

  // 完成后拉结果；进度事件与轮询都只更新进度，避免重复请求结果
  useEffect(() => {
    if (activeId != null && prog?.status === 'completed') loadResult(activeId)
  }, [activeId, prog?.status, loadResult])

  // 进度轮询兜底（WS 断线时仍能推进），与 WS 事件互不冲突
  useEffect(() => {
    if (activeId == null || finished) return
    let alive = true
    const poll = () =>
      getBacktestProgress(activeId)
        .then((p) => {
          if (alive) setProg(p)
        })
        .catch(() => undefined)
    poll()
    const t = setInterval(poll, 1500)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [activeId, finished])

  useEffect(
    () =>
      openEvents((data) => {
        const d = data as {
          type?: string
          data?: { action?: string; run_id?: number; done?: number; total?: number; message?: string }
        }
        const dd = d.data
        if (d?.type !== 'log' || !dd?.action) return
        const runId = dd.run_id
        if (runId == null || runId !== activeRef.current) return
        if (dd.action === 'backtest_progress') {
          const total = dd.total ?? 0
          const done = dd.done ?? 0
          setProg((cur) => ({
            ...(cur ?? {}),
            run_id: runId,
            status: 'running',
            done,
            total,
            percent: total ? Math.round((100 * done) / total * 10) / 10 : 0,
          }))
        } else if (dd.action === 'backtest_done') {
          setProg({ run_id: runId, status: 'completed', percent: 100, message: dd.message })
        }
      }),
    [],
  )

  const submit = async () => {
    if (!selected || (activeId != null && !finished)) return
    setError('')
    setDetail(null)
    setProg({ status: 'queued', percent: 0 })
    try {
      const typed: Record<string, string | number> = {}
      for (const [k, raw] of Object.entries(params)) {
        const def = current?.params[k]
        const num = Number(raw)
        typed[k] = typeof def === 'number' && Number.isFinite(num) ? num : raw
      }
      const ack = await submitBacktest({
        strategy: selected,
        symbols: symbol.split(/[,，\s]+/).filter(Boolean),
        params: typed,
        start: start || undefined,
        end: end || undefined,
        initial_cash: Number(initialCash) || 1000000,
        slippage: Number(slippage) || 0,
      })
      setActiveId(ack.run_id)
    } catch (e) {
      setProg(null)
      setError(apiError(e)) // 入队前校验失败（如本地无数据）由服务端给原因
    }
  }

  const percent = prog ? percentOf(prog) : 0
  const runningNow = activeId != null && !finished

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
          <button disabled={runningNow || !selected} onClick={submit}>
            {runningNow ? '回测排队/运行中…' : '运行回测'}
          </button>
          <p className="sub-note">
            默认异步执行：提交后立即返回 run_id，进度来自事件流与轮询（二者取其先到的）。
            可在「设置」中通过 backtest_async 改回同步。
          </p>
          {error && <p className="badge badge-down">{error}</p>}
        </div>

        <div className="backtest-output">
          {activeId != null && prog && (
            <div className="card">
              <div className="row-btns">
                <h3>运行进度 #{activeId}</h3>
                <span className="badge">{RUN_STATUS_CN[prog.status] ?? prog.status}</span>
              </div>
              <div className="progress-track">
                <div className="progress-fill" style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
              </div>
              <p className="sub-note">
                {prog.done != null && prog.total ? `已处理 ${prog.done}/${prog.total} 根 bar · ` : ''}
                {percent.toFixed(1)}%
                {prog.message ? ` · ${prog.message}` : ''}
              </p>
              {prog.status === 'failed' && (
                <p className="badge badge-down">
                  状态 failed：{prog.error || '后端未返回错误详情，请查看运行记录备注'}
                </p>
              )}
            </div>
          )}

          {detail ? (
            <>
              <div className="metric-cards">
                {METRIC_CARDS.map((m) => (
                  <div className="card metric-card" key={m.key}>
                    <span className="metric-label">{m.label}</span>
                    <span className="metric-value">
                      {m.fmt((detail.metrics[m.key] as number | null | undefined) ?? null)}
                    </span>
                  </div>
                ))}
              </div>
              <div className="card">
                <h3>
                  净值曲线（run #{detail.run_id} · {detail.strategy} · {detail.symbol}）
                </h3>
                {detail.equity.length > 0 ? (
                  <EquityChart data={detail.equity} />
                ) : (
                  <p className="placeholder">无净值数据。</p>
                )}
                {detail.remark && <p className="sub-note warn">备注：{detail.remark}</p>}
              </div>
              <div className="card">
                <h3>成交明细（{(detail.trades ?? []).length} 笔）</h3>
                {(detail.trades ?? []).length > 0 ? (
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
                      {(detail.trades ?? []).map((t) => (
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
                ) : (
                  <p className="placeholder">
                    成交明细仅保留在进程内缓存中；服务重启后回看只有净值与指标。
                  </p>
                )}
                {(detail.logs ?? []).length > 0 && (
                  <details>
                    <summary>过程日志（{detail.logs?.length} 条）</summary>
                    <pre className="logs">{detail.logs?.join('\n')}</pre>
                  </details>
                )}
              </div>
            </>
          ) : runningNow ? (
            <div className="card placeholder">运行中，完成后再渲染曲线。</div>
          ) : (
            <div className="card placeholder">
              配置参数并点击「运行回测」；结果将展示净值曲线、指标卡片与成交明细。
            </div>
          )}

          <RunHistory activeId={detail?.run_id ?? null} onPick={(id) => {
            // 回看不启动进度轮询：只拉详情，避免历史运行反复请求 /progress
            setActiveId(null)
            setProg(null)
            setError('')
            loadResult(id)
          }} />
        </div>
      </div>
    </section>
  )
}
