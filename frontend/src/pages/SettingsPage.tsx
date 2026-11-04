import { useCallback, useEffect, useState } from 'react'
import { apiError, getChannels, getSettings, putSettings, type AppSettings, type ChannelsOverview } from '../api/client'

const RISK_CONFIRM_TEXT = '我知晓并承担实盘交易风险'

/** 数值型可编辑字段（含新增热参与韧性参数）。 */
const NUM_FIELDS: { key: keyof AppSettings; label: string; hint?: string }[] = [
  { key: 'commission_rate', label: '佣金率（如 0.00025）' },
  { key: 'min_commission', label: '最低佣金（元）' },
  { key: 'stamp_tax_sell', label: '印花税率（卖出）' },
  { key: 'transfer_fee', label: '过户费率（双边）' },
  { key: 'akshare_min_interval', label: 'AKShare 最小间隔（秒）' },
  { key: 'tushare_min_interval', label: 'Tushare 最小间隔（秒）' },
  { key: 'max_position_value', label: '单票市值上限（元）', hint: 'RiskGate 每单动态读取' },
  { key: 'max_daily_orders', label: '日委托数上限（单）', hint: '正整数' },
  { key: 'watchdog_interval_s', label: '通道探活间隔（秒）', hint: '断线重连与探活节奏' },
  { key: 'reconcile_interval_s', label: '对账间隔（秒）', hint: '实盘全量核对周期' },
]

const STR_FIELDS: { key: keyof AppSettings; label: string; hint?: string }[] = [
  { key: 'tdx_install_dir', label: '通达信安装目录（T0002 所在）', hint: '板块导出「写入客户端目录」需要' },
  { key: 'ths_install_dir', label: '同花顺安装目录', hint: '预留；easytrader 凭据另见通道页保险库' },
]

/** 通道路由优先级：改这里就等于改「与哪个软件结合」，服务端按注册表校验名字。 */
const ROUTE_FIELDS: { key: keyof AppSettings; label: string; hint?: string }[] = [
  { key: 'feed_priority_history', label: '历史K线优先级', hint: '逗号分隔注册名，按顺序降级；未装 SDK 的源可以先写上' },
  { key: 'feed_priority_realtime', label: '实时主源（取第一个可用者）', hint: '断线不换源（宁停勿错），这里只决定谁是主源' },
  { key: 'feed_priority_financial', label: '财务数据优先级', hint: '逗号分隔，按顺序降级' },
]

/** 影响清单：切实盘前明示（D19）。 */
function ImpactList() {
  return (
    <ul className="impact-list">
      <li>新订单不再路由内置模拟撮合，改为真实券商通道（须已配置并在线）</li>
      <li>实盘下单强制服务端二次确认：每单弹窗 10 秒内人工确认，超时作废</li>
      <li>通道断线一律 fail-closed 拒单（不排队、不重试）</li>
      <li>全部委托继续受 A 股规则校验（T+1 / 整手 / 涨跌停价 / 仓位 / 资金）</li>
      <li>程序化交易须已完成报备（见「报备清单」页）</li>
    </ul>
  )
}

/** 后端静态托管提示：桌面壳与浏览器直连都走 8787，前端产物由后端伺服。 */
function HostingHint() {
  const port = window.location.port
  const served = port === '8787'
  return (
    <div className="card">
      <h3>运行形态</h3>
      <p className="sub-note">
        {served
          ? '当前由后端静态托管前端产物（同源 127.0.0.1:8787，SPA 路由 /channels、/interop 可直连访问）。'
          : `当前端口 ${port || '80'} 非后端端口：应是 Vite 开发/预览模式，API 与事件流经代理转发到 127.0.0.1:8787。`}
      </p>
      <p className="sub-note">
        后端只绑 127.0.0.1，全部数据本地存储；绑定地址不提供配置项。
      </p>
    </div>
  )
}

