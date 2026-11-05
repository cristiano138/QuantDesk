import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  apiError,
  getDataInventory,
  getDataTask,
  getAdaptersHealth,
  getLogFiles,
  getSettings,
  startDataDownload,
  type AdaptersHealth,
  type AppSettings,
  type DataTask,
  type Inventory,
  type LogFileInfo,
} from '../api/client'
import HelpLink from '../components/HelpLink'

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={ok ? 'lamp lamp-paper' : 'lamp lamp-down'} />
}

const STATUS_CN: Record<string, string> = {
  ready: '就绪',
  unready: '未就绪',
  disabled: '已禁用',
}

/** 运维面板（D28）：适配器健康灯 + 日志清单。挂在数据管理页下方。 */
function OpsPanel() {
  const [health, setHealth] = useState<AdaptersHealth | null>(null)
  const [logs, setLogs] = useState<LogFileInfo[]>([])
  const [err, setErr] = useState('')

  useEffect(() => {
    const load = () => {
      getAdaptersHealth().then(setHealth).catch((e: unknown) => setErr(apiError(e)))
      getLogFiles().then(setLogs).catch(() => setLogs([]))
    }
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  if (err) return <p className="badge badge-down">{err}</p>
  if (!health) return <p className="placeholder">加载中…</p>

  // 服务端只给一列 channels（含 kind），这里按数据源/交易通道并排展示，行数取两者较多的一边
  const feeds = (health.channels ?? []).filter((c) => c.kind === 'data')
  const brokers = (health.channels ?? []).filter((c) => c.kind === 'broker')
  const rows = Array.from({ length: Math.max(feeds.length, brokers.length) }, (_, i) => ({
    feed: feeds[i],
    broker: brokers[i],
  }))

  return (
    <div className="card">
      <h3>适配器健康面板</h3>
      <p className="placeholder" style={{ marginBottom: 8 }}>
        状态灯 = 注册表扫描结果（依赖缺失/未接入的适配器显示灰红）；探活不外呼、不耗数据源配额。
      </p>
      <table>
        <thead>
          <tr><th>数据源</th><th>状态</th><th>交易通道</th><th>状态</th></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.feed?.name ?? 'none'}-${r.broker?.name ?? i}`}>
              <td>{r.feed ? r.feed.label : ''}</td>
              <td>
                {r.feed && (
                  <>
                    <StatusDot ok={r.feed.status === 'ready'} />
                    {STATUS_CN[r.feed.status] ?? r.feed.status}
                    {r.feed.missing.length > 0 && (
                      <span className="sub-note">（缺 {r.feed.missing.join('、')}）</span>
                    )}
                  </>
                )}
              </td>
              <td>{r.broker ? r.broker.label : ''}</td>
              <td>
                {r.broker && (
                  <>
                    <StatusDot ok={r.broker.status === 'ready'} />
                    {STATUS_CN[r.broker.status] ?? r.broker.status}
                    {r.broker.official === false && (
                      <span className="dialog-red">非官方接口，稳定性低</span>
                    )}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="badge" style={{ marginTop: 8 }}>
        通道：{health.broker_channel}｜模式：{health.simulation_only ? '模拟盘' : '实盘'}
        ｜限速：AKShare {health.limits.akshare_min_interval}s / Tushare {health.limits.tushare_min_interval}s
      </p>
      <h4>历史日志（data/logs）</h4>
      {logs.length === 0 ? (
        <p className="placeholder">暂无日志文件。</p>
      ) : (
        <table>
          <thead>
            <tr><th>文件</th><th>类型</th><th>大小</th><th>时间</th></tr>
          </thead>
          <tbody>
            {logs.slice(0, 10).map((l) => (
              <tr key={l.file}>
                <td>{l.file}</td>
                <td>{l.kind}</td>
                <td>{(l.size / 1024).toFixed(1)} KB</td>
                <td>{new Date(l.mtime).toLocaleString('zh-CN')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

const FREQS = ['1d', '1w', '1M', '1m', '5m']
const ADJUSTS = ['none', 'qfq', 'hfq']
const FREQ_CN: Record<string, string> = {
  '1d': '1d 日线',
  '1w': '1w 周线',
  '1M': '1M 月线',
  '1m': '1m 分钟线',
  '5m': '5m 分钟线',
}
const ADJUST_CN: Record<string, string> = {
  none: 'none 不复权（下单/看盘口径）',
  qfq: 'qfq 前复权（回测与指标口径）',
  hfq: 'hfq 后复权',
}

const TERMINAL = new Set(['done', 'error'])

/** 历史数据下载：真提交任务 + 1s 轮询进度，逐标的回显写入行数与失败原因。 */
function DownloadPanel() {
  const [symbols, setSymbols] = useState('600000.SH')
  const [start, setStart] = useState('2019-01-01')
  const [end, setEnd] = useState('')
  const [freq, setFreq] = useState('1d')
  const [adjust, setAdjust] = useState('qfq')
  const [taskId, setTaskId] = useState('')
  const [task, setTask] = useState<DataTask | null>(null)
  const [starting, setStarting] = useState(false)
  const [err, setErr] = useState('')
  const [hint, setHint] = useState('')

  // 任务未进终态就 1s 问一次；进终态（done/error）自动停，不留 timer
  useEffect(() => {
    if (!taskId) return
    if (task && TERMINAL.has(task.state)) return
    let alive = true
    const timer = window.setTimeout(() => {
      getDataTask(taskId)
        .then((t) => {
          if (alive) {
            setTask(t)
            setErr('')
          }
        })
        .catch((e: unknown) => {
          if (alive) setErr(apiError(e))
        })
    }, 1000)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [taskId, task])

  const submit = async () => {
    setErr('')
    setHint('')
    const list = symbols.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean)
    if (list.length === 0) {
      setErr('请先填写标的代码（逗号分隔，如 600000.SH,000001.SZ,00700.HK）')
      return
    }
    setStarting(true)
    setTask(null)
    setTaskId('')
    try {
      const r = await startDataDownload({
        symbols: list,
        freq,
        start: start || undefined,
        end: end || undefined,
        adjust,
      })
      setTaskId(r.task_id)
      setHint(`任务已提交（${list.length} 只标的），正在排队…`)
    } catch (e) {
      setErr(apiError(e)) // 后端给的限流/依赖缺失原因原样上屏
    } finally {
      setStarting(false)
    }
  }

  const okEntries = Object.entries(task?.ok ?? {})
  const failEntries = Object.entries(task?.failed ?? {})
  const totalRows = okEntries.reduce((acc, [, n]) => acc + n, 0)
  const percent = task && task.total > 0 ? Math.round((task.done / task.total) * 100) : 0
  const finished = !!task && task.state === 'done'
  const errored = !!task && task.state === 'error'

  return (
    <div className="card">
      <h3>历史数据下载（界面提交任务）</h3>
      <p className="sub-note">
        下载走服务端限速与降级路由（AKShare/Tushare 等按设置页优先级），已入库标的是增量补、不覆盖已有行。
        下载完的数据写在本地 Parquet，回测与行情面板读的就是它。
      </p>

      <div className="form-grid">
        <label>
          标的代码（逗号分隔）
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            placeholder="600000.SH,000001.SZ,00700.HK"
          />
        </label>
        <label>
          周期
          <select value={freq} onChange={(e) => setFreq(e.target.value)}>
            {FREQS.map((f) => (
              <option key={f} value={f}>{FREQ_CN[f] ?? f}</option>
            ))}
          </select>
        </label>
        <label>
          开始日期
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label>
          结束日期（留空＝到今天）
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <label>
          复权
          <select value={adjust} onChange={(e) => setAdjust(e.target.value)}>
            {ADJUSTS.map((a) => (
              <option key={a} value={a}>{ADJUST_CN[a] ?? a}</option>
            ))}
          </select>
        </label>
      </div>

      <button disabled={starting} onClick={() => void submit()}>
        {starting ? '提交中…' : '开始下载'}
      </button>
      {hint && <p className="badge">{hint}</p>}
      {err && <p className="badge badge-down">{err}</p>}

      {task && (
        <>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${Math.min(100, percent)}%` }} />
          </div>
          <p className="sub-note">
            状态：{task.state === 'queued' ? '排队中' : task.state === 'running' ? '下载中' : task.state === 'done' ? '已完成' : '任务出错'}
            ｜进度 {task.done}/{task.total}
            {task.current ? `｜正在处理 ${task.current}` : ''}
          </p>
        </>
      )}

      {errored && task?.error && (
        <p className="dialog-red">任务失败：{task.error}</p>
      )}

      {okEntries.length > 0 && (
        <div className="result-block">
          <h4 style={{ margin: '0 0 6px' }}>写入结果（成功 {okEntries.length} 只）</h4>
          <table className="ob-table">
            <thead>
              <tr><th>标的</th><th>写入行数</th></tr>
            </thead>
            <tbody>
              {okEntries.map(([sym, n]) => (
                <tr key={sym}>
                  <td>{sym}</td>
                  <td>{n}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {finished && (
            <p className="badge badge-paper" style={{ marginTop: 8 }}>
              已写入本地 Parquet：data/parquet/（按 市场/代码/周期/年份 分文件），共 {totalRows} 行。
              现在可以去
              <Link to="/backtest" style={{ color: '#1d4ed8', margin: '0 4px' }}>回测报告</Link>
              跑策略；行情面板
              <Link to="/market" style={{ color: '#1d4ed8', margin: '0 4px' }}>/market</Link>
              也能看到这段 K 线。
            </p>
          )}
        </div>
      )}

      {failEntries.length > 0 && (
        <div className="result-block">
          <h4 style={{ margin: '0 0 6px' }} className="dialog-red">
            失败 {failEntries.length} 只（原因按服务端原文逐条列出，没有被吞掉）
          </h4>
          <ul className="warn" style={{ fontSize: 13, lineHeight: 1.8, margin: 0, paddingLeft: 20 }}>
            {failEntries.map(([sym, why]) => (
              <li key={sym}>
                <b>{sym}</b>：{why}
              </li>
            ))}
          </ul>
          <p className="sub-note">
            限流类原因（等待/冷却）隔几分钟重下同一批即可；「依赖缺失」类原因要到
            <Link to="/channels" style={{ color: '#1d4ed8', margin: '0 4px' }}>通道</Link>
            页把对应数据源装好并配凭据。
          </p>
        </div>
      )}

      {finished && failEntries.length === 0 && okEntries.length === 0 && (
        <p className="sub-note warn">任务结束但没有写入记录：后端返回的 ok 列表为空，请重试或改用下方命令行。</p>
      )}
    </div>
  )
}

/** 命令行入口保留：脚本仍然有效，且是不依赖界面的兜底路径。 */
function CliPanel({ initialSymbol, initialStart }: { initialSymbol: string; initialStart: string }) {
  const [symbol, setSymbol] = useState(initialSymbol)
  const [start, setStart] = useState(initialStart)
  const [msg, setMsg] = useState('')

  return (
    <div className="card">
      <h3>命令行（等价）</h3>
      <p className="placeholder">
        批量下载命令行（限速熔断保护，已入库标的增量零配额）：
        <br />
        <code>uv run python scripts/download_history.py --symbols {symbol || '600000.SH'} --start {start || '2019-01-01'}</code>
      </p>
      <div className="watch-add">
        <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="600000.SH,000001.SZ" />
        <input value={start} onChange={(e) => setStart(e.target.value)} style={{ maxWidth: 130 }} />
        <button onClick={() => setMsg('命令已生成：复制上面那行到终端执行（在项目根目录 F:\\量化交易 下运行）')}>
          生成命令
        </button>
      </div>
      {msg && <p className="badge">{msg}</p>}
    </div>
  )
}

/** 本地数据清单：库里到底有什么、覆盖到哪天、哪段是"有 K 线但没成交额"的降级数据。 */
function InventoryPanel() {
  const [inv, setInv] = useState<Inventory | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [q, setQ] = useState('')

  const load = useCallback(async () => {
    setBusy(true)
    setErr('')
    try {
      setInv(await getDataInventory())
    } catch (e: unknown) {
      setErr(apiError(e))
    } finally {
      setBusy(false)
    }
  }, [])

  // 只在进页面时扫一次：这一页不是盯盘，没有"实时"可言，反复扫全库只会拖慢界面
  useEffect(() => {
    void load()
  }, [load])

  const items = (inv?.items ?? []).filter(
    (it) => !q.trim() || it.symbol.toUpperCase().includes(q.trim().toUpperCase()),
  )
  const noAmount = (inv?.items ?? []).filter((it) => it.amount_zero_pct > 50)

  return (
    <div className="card">
      <div className="row-btns">
        <h3>本地数据清单（回测与行情面板读的就是这些）</h3>
        <button disabled={busy} onClick={() => void load()}>
          {busy ? '扫描中…' : '重新扫描'}
        </button>
      </div>
      {err && (
        <p className="badge badge-down">
          清单读取失败：{err}
          <button style={{ marginLeft: 8 }} onClick={() => void load()}>重试</button>
        </p>
      )}
      {inv && (
        <>
          <p className="sub-note">
            目录 {inv.root} ｜ {inv.symbols} 个标的 · {inv.entries} 条（标的×周期×复权） · {inv.rows} 行
            {inv.bad_files > 0 ? ` · ${inv.bad_files} 个文件读不出来（已排除在统计外）` : ''}
          </p>
          {noAmount.length > 0 && (
            <p className="warn">
              {noAmount.length} 条数据的成交额 ≥50% 为 0（例如 {noAmount[0].symbol} {noAmount[0].freq}{' '}
              {noAmount[0].adjust}：{noAmount[0].amount_zero_pct}%）。这类行通常是公开免费源的降级数据
              （腾讯日线不返回成交额）——量、额相关的回测指标在这种数据上不可信，
              换 akshare/tushare 重下这一段（
              <Link to="/manual?s=data" style={{ color: '#1d4ed8' }}>怎么说？</Link>）
            </p>
          )}
          <input
            placeholder="按代码过滤，如 600 或 00700.HK"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ marginBottom: 8 }}
          />
          {items.length === 0 ? (
            <p className="placeholder">
              {inv.items.length === 0
                ? '本地库是空的：还没有任何标的被下载进来。回测与行情面板此时都会报「本地无数据」。'
                : '没有匹配这个过滤词的条目。'}
            </p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>标的</th><th>周期</th><th>复权</th><th>行数</th><th>覆盖区间</th><th>成交额缺失</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <tr key={`${it.symbol}-${it.freq}-${it.adjust}`}>
                    <td>{it.symbol}</td>
                    <td>{it.freq}</td>
                    <td>{it.adjust}</td>
                    <td>{it.rows.toLocaleString('zh-CN')}</td>
                    <td>{it.start} ~ {it.end}</td>
                    <td className={it.amount_zero_pct > 50 ? 'dialog-red' : ''}>
                      {it.amount_zero_pct}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
      {!inv && !err && <p className="placeholder">正在扫描本地库…</p>}
    </div>
  )
}

export default function DataPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [settingsErr, setSettingsErr] = useState('')

  useEffect(() => {
    getSettings().then(setSettings).catch((e: unknown) => setSettingsErr(apiError(e)))
  }, [])

  return (
    <section>
      <h2>数据管理<HelpLink slug="data" /></h2>
      <DownloadPanel />
      <InventoryPanel />
      <CliPanel initialSymbol="600000.SH" initialStart="2019-01-01" />
      {settings && (
        <p className="badge">
          限速：AKShare {settings.akshare_min_interval}s / Tushare {settings.tushare_min_interval}s
          （
          <Link to="/settings" style={{ color: '#1d4ed8' }}>设置页</Link>
          可调）
        </p>
      )}
      {settingsErr && <p className="badge badge-down">设置读取失败：{settingsErr}</p>}
      <OpsPanel />
    </section>
  )
}
