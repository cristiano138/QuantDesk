import { useCallback, useEffect, useState } from 'react'
import {
  apiError,
  deleteCredential,
  getChannels,
  getRealtimeState,
  getReconcile,
  listCredentials,
  openEvents,
  probeChannel,
  putCredential,
  putSettings,
  runReconcile,
  type AppSettings,
  type ChannelInfo,
  type ChannelsOverview,
  type MaskedCredential,
  type RealtimeState,
  type ReconcileDiff,
} from '../api/client'

const RISK_CONFIRM_TEXT = '我知晓并承担实盘交易风险'

const CAP_CN: Record<string, string> = {
  history_bar: '历史K线',
  realtime_tick: '实时Tick',
  realtime_bar: '实时Bar',
  financial: '财务数据',
  transaction: '逐笔成交',
  dynamic: '动态聚合',
}

const STATUS_TEXT: Record<string, string> = {
  ready: '就绪',
  unready: '未就绪',
  disabled: '已禁用',
}

const KIND_LABEL: Record<string, string> = {
  order_missing_local: '券商有委托、本地无记录',
  order_state_mismatch: '委托状态不一致',
  trade_missing_local: '券商有成交、本地无记录',
  position_mismatch: '持仓不一致',
  account_mismatch: '资金不一致',
}

function lampClass(c: ChannelInfo): string {
  if (c.status === 'ready') return 'lamp lamp-ready'
  if (c.status === 'unready') return 'lamp lamp-unready'
  return 'lamp lamp-disabled'
}

function isSecretKey(key: string): boolean {
  return /passw|token|secret|key/i.test(key)
}

function ChannelRow({ row, onChanged }: { row: ChannelInfo; onChanged: () => void }) {
  const [probing, setProbing] = useState(false)
  const [probeMsg, setProbeMsg] = useState('')

  const probe = async () => {
    setProbing(true)
    setProbeMsg('')
    try {
      const r = await probeChannel(row.name)
      setProbeMsg(r.ok ? `通过 · ${r.latency_ms} ms` : `未通过 · ${r.latency_ms} ms`)
    } catch (e) {
      setProbeMsg(apiError(e))
    } finally {
      setProbing(false)
      onChanged()
    }
  }

  return (
    <>
      <tr>
        <td>
          <span className={lampClass(row)} />
          {row.label}
        </td>
        <td>{row.kind === 'data' ? '行情' : '交易'}</td>
        <td>
          {STATUS_TEXT[row.status] ?? row.status}
          {row.status === 'unready' && row.missing.length > 0 && (
            <span className="sub-note">（缺 {row.missing.join('、')}）</span>
          )}
        </td>
        <td>
          {row.capabilities.map((c) => (
            <span className="tag" key={c}>
              {CAP_CN[c] ?? c}
            </span>
          ))}
        </td>
        <td>{row.connected ? <span className="badge badge-paper">已连接</span> : <span className="sub-note">—</span>}</td>
        <td>
          {!row.official && <span className="dialog-red">非官方接口，稳定性低</span>}
        </td>
        <td>
          <button className="mini" disabled={probing} onClick={probe}>
            {probing ? '探活中…' : '测试连接'}
          </button>
          {probeMsg && <span className="sub-note">{probeMsg}</span>}
        </td>
      </tr>
      <tr className="detail-row">
        <td colSpan={7}>
          <details>
            <summary>接入引导 / 诊断</summary>
            <p className="prereq">{row.prereq || '（后端未提供前置说明）'}</p>
            {row.reason && <p className="dialog-red">原因：{row.reason}</p>}
            {row.requires.length > 0 && (
              <p className="sub-note">
                依赖：{row.requires.join('、')}
                {row.requires_any ? '（任一可用即可）' : '（全部需要）'}
              </p>
            )}
            {!row.default_enabled && (
              <p className="sub-note">
                默认禁用，需设置项 {row.enable_flag || '（未声明）'} 打开后进入交易视野。
              </p>
            )}
            {row.channel_health && (
              <p className="sub-note">
                探活 {row.channel_health.probes} 次 / 失败 {row.channel_health.probe_failures}
                {' '}· 重连 {row.channel_health.reconnects} 次 · 状态 {row.channel_health.state}
                {row.channel_health.last_error ? ` · 最近错误 ${row.channel_health.last_error}` : ''}
              </p>
            )}
          </details>
        </td>
      </tr>
    </>
  )
}