export default function SettingsPage() {
  const [s, setS] = useState<AppSettings | null>(null)
  const [ov, setOv] = useState<ChannelsOverview | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [confirmText, setConfirmText] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const refresh = useCallback(() => {
    getSettings()
      .then((next) => {
        setS(next)
        setDraft((cur) => {
          const d: Record<string, string> = { ...cur }
          for (const f of [...NUM_FIELDS, ...STR_FIELDS, ...ROUTE_FIELDS]) d[f.key] = String(next[f.key] ?? '')
          return d
        })
      })
      .catch((e: unknown) => setErr(apiError(e)))
    getChannels()
      .then(setOv)
      .catch(() => setOv(null))
  }, [])

  useEffect(refresh, [refresh])

  // 服务端校验失败（如缺确认文本）时原样回显 400 的 detail，前端不做风险判定
  const save = async (patch: Partial<AppSettings>, note: string) => {
    setErr('')
    setMsg('')
    try {
      const next = await putSettings(patch, confirmText || undefined)
      setS(next)
      setDraft((cur) => {
        const d: Record<string, string> = { ...cur }
        for (const k of Object.keys(patch)) {
          const v = next[k as keyof AppSettings]
          d[k] = String(v ?? '')
        }
        return d
      })
      setConfirmText('')
      setMsg(note)
      getChannels().then(setOv).catch(() => undefined)
    } catch (e) {
      setErr(apiError(e))
    }
  }

  const commitNum = (key: keyof AppSettings, note: string) => {
    const raw = draft[key]
    const v = Number(raw)
    if (!Number.isFinite(v) || v < 0) {
      setErr(`${key} 需为非负数字，未提交`)
      return
    }
    save({ [key]: key === 'max_daily_orders' ? Math.trunc(v) : v } as Partial<AppSettings>, note)
  }

  if (!s) {
    return (
      <section>
        <h2>设置</h2>
        <p className="placeholder">加载中…</p>
        {err && <p className="badge badge-down">{err}</p>}
      </section>
    )
  }

  const wantLive = !s.simulation_only
  const brokers = (ov?.channels ?? []).filter((c) => c.kind === 'broker' && c.status !== 'disabled')

  return (
    <section>
      <h2>设置</h2>

      <HostingHint />

      <div className="card">
        <h3>交易模式</h3>
        <p>
          当前：
          <b className={wantLive ? 'badge badge-live' : 'badge badge-paper'}>
            {wantLive ? '实盘模式（真实资金）' : '模拟盘模式'}
          </b>
          {' '}通道 {s.broker_channel}
          {ov?.live_error ? <span className="dialog-red">（{ov.live_error}）</span> : null}
        </p>
        {!wantLive ? (
          <>
            <p className="dialog-red">切换到实盘前请确认：</p>
            <ImpactList />
            <label className="confirm-line">
              输入确认文本（{RISK_CONFIRM_TEXT}）：
              <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
            </label>
            <button
              className="danger-btn"
              onClick={() => save({ simulation_only: false }, '已切换到实盘模式（每单强制二次确认）')}
            >
              切换到实盘模式
            </button>
            <p className="sub-note">
              未输入确认文本时服务端返回 400，原因显示在下方；是否放行由服务端判定。
            </p>
          </>
        ) : (
          <button onClick={() => save({ simulation_only: true }, '已切回模拟盘模式')}>
            切回模拟盘模式（推荐日常使用）
          </button>
        )}
      </div>

      <div className="card">
        <h3>交易通道</h3>
        <label>
          通道（只列交易 API 可见的通道 + 内置模拟撮合）
          <select value={s.broker_channel} onChange={(e) => save({ broker_channel: e.target.value }, '通道已保存')}>
            {!brokers.some((b) => b.name === 'paper') && <option value="paper">paper（内置模拟撮合）</option>}
            {brokers.length === 0 && <option value={s.broker_channel}>{s.broker_channel}</option>}
            {brokers.map((b) => (
              <option key={b.name} value={b.name}>
                {b.label}
                {b.official ? '' : '（非官方接口，稳定性低）'}
              </option>
            ))}
          </select>
        </label>
        {s.broker_channel !== 'paper' && (
          <p className="sub-note">选择非 paper 通道同样需服务端确认文本，可在上方输入后提交。</p>
        )}
        {s.broker_channel === 'easytrader' && (
          <p className="dialog-red">非官方接口，稳定性低；默认禁用，操作期间勿动鼠标键盘。</p>
        )}
        {s.broker_channel !== 'paper' && !wantLive && (
          <p className="placeholder">当前为模拟盘模式：任何下单仍路由内置模拟撮合。</p>
        )}
      </div>

      <div className="card">
        <h3>非官方通道启用位</h3>
        <p className="sub-note">
          easytrader 默认禁用（红字标注：非官方接口，稳定性低）。打开需确认文本，服务端强制校验。
        </p>
        {s.enable_easytrader ? (
          <button onClick={() => save({ enable_easytrader: false }, '已禁用 easytrader')}>
            禁用 easytrader 通道
          </button>
        ) : (
          <button className="danger-btn" onClick={() => save({ enable_easytrader: true }, '已启用 easytrader')}>
            启用 easytrader（非官方）
          </button>
        )}
      </div>

      <div className="card">
        <h3>费率与限速参数</h3>
        <p className="sub-note">限速为数据源反爬保护；费率以券商实际为准。</p>
        {NUM_FIELDS.map((f) => (
          <label key={f.key}>
            {f.label}
            {f.hint && <span className="sub-note">{f.hint}</span>}
            <input
              value={draft[f.key] ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
              onBlur={() => commitNum(f.key, `${f.label} 已保存`)}
            />
          </label>
        ))}
      </div>

      <div className="card">
        <h3>软件目录（互操作写入用）</h3>
        {STR_FIELDS.map((f) => (
          <label key={f.key}>
            {f.label}
            {f.hint && <span className="sub-note">{f.hint}</span>}
            <input
              value={draft[f.key] ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
              onBlur={() => save({ [f.key]: draft[f.key] ?? '' } as Partial<AppSettings>, `${f.label} 已保存`)}
            />
          </label>
        ))}
      </div>

      <div className="card">
        <h3>回测执行方式</h3>
        <p className="sub-note">开启后回测入队后台执行并在回测页显示进度；关闭则同步等待结果。</p>
        <label className="confirm-line">
          异步回测（backtest_async）
          <input type="checkbox" checked={s.backtest_async} onChange={(e) => save({ backtest_async: e.target.checked }, '回测执行方式已保存')} />
        </label>
      </div>

      <div className="card">
        <h3>行情通道路由优先级</h3>
        <p className="sub-note">
          当前生效：历史链 {ov?.history_chain.join(' → ') || '（无可用源）'} ·
          财务链 {ov?.financial_chain.join(' → ') || '（无）'} ·
          实时主源 {ov?.realtime_active || ov?.realtime_chain.join('、') || '（无）'}
        </p>
        {ROUTE_FIELDS.map((f) => (
          <label key={f.key}>
            {f.label}
            {f.hint && <span className="sub-note">{f.hint}</span>}
            <input
              value={draft[f.key] ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
              onBlur={() => save({ [f.key]: draft[f.key] ?? '' } as Partial<AppSettings>, `${f.label} 已保存`)}
            />
          </label>
        ))}
        <p className="sub-note">
          名字必须是注册表里真实存在的行情通道（写错服务端返回 400 并列出可选值）；
          就绪状态与安装引导见「通道」页。实时链只决定谁是主源——主源断线时不切换到第二源，
          避免两条口径拼出一条失真的时间轴。
        </p>
      </div>

      {msg && <p className="badge badge-paper">{msg}</p>}
      {err && <p className="badge badge-down">{err}</p>}
    </section>
  )
}
