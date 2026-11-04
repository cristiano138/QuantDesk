import Editor from '@monaco-editor/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  listStrategyFiles,
  openEvents,
  readStrategyFile,
  saveStrategyFile,
  startStrategy,
  stopStrategy,
  strategyStatus,
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

export default function StrategyPage() {
  const [files, setFiles] = useState<string[]>([])
  const [selected, setSelected] = useState<string>('')
  const [code, setCode] = useState<string>(NEW_TEMPLATE)
  const [dirty, setDirty] = useState(false)
  const [runningId, setRunningId] = useState<string | null>(null)
  const [status, setStatus] = useState<StrategyStatus | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  const logRef = useRef<HTMLPreElement>(null)

  const refreshFiles = useCallback(() => {
    listStrategyFiles()
      .then((fs) => {
        setFiles(fs)
        setSelected((cur) => cur || fs[0] || '')
      })
      .catch((e: unknown) => setMsg(e instanceof Error ? e.message : String(e)))
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

  // 日志流（WS）
  useEffect(() => {
    const close = openEvents((data) => {
      const d = data as { type: string; data?: { message?: string; strategy_id?: string } }
      const msgText = d?.data?.message
      if (d?.type === 'log' && msgText) {
        const sid = d.data?.strategy_id
        setLogs((prev) => [...prev, sid ? `[${sid}] ${msgText}` : msgText].slice(-300))
      }
    })
    return close
  }, [])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [logs])

  const save = async () => {
    if (!selected) return
    try {
      const r = await saveStrategyFile(selected, code)
      setMsg(`已保存 ${r.saved}`)
      setDirty(false)
      refreshFiles()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const saveAs = () => {
    const name = window.prompt('新策略文件名（如 my_ma.py）：')
    if (!name) return
    setSelected(name)
    setDirty(true)
  }

  const toggleRun = async () => {
    try {
      if (runningId) {
        await stopStrategy(runningId)
        setRunningId(null)
        setStatus(null)
        setMsg('已停止')
      } else {
        const id = selected.replace(/\.py$/, '').split('/').pop() || 'strategy'
        await save()
        const r = await startStrategy(selected, 'paper')
        setRunningId(id)
        setMsg(`启动：${r.status}`)
      }
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
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
                <button
                  className={f === selected ? 'active' : ''}
                  onClick={() => setSelected(f)}
                >
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
              <button className={runningId ? 'stop' : ''} onClick={toggleRun} disabled={!selected}>
                {runningId ? '停止' : '模拟盘启动'}
              </button>
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
                {status.last_heartbeat_age_s != null && (
                  <>（心跳 {status.last_heartbeat_age_s}s 前）</>
                )}
              </p>
              <p>重启次数/小时：{status.restarts_last_hour}</p>
              <h4>策略日志</h4>
              <pre ref={logRef} className="logs">
                {(status.recent_logs.length > 0
                  ? status.recent_logs
                  : logs.slice(-20)
                ).join('\n')}
                {'\n'}
                {logs.slice(-20).join('\n')}
              </pre>
            </>
          ) : (
            <p className="placeholder">未运行。选择策略后点击「模拟盘启动」。</p>
          )}
        </div>
      </div>
    </section>
  )
}
