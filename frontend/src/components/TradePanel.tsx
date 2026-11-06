import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  apiError,
  getAccount,
  getOrderRules,
  placeOrder,
  type OrderRules,
} from '../api/client'
import HelpLink from './HelpLink'

/**
 * 模拟买卖面板（宪法第 2 条：这里只做录入与回显，能不能成交由服务端 RiskGate 裁决）。
 *
 * 界面上出现的每个数字都来自后端同一份规则（/api/trading/rules），前端不自己算
 * 涨跌停、不自己判整手——历史上文案与执行各算一套，用户按界面填的单会被一条看不见的
 * 规则退回，那种「点了没反应」最难查。
 */

const CCY_LABEL: Record<string, string> = { CNY: '元', HKD: '港币', USD: '美元' }
const STATUS_CN: Record<string, string> = {
  filled: '已成交',
  part_filled: '部分成交',
  submitted: '已提交，等待成交',
  rejected: '被拒绝',
  failed: '提交失败',
  pending_confirm: '等待你二次确认',
  cancelled: '已撤销',
  expired_confirm: '确认超时作废',
}

function money(v: number | null | undefined, ccy: string): string {
  if (v == null || !isFinite(v)) return '—'
  return `${v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${CCY_LABEL[ccy] ?? '元'}`
}

export default function TradePanel({ symbol, compact = false }: { symbol: string; compact?: boolean }) {
  const [rules, setRules] = useState<OrderRules | null>(null)
  const [rulesErr, setRulesErr] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [volume, setVolume] = useState<number>(100)
  const [price, setPrice] = useState<string>('')
  const [market, setMarket] = useState(false)
  const [cash, setCash] = useState<number | null>(null)
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState<{ status: string; message: string; avg: number; vol: number } | null>(null)

  const load = useCallback(() => {
    setRulesErr('')
    getOrderRules(symbol)
      .then((r) => {
        setRules(r)
        const mv = Math.max(Number(r.min_volume) || 100, 1)
        setVolume((v) => (v > 0 ? v : mv))
        if (r.last) setPrice((p) => p || String(r.last))
      })
      .catch((e) => setRulesErr(apiError(e)))
    getAccount()
      .then((a) => setCash(a.cash))
      .catch(() => setCash(null))
  }, [symbol])

  useEffect(() => {
    setRules(null)
    setResult(null)
    setPrice('')
    load()
  }, [load])

  const step = Math.max(Number(rules?.lot_step) || 1, 1)
  const minVolume = Math.max(Number(rules?.min_volume) || step, 1)
  const ccy = rules?.currency ?? 'CNY'
  const qty = Math.max(Math.floor(Number(volume) || 0), 0)
  const px = Number(price)
  const need = useMemo(() => {
    const unit = market ? (rules?.last ?? 0) : px
    if (!unit || !qty) return 0
    return unit * qty + (unit * qty) * 0.00035 + 5   // 粗算占位，准确以服务端回执为准
  }, [market, px, qty, rules])

  const submit = async () => {
    if (!qty) {
      setResult({ status: 'rejected', message: '数量要填大于 0 的股数', avg: 0, vol: 0 })
      return
    }
    setSending(true)
    setResult(null)
    try {
      const o = await placeOrder({
        symbol,
        side,
        volume: qty,
        price: market ? null : px > 0 ? px : null,
        order_type: market ? 'market' : 'limit',
      })
      setResult({
        status: o.status,
        message: o.message || '',
        avg: o.avg_filled_price,
        vol: o.filled_volume,
      })
      getAccount().then((a) => setCash(a.cash)).catch(() => undefined)
    } catch (e) {
      setResult({ status: 'failed', message: apiError(e), avg: 0, vol: 0 })
    } finally {
      setSending(false)
    }
  }

  if (rulesErr)
    return (
      <div className="card trade-panel">
        <p className="badge badge-down">
          {rulesErr}
          <button className="mini" style={{ marginLeft: 8 }} onClick={load}>重试</button>
        </p>
      </div>
    )

  return (
    <div className="card trade-panel">
      <h3>
        模拟买卖
        <span className="sub-note" style={{ marginLeft: 8 }}>
          {rules?.simulation_only === false ? '实盘模式：每笔都要二次确认' : '本机虚拟资金，不接任何券商'}
        </span>
      </h3>

      <div className="trade-side">
        <button className={side === 'buy' ? 'active buy' : ''} onClick={() => setSide('buy')}>买入</button>
        <button className={side === 'sell' ? 'active sell' : ''} onClick={() => setSide('sell')}>卖出</button>
        <span className="sub-note">{symbol}{rules?.name ? ` ${rules.name}` : ''}
          {rules?.board_cn ? ` · ${rules.board_cn}` : ''}</span>
      </div>

      <label className="trade-row">
        <span>数量（股）</span>
        <span className="trade-qty">
          <button className="mini" onClick={() => setVolume(Math.max(qty - step, 0))}>−</button>
          <input
            type="number"
            min={minVolume}
            step={step}
            value={volume}
            onChange={(e) => setVolume(Number(e.target.value))}
          />
          <button className="mini" onClick={() => setVolume(qty + step)}>＋</button>
          <button className="mini" onClick={() => setVolume(minVolume)}>最少 {minVolume}</button>
        </span>
      </label>
      <p className="sub-note">{rules?.lot_rule ?? ''}{rules?.t_plus_1 ? '；今天买的股票明天才能卖（T+1）' : '；当天买入当天即可卖出'}</p>

      <label className="trade-row">
        <span>价格</span>
        <span className="trade-price">
          <input
            type="number"
            step="0.01"
            disabled={market}
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
          <label className="trade-market">
            <input type="checkbox" checked={market} onChange={(e) => setMarket(e.target.checked)} />
            市价（按对方挂单价）
          </label>
        </span>
      </label>
      <p className="sub-note">
        可委托区间 {rules?.limit_down != null ? rules.limit_down.toFixed(2) : '不限'} ~{' '}
        {rules?.limit_up != null ? rules.limit_up.toFixed(2) : '不限'}
        {rules?.band_basis ? `（${rules.band_basis}）` : ''}
      </p>

      <div className="trade-foot">
        <span>{side === 'buy' ? '大约需要' : '大约可得'} {money(need, ccy)}</span>
        <span>可用资金 {cash == null ? '—' : money(cash, ccy)}</span>
        <button className="primary" disabled={sending} onClick={submit}>
          {sending ? '提交中…' : `${side === 'buy' ? '买入' : '卖出'} ${qty} 股`}
        </button>
      </div>

      {rules && rules.ok === false && (
        <p className="badge badge-down">{rules.reason || rules.error || '当前不可交易'}</p>
      )}
      {result && (
        <p className={result.status === 'filled' || result.status === 'part_filled' ? 'badge badge-up' : 'badge badge-down'}>
          {STATUS_CN[result.status] ?? result.status}
          {result.vol ? `　成交 ${result.vol} 股 @ ${result.avg.toFixed(2)}` : ''}
          {result.message ? `　${result.message}` : ''}
        </p>
      )}
      {!compact && (
        <p className="sub-note">
          这里的委托不会发往任何券商。要真的下单，需在
          <HelpLink slug="channels" label="行情与交易" /> 页接入券商官方通道，
          并且每一笔都要二次确认。
        </p>
      )}
      <button className="mini" onClick={load}>刷新规则</button>
    </div>
  )
}
