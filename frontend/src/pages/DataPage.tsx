import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  apiError,
  getDataInventory,
  getDataTask,
  startDataDownload,
  type DataTask,
  type Inventory,
} from '../api/client'
import HelpLink from '../components/HelpLink'

const FREQS = ['1d', '1w', '1M', '1m', '5m']
const ADJUSTS = ['none', 'qfq', 'hfq']
const FREQ_CN: Record<string, string> = {
  '1d': '日线',
  '1w': '周线',
  '1M': '月线',
  '1m': '1 分钟',
  '5m': '5 分钟',
  '15m': '15 分钟',
  '30m': '30 分钟',
  '60m': '60 分钟',
}
const ADJUST_CN: Record<string, string> = {
  none: '不复权（下单与看盘口径）',
  forward: '前复权（回测与指标口径）',
  backward: '后复权',
  qfq: '前复权（回测与指标口径）',
  hfq: '后复权',
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
      setErr(apiError(e)) // 限流/缺少行情软件的原因原样上屏，不吞掉
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
      <h3>历史 K 线下载</h3>
      <p className="sub-note">
        下载会自动控制速度并在多个行情来源之间切换，已经下载过的日期只补缺、不覆盖。
        下载完成后保存在本机，回测与行情面板直接使用。
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
              已保存到本机，共 {totalRows} 行。
              现在可以去
              <Link to="/backtest" style={{ color: '#1d4ed8', margin: '0 4px' }}>回测报告</Link>
              跑策略；在
              <Link to="/market" style={{ color: '#1d4ed8', margin: '0 4px' }}>行情面板</Link>
              也能看到这段 K 线。
            </p>
          )}
        </div>
      )}

      {failEntries.length > 0 && (
        <div className="result-block">
          <h4 style={{ margin: '0 0 6px' }} className="dialog-red">
            失败 {failEntries.length} 只（原因逐条列出）
          </h4>
          <ul className="warn" style={{ fontSize: 13, lineHeight: 1.8, margin: 0, paddingLeft: 20 }}>
            {failEntries.map(([sym, why]) => (
              <li key={sym}>
                <b>{sym}</b>：{why}
              </li>
            ))}
          </ul>
          <p className="sub-note">
            限流类原因（等待/冷却）隔几分钟重下同一批即可；需要先安装行情软件的，到
            <Link to="/channels" style={{ color: '#1d4ed8', margin: '0 4px' }}>行情与交易</Link>
            页按提示安装并登录。
          </p>
        </div>
      )}

      {finished && failEntries.length === 0 && okEntries.length === 0 && (
        <p className="sub-note warn">任务结束但没有写入记录，请稍后重试。</p>
      )}
    </div>
  )
}

/** 本地数据清单：已下载了哪些标的、覆盖到哪天、哪段是"有 K 线但没成交额"的数据。 */
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
        <h3>已下载的历史 K 线（回测与行情面板使用的就是这些）</h3>
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
            {inv.symbols} 个标的 · {inv.entries} 条（标的×周期×复权） · {inv.rows} 行
            {inv.bad_files > 0 ? ` · ${inv.bad_files} 条数据读取异常（已排除在统计外）` : ''}
          </p>
          {noAmount.length > 0 && (
            <p className="warn">
              {noAmount.length} 条数据的成交额 ≥50% 为 0（例如 {noAmount[0].symbol} {noAmount[0].freq}{' '}
              {noAmount[0].adjust}：{noAmount[0].amount_zero_pct}%）。这类数据来自不提供成交额的行情来源
              （如腾讯日线）——涉及量、额的回测指标在这类数据上不可信，
              请改用能提供成交额的行情来源重新下载这一段（
              <Link to="/manual?s=data" style={{ color: '#1d4ed8' }}>怎么处理？</Link>）
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
                ? '还没有下载任何标的。回测与行情面板此时会提示「本地无数据」。'
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
                    <td>{FREQ_CN[it.freq] ?? it.freq}</td>
                    <td>{ADJUST_CN[it.adjust] ?? it.adjust}</td>
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
      {!inv && !err && <p className="placeholder">正在读取已下载数据…</p>}
    </div>
  )
}

export default function DataPage() {
  return (
    <section>
      <h2>数据管理<HelpLink slug="data" /></h2>
      <DownloadPanel />
      <InventoryPanel />
      <p className="badge">
        下载间隔与行情来源优先级可在
        <Link to="/settings" style={{ color: '#1d4ed8', margin: '0 4px' }}>设置页</Link>
        调整
      </p>
    </section>
  )
}
