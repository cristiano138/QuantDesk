import { useEffect, useState } from 'react'
import {
  apiError,
  complianceExportUrl,
  getCompliance,
  putCompliance,
  type ComplianceItem,
} from '../api/client'
import HelpLink from '../components/HelpLink'

/** 程序化交易报备清单（D28）——常驻导航入口（宪法第 9 条），按监管要点预置模板。 */
export default function CompliancePage() {
  const [items, setItems] = useState<ComplianceItem[]>([])
  const [err, setErr] = useState('')
  const [loadErr, setLoadErr] = useState('')
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    setLoadErr('')
    getCompliance().then(setItems).catch((e: unknown) => setLoadErr(apiError(e)))
  }, [retry])

  const toggle = async (it: ComplianceItem) => {
    setErr('')
    try {
      setItems(await putCompliance(it.item, it.category, !it.checked, it.note))
    } catch (e) {
      setErr(apiError(e)) // 勾选状态没落库要当场说，不能看起来勾上了
    }
  }

  const done = items.filter((i) => i.checked).length

  return (
    <section>
      <h2>程序化交易报备清单<HelpLink slug="compliance" /></h2>
      <p className="placeholder">
        依据证监会《证券市场程序化交易管理规定（试行）》要点预置。
        本页为常驻入口；开展实盘程序化交易前须向券商完成报备。
      </p>
      {loadErr && (
        <div className="alert-banner">
          <p>
            <b>清单读取失败：</b>
            {loadErr}（读取失败时这里不会显示成「没有清单项」）
          </p>
          <p>
            <button className="mini" onClick={() => setRetry((v) => v + 1)}>
              重试
            </button>
          </p>
        </div>
      )}
      <div className="card">
        <h3>
          报备进度：{done}/{items.length}
          <button
            className="mini"
            style={{ marginLeft: 12 }}
            onClick={() => window.open(complianceExportUrl(), '_blank')}
          >
            导出文本
          </button>
        </h3>
        {err && <p className="badge badge-down">{err}</p>}
        <table>
          <thead>
            <tr>
              <th>状态</th>
              <th>类别</th>
              <th>事项</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.item}>
                <td>
                  <input type="checkbox" checked={it.checked} onChange={() => toggle(it)} />
                </td>
                <td>{it.category}</td>
                <td>{it.item}</td>
                <td>
                  {it.updated_at ? new Date(it.updated_at).toLocaleString('zh-CN') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
