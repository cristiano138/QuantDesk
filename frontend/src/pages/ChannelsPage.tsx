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
  resolveReconcileDiff,
  type AppSettings,
  type ChannelInfo,
  type ChannelsOverview,
  type MaskedCredential,
  type RealtimeState,
  type ReconcileDiff,
} from '../api/client'
import HelpLink from '../components/HelpLink'

const RISK_CONFIRM_TEXT = '我知晓并承担实盘交易风险'

const CAP_CN: Record<string, string> = {
  history_bar: '历史 K 线',
  realtime_tick: '实时行情',
  realtime_bar: '实时 K 线',
  financial: '财务数据',
  transaction: '逐笔成交',
  dynamic: '多源组合',
}

const STATUS_TEXT: Record<string, string> = {
  ready: '可用',
  unready: '需先安装',
  disabled: '已关闭',
}

/** 行情来源在界面上只出现产品名，不出现内部代号。 */
const FEED_CN: Record<string, string> = {
  qmt_xtdata: 'miniQMT 行情',
  tdx_aidata: '通达信行情',
  tx_quote: '腾讯行情',
  em_quote: '东方财富行情',
  akshare: '免费公开数据',
  tushare: 'Tushare 专业行情',
  ths_ifind: '同花顺 iFinD',
  paper: '内置模拟盘',
}

const KIND_LABEL: Record<string, string> = {
  order_missing_local: '券商有委托、本地无记录',
  order_state_mismatch: '委托状态不一致',
  trade_missing_local: '券商有成交、本地无记录',
  position_mismatch: '持仓不一致',
  account_mismatch: '资金不一致',
  // 每轮无差异也留一条记录：它是核对留痕，不是差异，所以不给「标为已处置」
  clean: '本轮核对无差异（留痕）',
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
            <span className="sub-note">（需先安装并登录对应软件）</span>
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
          {!row.official && <span className="dialog-red">非官方软件，稳定性低</span>}
        </td>
        <td>
          <button className="mini" disabled={probing} onClick={probe}>
            {probing ? '检测中…' : '测试连接'}
          </button>
          {probeMsg && <span className="sub-note">{probeMsg}</span>}
        </td>
      </tr>
      <tr className="detail-row">
        <td colSpan={7}>
          <details>
            <summary>如何启用</summary>
            <p className="prereq">{row.prereq || '（暂未提供说明）'}</p>
            {row.reason && row.status === 'unready' && (
              <p className="dialog-red">还没有检测到对应的行情或交易软件，请先安装并登录。</p>
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
      <h3>账户与密钥</h3>
      <p className="dialog-red">加密保存在本机账户下，换电脑需重新录入；保存后不再显示原文。</p>
      {spec.length === 0 && <p className="placeholder">暂不需要填写账户信息。</p>}
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
      <h4>已保存的账户信息（隐藏显示）</h4>
      {stored.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>项目</th>
              <th>显示</th>
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
        <p className="placeholder">还没有填写任何账户信息。</p>
      )}
      {msg && <p className="badge badge-paper">{msg}</p>}
      {err && <p className="badge badge-down">{err}</p>}
    </div>
  )
}