function VaultSection({ spec }: { spec: ChannelsOverview['credentials_spec'] }) {
  const [stored, setStored] = useState<MaskedCredential[]>([])
  const [form, setForm] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const refresh = useCallback(() => {
    listCredentials().then(setStored).catch((e: unknown) => setErr(apiError(e)))
  }, [])

  useEffect(refresh, [refresh])

  const save = async (key: string) => {
    setErr('')
    setMsg('')
    const value = form[key] ?? ''
    try {
      await putCredential(key, value)
      setForm((f) => ({ ...f, [key]: '' })) // 保存后不回显明文
      setMsg(value ? `已保存 ${key}（输入框已清空）` : `已清空 ${key}`)
      refresh()
    } catch (e) {
      setErr(apiError(e)) // 400 原样回显服务端原因
    }
  }

  const erase = async (key: string) => {
    setErr('')
    try {
      await deleteCredential(key)
      refresh()
    } catch (e) {
      setErr(apiError(e))
    }
  }

  return (
    <div className="card">
      <h3>凭据保险库</h3>
      <p className="dialog-red">加密存储于本机 Windows 账户，换机需重录；保存后不回显明文。</p>
      {spec.length === 0 && <p className="placeholder">后端未提供凭据项。</p>}
      {spec.map((s) => {
        const secret = isSecretKey(s.key)
        return (
          <label key={s.key}>
            {s.channel} · {s.label}
            {s.required ? '（必填）' : '（可选）'}
            <div className="cred-line">
              <input
                type={secret ? 'password' : 'text'}
                value={form[s.key] ?? ''}
                placeholder="录入后保存即清空"
                onChange={(e) => setForm((f) => ({ ...f, [s.key]: e.target.value }))}
              />
              <button className="mini" onClick={() => save(s.key)}>
                保存
              </button>
            </div>
          </label>
        )
      })}
      <h4>已存凭据（掩码）</h4>
      {stored.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>键名</th>
              <th>掩码</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {stored.map((c) => (
              <tr key={c.key}>
                <td>{c.key}</td>
                <td>{c.value_masked}</td>
                <td>
                  <button className="mini" onClick={() => erase(c.key)}>
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="placeholder">尚未录入任何凭据。</p>
      )}
      {msg && <p className="badge badge-paper">{msg}</p>}
      {err && <p className="badge badge-down">{err}</p>}
    </div>
  )
}

function ReconcileSection() {
  const [diffs, setDiffs] = useState<ReconcileDiff[]>([])
  const [status, setStatus] = useState<ChannelsOverview['reconcile'] | null>(null)
  const [running, setRunning] = useState(false)
  const [err, setErr] = useState('')

  const refresh = useCallback(() => {
    return getReconcile()
      .then((d) => {
        setDiffs(d.diffs)
        setStatus(d.status)
      })
      .catch((e: unknown) => setErr(apiError(e)))
  }, [])

  useEffect(() => {
    refresh()
    return openEvents((data) => {
      const d = data as { type?: string; data?: { action?: string } }
      if (d?.type === 'risk' && d.data?.action === 'reconcile_diff') refresh()
    })
  }, [refresh])

  const runNow = async () => {
    setRunning(true)
    setErr('')
    try {
      await runReconcile()
      await refresh()
    } catch (e) {
      setErr(apiError(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="card">
      <h3>对账</h3>
      {status && (
        <p className="sub-note">
          上次核对：{status.ran_at ? new Date(status.ran_at).toLocaleString('zh-CN') : '尚未执行'}
          {' '}· 通道 {status.channel || '—'} · 周期 {status.interval_s}s ·
          {status.running ? ' 运行中' : ' 未运行'}
          {status.diffs != null ? ` · 本轮差异 ${status.diffs} 项` : ''}
          {status.skipped ? ` · 跳过原因：${status.skipped}` : ''}
        </p>
      )}
      <button className="mini" disabled={running} onClick={runNow}>
        {running ? '对账中…' : '立即对账'}
      </button>
      {err && <p className="badge badge-down">{err}</p>}
      {diffs.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>类型</th>
              <th>对象</th>
              <th>通道</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {diffs.map((d) => (
              <tr key={d.id}>
                <td>{new Date(d.ran_at).toLocaleString('zh-CN')}</td>
                <td>{KIND_LABEL[d.kind] ?? d.kind}</td>
                <td>{d.ref}</td>
                <td>{d.channel}</td>
                <td>
                  <details>
                    <summary>展开</summary>
                    <pre className="logs">{JSON.stringify(d.detail, null, 2)}</pre>
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="placeholder">暂无差异记录。</p>
      )}
    </div>
  )
}

function RealtimeSection() {
  const [rt, setRt] = useState<RealtimeState | null>(null)
  const [err, setErr] = useState('')

  const refresh = useCallback(() => {
    getRealtimeState().then(setRt).catch((e: unknown) => setErr(apiError(e)))
  }, [])

  useEffect(refresh, [refresh])

  const entries = rt ? Object.entries(rt.strategies) : []

  return (
    <div className="card">
      <h3>实时泵</h3>
      <button className="mini" onClick={refresh}>
        刷新
      </button>
      {err && <p className="badge badge-down">{err}</p>}
      {rt ? (
        <>
          <p className="sub-note">
            当前实时源：<b>{rt.realtime_source || '（无）'}</b> · 已盯标的 {rt.watched.length} 只 ·
            ticks {rt.ticks_seen} · bars {rt.bars_emitted}
          </p>
          {entries.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>策略</th>
                  <th>聚合周期</th>
                  <th>订阅标的</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(([sid, s]) => (
                  <tr key={sid}>
                    <td>{sid}</td>
                    <td>{s.freq}</td>
                    <td>{s.symbols.join('、')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="placeholder">没有策略向实时泵订阅。</p>
          )}
        </>
      ) : (
        <p className="placeholder">加载中…</p>
      )}
    </div>
  )
}

export default function ChannelsPage() {
  const [ov, setOv] = useState<ChannelsOverview | null>(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [confirmText, setConfirmText] = useState('')

  const refresh = useCallback(() => {
    getChannels().then(setOv).catch((e: unknown) => setErr(apiError(e)))
  }, [])

  useEffect(() => {
    refresh()
    return openEvents((data) => {
      const d = data as { type?: string; data?: { action?: string } }
      const a = d?.data?.action
      if (d?.type === 'risk' && (a === 'channel_up' || a === 'channel_down')) refresh()
    })
  }, [refresh])

  const patch = async (body: Parameters<typeof putSettings>[0], note: string) => {
    setErr('')
    setMsg('')
    try {
      await putSettings(body, confirmText || undefined)
      setMsg(note)
      setConfirmText('')
      refresh()
    } catch (e) {
      setErr(apiError(e)) // 服务端 400 原文回显，前端不判定
    }
  }

  if (!ov) {
    return (
      <section>
        <h2>通道</h2>
        <p className="placeholder">加载中…</p>
        {err && <p className="badge badge-down">{err}</p>}
      </section>
    )
  }

  const brokerOptions = ov.channels.filter((c) => c.kind === 'broker' && c.status !== 'disabled')
  // 带 enable_flag 的通道（如 easytrader）：启用/禁用都走服务端确认文本校验
  const gateRows = ov.channels.filter((c) => !!c.enable_flag)

  return (
    <section>
      <h2>通道</h2>

      <div className="card">
        <div className="row-btns">
          <button className="mini" onClick={refresh}>
            刷新全景
          </button>
          <span className="sub-note">
            历史链 {ov.history_chain.join(' → ') || '（无）'} · 财务链{' '}
            {ov.financial_chain.join(' → ') || '（无）'} · 实时优先级{' '}
            {ov.realtime_chain.join('、') || '（无）'} · 当前实时源{' '}
            <b>{ov.realtime_active || '（无）'}</b>
          </span>
        </div>
        <p className="sub-note">
          路由优先级在「设置」页修改（服务端按注册表校验通道名）；这里显示的是当前生效配置与实际装配结果。
        </p>
        {err && <p className="badge badge-down">{err}</p>}
        {msg && <p className="badge badge-paper">{msg}</p>}
        <table>
          <thead>
            <tr>
              <th>通道</th>
              <th>类型</th>
              <th>状态</th>
              <th>能力</th>
              <th>连接</th>
              <th>接口性质</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {ov.channels.map((c) => (
              <ChannelRow key={c.name} row={c} onChanged={refresh} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>交易通道路由</h3>
        <p>
          当前模式：
          <b className={ov.simulation_only ? 'badge badge-paper' : 'badge badge-live'}>
            {ov.simulation_only ? '模拟盘（一律走内置模拟撮合）' : '实盘模式'}
          </b>
          {' '}通道 {ov.broker_channel}
          {ov.live_connected ? ' · 实盘通道已连接' : ''}
        </p>
        {ov.live_error && <p className="dialog-red">实盘通道：{ov.live_error}</p>}
        <label>
          交易通道（仅列出 API 可见通道 + 内置模拟撮合）
          <select value={ov.broker_channel} onChange={(e) => patch({ broker_channel: e.target.value }, '通道已保存')}>
            {brokerOptions.map((c) => (
              <option key={c.name} value={c.name}>
                {c.label}
                {c.official ? '' : ' · 非官方接口，稳定性低'}
              </option>
            ))}
          </select>
        </label>
        <label className="confirm-line">
          确认文本（服务端强制校验，非 paper / 关模拟盘 / 启用非官方通道时必填）
          <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
        </label>
        <div className="row-btns">
          {ov.simulation_only ? (
            <button className="danger-btn" onClick={() => patch({ simulation_only: false }, '已切换到实盘模式')}>
              关闭模拟盘（切实盘）
            </button>
          ) : (
            <button onClick={() => patch({ simulation_only: true }, '已切回模拟盘模式')}>切回模拟盘模式</button>
          )}
        </div>
        {gateRows.map((c) => (
          <div className="row-btns" key={c.name}>
            <span className="sub-note">
              {c.label}：{c.status === 'disabled' ? '未启用（不在交易 API 视野）' : '已启用'}
              {c.official ? '' : ' · 非官方接口，稳定性低'}
            </span>
            {c.status === 'disabled' ? (
              <button
                className="danger-btn"
                onClick={() => patch({ [c.enable_flag]: true } as Partial<AppSettings>, `已启用 ${c.label}`)}
              >
                启用
              </button>
            ) : (
              <button
                className="mini"
                onClick={() => patch({ [c.enable_flag]: false } as Partial<AppSettings>, `已禁用 ${c.label}`)}
              >
                禁用
              </button>
            )}
          </div>
        ))}
        <p className="placeholder">
          输入确认文本「{RISK_CONFIRM_TEXT}」后提交；未带确认文本时服务端返回 400，原因在上方原样显示。
          前端不做任何风险判定。
        </p>
      </div>

      <VaultSection spec={ov.credentials_spec} />
      <ReconcileSection />
      <RealtimeSection />
    </section>
  )
}
