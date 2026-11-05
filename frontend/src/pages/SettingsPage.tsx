import { useCallback, useEffect, useState } from 'react'
import { apiError, getChannels, getSettings, putSettings, type AppSettings, type ChannelsOverview } from '../api/client'
import HelpLink from '../components/HelpLink'

const RISK_CONFIRM_TEXT = '我知晓并承担实盘交易风险'

/** 数值型可编辑字段。 */
const NUM_FIELDS: { key: keyof AppSettings; label: string; hint?: string }[] = [
  { key: 'commission_rate', label: '佣金率（如 0.00025）' },
  { key: 'min_commission', label: '最低佣金（元）' },
  { key: 'stamp_tax_sell', label: '印花税率（卖出）' },
  { key: 'transfer_fee', label: '过户费率（双边）' },
  { key: 'akshare_min_interval', label: '免费行情取数间隔（秒）', hint: '间隔太小容易被行情来源限制' },
  { key: 'tushare_min_interval', label: '付费行情取数间隔（秒）', hint: '间隔太小容易被行情来源限制' },
  { key: 'max_position_value', label: '单票市值上限（元）', hint: '每次下单前自动检查' },
  { key: 'max_daily_orders', label: '日委托数上限（单）', hint: '正整数' },
  { key: 'watchdog_interval_s', label: '连接检查间隔（秒）', hint: '交易软件断线后的检查与重连节奏' },
  { key: 'reconcile_interval_s', label: '持仓对账间隔（秒）', hint: '实盘账户与本地记录核对周期' },
  { key: 'log_retention_days', label: '运行记录保留期（天）',
    hint: '只清理运行记录与「本轮无差异」记录；委托、成交、持仓、回测明细不会自动删除' },
]

const STR_FIELDS: { key: keyof AppSettings; label: string; hint?: string }[] = [
  { key: 'tdx_install_dir', label: '通达信安装目录（T0002 所在）', hint: '把板块写入通达信时需要' },
  { key: 'ths_install_dir', label: '同花顺安装目录', hint: '同花顺下单程序所在目录' },
]

/** 行情来源顺序：决定优先用哪个软件的数据。 */
const ROUTE_FIELDS: { key: keyof AppSettings; label: string; hint?: string }[] = [
  { key: 'feed_priority_history', label: '历史 K 线来源顺序', hint: '按顺序取用，前一个不可用再用下一个；未安装的软件也可以先写上' },
  { key: 'feed_priority_realtime', label: '实时行情主来源', hint: '只使用第一个可用的来源；断线时不切换，避免价格口径混用' },
  { key: 'feed_priority_financial', label: '财务数据来源顺序', hint: '按顺序取用' },
]

/** 影响清单：切实盘前明示。 */
function ImpactList() {
  return (
    <ul className="impact-list">
      <li>新订单改为发送到真实券商账户（须已登录并连接）</li>
      <li>每笔实盘下单都要在弹窗中人工确认，10 秒内未确认自动作废</li>
      <li>交易软件断线时直接拒绝下单（不排队、不重试）</li>
      <li>全部委托继续受 A 股规则校验（T+1 / 整手 / 涨跌停价 / 仓位 / 资金）</li>
      <li>程序化交易须已完成报备（见「报备清单」页）</li>
    </ul>
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
        <h2>设置<HelpLink slug="settings" /></h2>
        <p className="placeholder">加载中…</p>
        {err && <p className="badge badge-down">{err}</p>}
      </section>
    )
  }

  const wantLive = !s.simulation_only
  const brokers = (ov?.channels ?? []).filter((c) => c.kind === 'broker' && c.status !== 'disabled')
  const brokerName = (name: string) =>
    name === 'paper' ? '内置模拟盘' : (brokers.find((b) => b.name === name)?.label ?? name)

  return (
    <section>
      <h2>设置<HelpLink slug="settings" /></h2>

      <div className="card">
        <h3>交易模式</h3>
        <p>
          当前：
          <b className={wantLive ? 'badge badge-live' : 'badge badge-paper'}>
            {wantLive ? '实盘模式（真实资金）' : '模拟盘模式'}
          </b>
          {' '}账户 {brokerName(s.broker_channel)}
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
              未输入确认文本不会生效。
            </p>
          </>
        ) : (
          <button onClick={() => save({ simulation_only: true }, '已切回模拟盘模式')}>
            切回模拟盘模式（推荐日常使用）
          </button>
        )}
      </div>

      <div className="card">
        <h3>券商账户</h3>
        <label>
          下单使用的账户
          <select value={s.broker_channel} onChange={(e) => save({ broker_channel: e.target.value }, '券商账户已保存')}>
            {!brokers.some((b) => b.name === 'paper') && <option value="paper">内置模拟盘（模拟撮合）</option>}
            {brokers.length === 0 && <option value={s.broker_channel}>{brokerName(s.broker_channel)}</option>}
            {brokers.map((b) => (
              <option key={b.name} value={b.name}>
                {b.label}
                {b.official ? '' : '（非官方软件，稳定性低）'}
              </option>
            ))}
          </select>
        </label>
        {s.broker_channel !== 'paper' && (
          <p className="sub-note">改用券商账户同样需要在上方输入确认文本。</p>
        )}
        {s.broker_channel === 'easytrader' && (
          <p className="dialog-red">非官方软件，稳定性低；默认关闭，操作期间勿动鼠标键盘。</p>
        )}
        {s.broker_channel !== 'paper' && !wantLive && (
          <p className="placeholder">当前为模拟盘模式：任何下单仍由内置模拟撮合完成。</p>
        )}
      </div>

      <div className="card">
        <h3>同花顺客户端下单（非官方）</h3>
        <p className="sub-note">
          默认关闭。开启后通过同花顺下单程序发送委托，客户端升级后可能失效；操作期间不要移动鼠标键盘。
        </p>
        {s.enable_easytrader ? (
          <button onClick={() => save({ enable_easytrader: false }, '已关闭同花顺客户端下单')}>
            关闭同花顺客户端下单
          </button>
        ) : (
          <button className="danger-btn" onClick={() => save({ enable_easytrader: true }, '已开启同花顺客户端下单')}>
            开启同花顺客户端下单（非官方）
          </button>
        )}
      </div>

      <div className="card">
        <h3>费率与刷新间隔</h3>
        <p className="sub-note">刷新间隔用于避免请求过密；费率以券商实际收取为准。</p>
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
        <p className="sub-note">开启后回测在后台运行并在回测页显示进度；关闭则等待结果。</p>
        <label className="confirm-line">
          后台执行回测
          <input type="checkbox" checked={s.backtest_async} onChange={(e) => save({ backtest_async: e.target.checked }, '回测执行方式已保存')} />
        </label>
      </div>

      <div className="card">
        <h3>行情来源顺序</h3>
        <p className="sub-note">
          当前使用：历史 K 线 {ov?.history_chain.join(' → ') || '（暂无可用来源）'} ·
          财务 {ov?.financial_chain.join(' → ') || '（无）'} ·
          实时 {ov?.realtime_active || ov?.realtime_chain.join('、') || '（无）'}
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
          填写「行情与交易」页里显示的来源名称（如 免费公开行情、腾讯行情、miniQMT 行情）。未安装的软件可以先填，安装后自动生效；
          实时行情只使用第一个可用来源，避免不同来源的价格混在一起。
        </p>
      </div>

      {msg && <p className="badge badge-paper">{msg}</p>}
      {err && <p className="badge badge-down">{err}</p>}
    </section>
  )
}
