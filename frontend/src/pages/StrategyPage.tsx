import Editor from '@monaco-editor/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  apiError,
  getLogSources,
  listStrategyFiles,
  openEvents,
  queryLogs,
  readStrategyFile,
  saveStrategyFile,
  startStrategy,
  stopStrategy,
  strategyStatus,
  type LogRow,
  type LogSourceCount,
  type StrategyStatus,
} from '../api/client'

const NEW_TEMPLATE = `"""我的策略：只依赖 BaseStrategy/ctx，禁止 import 任何适配器（宪法第 6 条）。"""

from quantdesk.engine.strategy_base import BaseStrategy


class MyStrategy(BaseStrategy):
    name = "my_strategy"
    params = {"symbol": "600000.SH", "volume": 500}

    def on_start(self):
        self.ctx.subscribe([str(self.params["symbol"])])

    def on_bar(self, bar):
        pos = self.ctx.get_position(bar.symbol)
        held = pos.volume if pos else 0
        if held == 0:
            self.ctx.buy(bar.symbol, int(self.params["volume"]))
`

const FREQS = ['1d', '1m', '5m', '15m', '30m', '60m']
const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR']
const PAGE_SIZE = 100

function fmtTs(ms: number): string {
  return new Date(ms).toLocaleString('zh-CN')
}

/** 启动对话框：模式 + 实时聚合周期 freq，由服务端按 mode/freq 装载。 */
function StartDialog({
  open, fileName, onClose, onGo,
}: {
  open: boolean
  fileName: string
  onClose: () => void
  onGo: (mode: 'paper' | 'live', freq: string) => Promise<void>
}) {
  const [mode, setMode] = useState<'paper' | 'live'>('paper')
  const [freq, setFreq] = useState('1d')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) {
      setMode('paper')
      setFreq('1d')
      setBusy(false)
    }
  }, [open, fileName])

  if (!open) return null
  return (
    <div className="dialog-mask" onClick={busy ? undefined : onClose}>
      <div className="card dialog" onClick={(e) => e.stopPropagation()}>
        <h3>启动策略</h3>
        <p className="sub-note">文件：{fileName}</p>
        <label>
          运行模式
          <select value={mode} onChange={(e) => setMode(e.target.value as 'paper' | 'live')}>
            <option value="paper">模拟盘（paper）</option>
            <option value="live">实盘（live，需通过生命周期门禁）</option>
          </select>
        </label>
        <label>
          实时聚合周期（freq）
          <select value={freq} onChange={(e) => setFreq(e.target.value)}>
            {FREQS.map((f) => (
              <option key={f} value={f}>
                {f === '1d' ? '1d（日线）' : `${f}（分钟线）`}
              </option>
            ))}
          </select>
        </label>
        <p className="sub-note">freq 决定实时 tick 在泵内聚合成 bar 的周期；日线策略保持 1d。</p>
        <div className="dialog-actions">
          <button className="danger" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            className="primary"
            disabled={busy}
            onClick={async () => {
              setBusy(true)
              await onGo(mode, freq)
              setBusy(false)
            }}
          >
            {busy ? '启动中…' : '启动'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** 历史日志标签页：/api/system/logs/query 分页 + 关键字/来源/级别过滤。 */
function HistoryLogs({ strategyId }: { strategyId: string | null }) {
  const [source, setSource] = useState('')
  const [sources, setSources] = useState<LogSourceCount[]>([])
  const [level, setLevel] = useState('')
  const [contains, setContains] = useState('')
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [items, setItems] = useState<LogRow[]>([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    getLogSources().then(setSources).catch(() => setSources([]))
  }, [])

  const search = useCallback(
    (skip: number) => {
      setBusy(true)
      setErr('')
      queryLogs({
        source: source || undefined,
        level: level || undefined,
        contains: contains || undefined,
        strategy_id: strategyId || undefined,
        limit: PAGE_SIZE,
        offset: skip,
      })
        .then((r) => {
          setItems(r.items)
          setTotal(r.total)
          setOffset(skip)
        })
        .catch((e: unknown) => setErr(apiError(e)))
        .finally(() => setBusy(false))
    },
    [source, level, contains, strategyId],
  )

  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="logs-history">
      <div className="filter-row">
        <label>
          来源
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">全部</option>
            {source && !sources.some((x) => x.source === source) && (
              <option value={source}>{source}</option>
            )}
            {sources.map((x) => (
              <option key={x.source} value={x.source}>
                {x.source}（{x.count}）
              </option>
            ))}
          </select>
        </label>
        <label>
          级别
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l || '全部'}
              </option>
            ))}
          </select>
        </label>
        <label>
          关键字
          <input value={contains} onChange={(e) => setContains(e.target.value)} placeholder="消息内容包含" />
        </label>
        <button className="mini" disabled={busy} onClick={() => search(0)}>
          {busy ? '查询中…' : '查询'}
        </button>
      </div>
      <p className="sub-note">
        {strategyId ? `限定策略：${strategyId} · ` : '未选策略时查全部 · '}
        共 {total} 条 · 第 {page}/{pages} 页
      </p>
      {err && <p className="badge badge-down">{err}</p>}
      {items.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>级别</th>
              <th>来源</th>
              <th>策略</th>
              <th>消息</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id}>
                <td>{fmtTs(r.ts)}</td>
                <td className={r.level === 'ERROR' ? 'dialog-red' : undefined}>{r.level}</td>
                <td>{r.source}</td>
                <td>{r.strategy_id || '—'}</td>
                <td className="msg-full">{r.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="placeholder">无匹配日志。</p>
      )}
      <div className="row-btns">
        <button className="mini" disabled={busy || offset <= 0} onClick={() => search(Math.max(0, offset - PAGE_SIZE))}>
          上一页
        </button>
        <button
          className="mini"
          disabled={busy || offset + PAGE_SIZE >= total}
          onClick={() => search(offset + PAGE_SIZE)}
        >
          下一页
        </button>
      </div>
    </div>
  )
}

