import { useEffect, useState } from 'react'
import {
  complianceExportUrl,
  getCompliance,
  putCompliance,
  type ComplianceItem,
} from '../api/client'

/** 程序化交易报备清单（D28）——常驻导航入口（宪法第 9 条），按监管要点预置模板。 */
export default function CompliancePage() {
  const [items, setItems] = useState<ComplianceItem[]>([])
  const [err, setErr] = useState('')

  useEffect(() => {
    getCompliance().then(setItems).catch((e: unknown) => setErr(String(e)))
  }, [])

  const toggle = async (it: ComplianceItem) => {
    try {
      setItems(await putCompliance(it.item, it.category, !it.checked, it.note))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  const done = items.filter((i) => i.checked).length

  return (
    <section>
      <h2>程序化交易报备清单</h2>
      <p className="placeholder">
        依据证监会《证券市场程序化交易管理规定（试行）》要点预置。
        本页为常驻入口；开展实盘程序化交易前须向券商完成报备。
      </p>
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
