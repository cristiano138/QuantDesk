import { useEffect, useState } from 'react'
import { getSettings, putSettings, type AppSettings } from '../api/client'

const RISK_CONFIRM_TEXT = '我知晓并承担实盘交易风险'

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

export default function SettingsPage() {
  const [s, setS] = useState<AppSettings | null>(null)
  const [confirmText, setConfirmText] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    getSettings().then(setS).catch((e: unknown) => setErr(String(e)))
  }, [])

  const save = async (patch: Partial<AppSettings>, note: string) => {
    setErr('')
    setMsg('')
    try {
      setS(await putSettings(patch))
      setMsg(note)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
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

  return (
    <section>
      <h2>设置</h2>

      <div className="card">
        <h3>交易模式</h3>
        <p>
          当前：<b className={wantLive ? 'badge badge-live' : 'badge badge-paper'}>
            {wantLive ? '实盘模式（真实资金）' : '模拟盘模式'}
          </b>
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
              disabled={confirmText !== RISK_CONFIRM_TEXT}
              onClick={() =>
                save({ simulation_only: false }, '已切换到实盘模式（每单强制二次确认）')}
            >
              切换到实盘模式
            </button>
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
          通道
          <select
            value={s.broker_channel}
            onChange={(e) => save({ broker_channel: e.target.value }, '通道已保存')}
          >
            <option value="paper">paper（内置模拟撮合）</option>
            <option value="qmt">qmt（miniQMT，需权限）</option>
            <option value="gm">gm（掘金，备选）</option>
            <option value="easytrader">easytrader（非官方接口，稳定性低）</option>
          </select>
        </label>
        {s.broker_channel === 'easytrader' && (
          <p className="dialog-red">非官方接口，稳定性低；默认禁用，操作期间勿动鼠标键盘。</p>
        )}
        {s.broker_channel !== 'paper' && !wantLive && (
          <p className="placeholder">当前为模拟盘模式：任何下单仍路由内置模拟撮合。</p>
        )}
      </div>

      <div className="card">
        <h3>费率参数（以券商实际为准）</h3>
        {(
          [
            ['commission_rate', '佣金率（如 0.00025）'],
            ['min_commission', '最低佣金（元）'],
            ['stamp_tax_sell', '印花税率（卖出）'],
            ['transfer_fee', '过户费率（双边）'],
          ] as const
        ).map(([k, label]) => (
          <label key={k}>
            {label}
            <input
              value={String(s[k])}
              onChange={(e) => setS({ ...s, [k]: e.target.value } as AppSettings)}
              onBlur={(e) => {
                const v = Number(e.target.value)
                if (Number.isFinite(v) && v >= 0) save({ [k]: v }, '费率已保存')
              }}
            />
          </label>
        ))}
      </div>

      <div className="card">
        <h3>限速参数（数据源反爬保护）</h3>
        {(
          [
            ['akshare_min_interval', 'AKShare 最小间隔（秒）'],
            ['tushare_min_interval', 'Tushare 最小间隔（秒）'],
          ] as const
        ).map(([k, label]) => (
          <label key={k}>
            {label}
            <input
              value={String(s[k])}
              onChange={(e) => setS({ ...s, [k]: e.target.value } as AppSettings)}
              onBlur={(e) => {
                const v = Number(e.target.value)
                if (Number.isFinite(v) && v >= 0) save({ [k]: v }, '限速已保存（新连接生效）')
              }}
            />
          </label>
        ))}
      </div>

      {msg && <p className="badge badge-paper">{msg}</p>}
      {err && <p className="badge badge-down">{err}</p>}
    </section>
  )
}