function ReconcileSection({ labelOf }: { labelOf: (name: string) => string }) {
  const [diffs, setDiffs] = useState<ReconcileDiff[]>([])
  const [status, setStatus] = useState<ChannelsOverview['reconcile'] | null>(null)
  const [running, setRunning] = useState(false)
  const [err, setErr] = useState('')
  const [onlyOpen, setOnlyOpen] = useState(true)
  const [includeClean, setIncludeClean] = useState(false)
  const [notes, setNotes] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState<number | null>(null)

  const refresh = useCallback(() => {
    return getReconcile(200, onlyOpen, !includeClean)
      .then((d) => {
        setDiffs(d.diffs)
        setStatus(d.status)
      })
      .catch((e: unknown) => setErr(apiError(e)))
  }, [onlyOpen, includeClean])

  useEffect(() => {
    refresh()
    return openEvents((data) => {
      const d = data as { type?: string; data?: { action?: string } }
      if (d?.type === 'risk' && d.data?.action === 'reconcile_diff') refresh()
      if (d?.type === 'risk' && d.data?.action === 'reconcile_resolved') refresh()
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

  const mark = async (id: number, resolve: boolean) => {
    setBusy(id)
    setErr('')
    try {
      await resolveReconcileDiff(id, notes[id] ?? '', resolve)
      setNotes((cur) => ({ ...cur, [id]: '' }))
      await refresh()
    } catch (e) {
      setErr(apiError(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card">
      <h3>持仓与资金核对</h3>
      {status && (
        <p className="sub-note">
          上次核对：{status.ran_at ? new Date(status.ran_at).toLocaleString('zh-CN') : '尚未执行'}
          {' '}· 来源 {status.channel ? labelOf(status.channel) : '—'} · 每 {status.interval_s} 秒自动核对 ·
          {status.running ? ' 正在核对' : ' 当前未运行'}
          {status.diffs != null ? ` · 本轮差异 ${status.diffs} 项` : ''}
          {status.open != null ? ` · 待处置 ${status.open} 项` : ''}
          {status.skipped ? ` · 跳过原因：${status.skipped}` : ''}
        </p>
      )}
      <div className="row-btns" style={{ gap: 8 }}>
        <button className="mini" disabled={running} onClick={runNow}>
          {running ? '核对中…' : '立即核对'}
        </button>
        <button className="mini" onClick={() => setOnlyOpen((v) => !v)}>
          {onlyOpen ? '看全部（含已处置）' : '只看未处置'}
        </button>
        <label className="mini" title="每轮核对即使没有差异也会留下一条记录；默认不显示">
          <input type="checkbox" checked={includeClean}
                 onChange={(e) => setIncludeClean(e.target.checked)} />
          {' '}含每轮无差异记录
        </label>
      </div>
      {err && <p className="badge badge-down">{err}</p>}
      {diffs.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>类型</th>
              <th>对象</th>
              <th>来源</th>
              <th>处置</th>
            </tr>
          </thead>
          <tbody>
            {diffs.map((d) => (
              <tr key={d.id}>
                <td>{new Date(d.ran_at).toLocaleString('zh-CN')}</td>
                <td>{KIND_LABEL[d.kind] ?? d.kind}</td>
                <td>{d.ref}</td>
                <td>{labelOf(d.channel)}</td>
                <td>
                  {d.kind === 'clean' ? (
                    <span className="sub-note">—（本轮无差异，无需处置）</span>
                  ) : d.resolved ? (
                    <span className="sub-note">
                      已处置{d.resolve_note ? `：${d.resolve_note}` : ''}
                      <button className="mini" disabled={busy === d.id} onClick={() => void mark(d.id, false)}>
                        退回
                      </button>
                    </span>
                  ) : (
                    <span style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                      <input
                        placeholder="核实结论（可留空）"
                        value={notes[d.id] ?? ''}
                        onChange={(e) => setNotes((cur) => ({ ...cur, [d.id]: e.target.value }))}
                        style={{ width: 160 }}
                      />
                      <button
                        className="mini"
                        disabled={busy === d.id}
                        onClick={() => void mark(d.id, true)}
                        title="标为已处置：这条差异已有人核对并留下结论，不再计入待处置"
                      >
                        标为已处置
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="placeholder">
          {onlyOpen
            ? '没有待处置的差异。已处置的记录点「看全部（含已处置）」回看。'
            : '暂无差异记录（还没核对过的话，点上面「立即核对」）。'}
        </p>
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
      <h3>实时行情</h3>
      <button className="mini" onClick={refresh}>
        刷新
      </button>
      {err && <p className="badge badge-down">{err}</p>}
      {rt ? (
        <>
          <p className="sub-note">
            当前行情来源：<b>{FEED_CN[rt.realtime_source] ?? (rt.realtime_source || '（无）')}</b> · 正在跟踪 {rt.watched.length} 只标的
          </p>
          {entries.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>策略</th>
                  <th>K 线周期</th>
                  <th>关注的股票</th>
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
            <p className="placeholder">暂无策略使用实时行情。</p>
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
        <h2>行情与交易<HelpLink slug="channels" /></h2>
        <p className="placeholder">加载中…</p>
        {err && <p className="badge badge-down">{err}</p>}
      </section>
    )
  }

  const brokerOptions = ov.channels.filter((c) => c.kind === 'broker' && c.status !== 'disabled')
  // 带 enable_flag 的软件（如同花顺下单）：开启/关闭都要输入风险确认
  const gateRows = ov.channels.filter((c) => !!c.enable_flag)
  const brokerName = (name: string) =>
    name === 'paper' ? '内置模拟盘' : (brokerOptions.find((c) => c.name === name)?.label ?? name)

  return (
    <section>
      <h2>行情与交易<HelpLink slug="channels" /></h2>

      <div className="card">
        <div className="row-btns">
          <button className="mini" onClick={refresh}>
            刷新
          </button>
          <span className="sub-note">
            历史 K 线 {ov.history_chain.join(' → ') || '（无）'} · 财务{' '}
            {ov.financial_chain.join(' → ') || '（无）'} · 实时{' '}
            {ov.realtime_chain.join('、') || '（无）'} · 当前使用{' '}
            <b>{ov.realtime_active || '（无）'}</b>
          </span>
        </div>
        <p className="sub-note">
          使用哪个软件的数据可在「设置」页调整；这里显示当前实际使用情况。
        </p>
        {err && <p className="badge badge-down">{err}</p>}
        {msg && <p className="badge badge-paper">{msg}</p>}
        <table>
          <thead>
            <tr>
              <th>软件 / 来源</th>
              <th>用途</th>
              <th>状态</th>
              <th>提供内容</th>
              <th>连接</th>
              <th>软件性质</th>
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
        <h3>下单账户</h3>
        <p>
          当前模式：
          <b className={ov.simulation_only ? 'badge badge-paper' : 'badge badge-live'}>
            {ov.simulation_only ? '模拟盘（由内置模拟撮合完成）' : '实盘模式'}
          </b>
          {' '}账户 {brokerName(ov.broker_channel)}
          {ov.live_connected ? ' · 实盘账户已连接' : ''}
        </p>
        {ov.live_error && <p className="dialog-red">实盘账户：{ov.live_error}</p>}
        <label>
          下单使用的账户
          <select value={ov.broker_channel} onChange={(e) => patch({ broker_channel: e.target.value }, '券商账户已保存')}>
            {brokerOptions.map((c) => (
              <option key={c.name} value={c.name}>
                {c.label}
                {c.official ? '' : ' · 非官方软件，稳定性低'}
              </option>
            ))}
          </select>
        </label>
        <label className="confirm-line">
          风险确认（切换到实盘、使用非官方软件时需要）
          <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
        </label>
        <div className="row-btns">
          {ov.simulation_only ? (
            <button className="danger-btn" onClick={() => patch({ simulation_only: false }, '已切换到实盘模式')}>
              关闭模拟盘（切换到实盘）
            </button>
          ) : (
            <button onClick={() => patch({ simulation_only: true }, '已切回模拟盘模式')}>切回模拟盘模式</button>
          )}
        </div>
        {gateRows.map((c) => (
          <div className="row-btns" key={c.name}>
            <span className="sub-note">
              {c.label}：{c.status === 'disabled' ? '未开启' : '已开启'}
              {c.official ? '' : ' · 非官方软件，稳定性低'}
            </span>
            {c.status === 'disabled' ? (
              <button
                className="danger-btn"
                onClick={() => patch({ [c.enable_flag]: true } as Partial<AppSettings>, `已开启 ${c.label}`)}
              >
                开启
              </button>
            ) : (
              <button
                className="mini"
                onClick={() => patch({ [c.enable_flag]: false } as Partial<AppSettings>, `已关闭 ${c.label}`)}
              >
                关闭
              </button>
            )}
          </div>
        ))}
        <p className="placeholder">
          输入确认文本「{RISK_CONFIRM_TEXT}」后才能提交；未输入不会生效。
        </p>
      </div>

      <VaultSection spec={ov.credentials_spec} />
      <ReconcileSection labelOf={brokerName} />
      <RealtimeSection />
    </section>
  )
}
