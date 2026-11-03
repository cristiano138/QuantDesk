import { useEffect, useState } from 'react'

export interface ConfirmRequest {
  order_id: string
  symbol: string
  side: string
  price: number
  volume: number
  strategy_id: string
}

interface Props {
  request: ConfirmRequest | null
  ttlSeconds?: number
  onDone: (orderId: string, decision: 'confirm' | 'reject' | 'expired') => void
}

/** 实盘二次确认弹窗（D17）：10s 倒计时；通道红字标注（宪法：前端只是表现层）。 */
export default function ConfirmDialog({ request, ttlSeconds = 10, onDone }: Props) {
  const [left, setLeft] = useState(ttlSeconds)

  useEffect(() => {
    if (!request) return
    setLeft(ttlSeconds)
    const t = setInterval(() => {
      setLeft((v) => {
        if (v <= 1) {
          clearInterval(t)
          onDone(request.order_id, 'expired')
          return 0
        }
        return v - 1
      })
    }, 1000)
    return () => clearInterval(t)
  }, [request, ttlSeconds, onDone])

  if (!request) return null
  const buy = request.side === 'buy'

  return (
    <div className="dialog-mask">
      <div className="card dialog confirm-dialog">
        <h3>实盘下单二次确认</h3>
        <p className="dialog-red">非模拟通道：真实资金委托，请逐单核对！</p>
        <table>
          <tbody>
            <tr>
              <th>策略</th>
              <td>{request.strategy_id}</td>
            </tr>
            <tr>
              <th>代码</th>
              <td>{request.symbol}</td>
            </tr>
            <tr>
              <th>方向</th>
              <td className={buy ? 'up' : 'down'}>{buy ? '买入' : '卖出'}</td>
            </tr>
            <tr>
              <th>价格</th>
              <td>{request.price.toFixed(2)}</td>
            </tr>
            <tr>
              <th>数量</th>
              <td>{request.volume}</td>
            </tr>
            <tr>
              <th>金额</th>
              <td>{(request.price * request.volume).toFixed(2)}</td>
            </tr>
          </tbody>
        </table>
        <p className="dialog-count">{left}s 后自动作废</p>
        <div className="dialog-actions">
          <button className="danger" onClick={() => onDone(request.order_id, 'reject')}>
            拒绝
          </button>
          <button className="primary" onClick={() => onDone(request.order_id, 'confirm')}>
            确认下单
          </button>
        </div>
      </div>
    </div>
  )
}