export default function StrategyPage() {
  const [files, setFiles] = useState<string[]>([])
  const [selected, setSelected] = useState<string>('')
  const [code, setCode] = useState<string>(NEW_TEMPLATE)
  const [dirty, setDirty] = useState(false)
  const [runningId, setRunningId] = useState<string | null>(null)
  const [status, setStatus] = useState<StrategyStatus | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  const [tab, setTab] = useState<'live' | 'history'>('live')
  const [dialog, setDialog] = useState(false)
  const logRef = useRef<HTMLPreElement>(null)

  const refreshFiles = useCallback(() => {
    listStrategyFiles()
      .then((fs) => {
        setFiles(fs)
        setSelected((cur) => cur || fs[0] || '')
      })
      .catch((e: unknown) => setMsg(apiError(e)))
  }, [])

  useEffect(() => refreshFiles(), [refreshFiles])

  // 打开文件
  useEffect(() => {
    if (!selected) return
    readStrategyFile(selected)
      .then((f) => {
        setCode(f.code)
        setDirty(false)
      })
      .catch(() => {
        setCode(NEW_TEMPLATE)
        setDirty(false)
      })
  }, [selected])

  // 启停状态轮询
  useEffect(() => {
    if (!runningId) return
    const load = () =>
      strategyStatus(runningId)
        .then(setStatus)
        .catch(() => setStatus(null))
    load()
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [runningId])

  // 日志流（WS）：LOG 事件 data 可能是 {message} 或 {action,...}，两种都要兼容
  useEffect(
    () =>
      openEvents((data) => {
        const d = data as {
          type?: string
          data?: { message?: string; action?: string; strategy_id?: string; level?: string; source?: string }
        }
        if (d?.type !== 'log') return
        const dd = d.data ?? {}
        const text = dd.message || (dd.action ? `[${dd.action}]` : '')
        if (!text) return
        const prefix = dd.source ? `${dd.source}` : ''
        const sid = dd.strategy_id
        setLogs((prev) =>
          [...prev, [prefix, sid ? `[${sid}]` : '', text].filter(Boolean).join(' ')].slice(-300),
        )
      }),
    [],
  )

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [logs])

  const save = async () => {
    if (!selected) return false
    try {
      const r = await saveStrategyFile(selected, code)
      setMsg(`已保存 ${r.saved}`)
      setDirty(false)
      refreshFiles()
      return true
    } catch (e) {
      setMsg(apiError(e))
      return false
    }
  }

  const saveAs = () => {
    const name = window.prompt('新策略文件名（如 my_ma.py）：')
    if (!name) return
    setSelected(name)
    setDirty(true)
  }

  const doStart = async (mode: 'paper' | 'live', freq: string) => {
    if (!(await save())) return
    try {
      const id = selected.replace(/\.py$/, '').split('/').pop() || 'strategy'
      const r = await startStrategy(id, mode, {}, freq)
      setRunningId(r.strategy_id || id)
      setMsg(`启动：${r.status}（模式 ${mode}，周期 ${freq}）`)
    } catch (e) {
      setMsg(apiError(e)) // 门禁 403 / 参数 400 原样回显
    } finally {
      setDialog(false)
    }
  }

  const doStop = async () => {
    if (!runningId) return
    try {
      await stopStrategy(runningId)
      setRunningId(null)
      setStatus(null)
      setMsg('已停止')
    } catch (e) {
      setMsg(apiError(e))
    }
  }

  return (
    <section>
      <h2>策略工作台</h2>
      <div className="strategy-layout">
        <div className="card strategy-tree">
          <h3>策略文件</h3>
          <ul>
            {files.map((f) => (
              <li key={f}>
                <button className={f === selected ? 'active' : ''} onClick={() => setSelected(f)}>
                  {f}
                </button>
              </li>
            ))}
          </ul>
          <button onClick={saveAs}>新建策略</button>
        </div>

        <div className="card strategy-editor">
          <div className="editor-bar">
            <span>
              {selected || '（未选择）'}
              {dirty ? ' *' : ''}
            </span>
            <div>
              <button onClick={save} disabled={!selected}>
                保存
              </button>
              {runningId ? (
                <button className="stop" onClick={doStop}>
                  停止
                </button>
              ) : (
                <button onClick={() => setDialog(true)} disabled={!selected}>
                  启动…
                </button>
              )}
            </div>
          </div>
          <Editor
            height="480px"
            theme="vs-dark"
            language="python"
            value={code}
            onChange={(v) => {
              setCode(v ?? '')
              setDirty(true)
            }}
          />
          {msg && <p className="badge">{msg}</p>}
        </div>

        <div className="card strategy-panel">
          <h3>运行状态</h3>
          {status ? (
            <>
              <p>
                状态：<b>{status.status}</b>
                {status.last_heartbeat_age_s != null && <>（心跳 {status.last_heartbeat_age_s}s 前）</>}
              </p>
              <p>重启次数/小时：{status.restarts_last_hour}</p>
            </>
          ) : (
            <p className="placeholder">未运行。选择策略后点击「启动…」。</p>
          )}

          <div className="tabs">
            <button className={tab === 'live' ? 'active' : ''} onClick={() => setTab('live')}>
              实时日志
            </button>
            <button className={tab === 'history' ? 'active' : ''} onClick={() => setTab('history')}>
              历史日志
            </button>
          </div>

          {tab === 'live' ? (
            <>
              <h4>策略日志</h4>
              <pre ref={logRef} className="logs">
                {(status?.recent_logs ?? []).join('\n')}
                {'\n'}
                {logs.slice(-40).join('\n')}
              </pre>
            </>
          ) : (
            <HistoryLogs strategyId={runningId ?? (selected ? selected.replace(/\.py$/, '').split('/').pop() ?? null : null)} />
          )}
        </div>
      </div>

      <StartDialog open={dialog} fileName={selected} onClose={() => setDialog(false)} onGo={doStart} />
    </section>
  )
}
