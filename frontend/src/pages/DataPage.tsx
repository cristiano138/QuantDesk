import { useEffect, useState } from 'react'
import {
  getAdaptersHealth,
  getLogFiles,
  getSettings,
  type AdaptersHealth,
  type AppSettings,
  type LogFileInfo,
} from '../api/client'

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={ok ? 'lamp lamp-paper' : 'lamp lamp-down'} />
}

/** 运维面板（D28）：适配器健康灯 + 日志清单。挂在数据管理页下方。 */
function OpsPanel() {
  const [health, setHealth] = useState<AdaptersHealth | null>(null)
  const [logs, setLogs] = useState<LogFileInfo[]>([])
  const [err, setErr] = useState('')

  useEffect(() => {
    const load = () => {
      getAdaptersHealth().then(setHealth).catch((e: unknown) => setErr(String(e)))
      getLogFiles().then(setLogs).catch(() => setLogs([]))
    }
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  if (err) return <p className="badge badge-down">{err}</p>
  if (!health) return <p className="placeholder">加载中…</p>

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
          {health.feeds.map((f, i) => (
            <tr key={f.name}>
              <td>{f.name}</td>
              <td><StatusDot ok={f.registered} />{f.status}</td>
              <td>{health.brokers[i]?.name ?? ''}</td>
              <td>
                {health.brokers[i] && (
                  <>
                    <StatusDot ok={health.brokers[i].registered} />
                    {health.brokers[i].status}
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

export default function DataPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [symbol, setSymbol] = useState('600000.SH')
  const [start, setStart] = useState('2019-01-01')
  const [msg, setMsg] = useState('')

  useEffect(() => {
    getSettings().then(setSettings).catch(() => undefined)
  }, [])

  return (
    <section>
      <h2>数据管理</h2>
      <div className="card">
        <h3>历史数据下载（AKShare/Tushare 联合源）</h3>
        <p className="placeholder">
          批量下载命令行（限速熔断保护，已入库标的增量零配额）：
          <br />
          <code>uv run python scripts/download_history.py --symbols {symbol} --start {start}</code>
        </p>
        <div className="watch-add">
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} />
          <input value={start} onChange={(e) => setStart(e.target.value)} style={{ maxWidth: 130 }} />
          <button onClick={() => setMsg('命令已生成（复制上方命令到终端执行；浏览器端执行在后续版本开放）')}>
            生成命令
          </button>
        </div>
        {msg && <p className="badge">{msg}</p>}
        {settings && (
          <p className="badge">
            限速：AKShare {settings.akshare_min_interval}s / Tushare {settings.tushare_min_interval}s（设置页可调）
          </p>
        )}
      </div>
      <OpsPanel />
    </section>
  )
}
