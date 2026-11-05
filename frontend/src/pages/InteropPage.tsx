import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  apiError,
  compileFormula,
  downloadBlock,
  exportBlock,
  getInteropTargets,
  getSettings,
  getWatchlist,
  importBlock,
  scanSignals,
  type BlockExportResult,
  type BlockDownload,
  type BlockTargetSpec,
  type FormulaResult,
  type InteropTargets,
  type SignalScanResult,
  type WatchItem,
} from '../api/client'
import HelpLink from '../components/HelpLink'

/** 各规则 kinds 的参数字段（与后端 services/interop.py 的 normalize 对齐）。 */
type FieldDef = { key: string; label: string; def: string; options?: string[] }

const RULE_FIELDS: Record<string, FieldDef[]> = {
  ma_cross: [
    { key: 'fast', label: '快线（MA 天数）', def: '5' },
    { key: 'slow', label: '慢线（MA 天数）', def: '20' },
    { key: 'direction', label: '方向', def: 'golden', options: ['golden', 'death'] },
  ],
  macd_golden: [
    { key: 'fast', label: '快线 EMA', def: '12' },
    { key: 'slow', label: '慢线 EMA', def: '26' },
    { key: 'signal', label: '信号线 DEA', def: '9' },
  ],
  rsi_threshold: [
    { key: 'period', label: '周期', def: '14' },
    {
      key: 'op',
      label: '判定',
      def: 'cross_above',
      options: ['above', 'below', 'cross_above', 'cross_below'],
    },
    { key: 'value', label: '阈值', def: '30' },
  ],
  volume_spike: [
    { key: 'ma_days', label: '均量天数', def: '5' },
    { key: 'multiple', label: '放量倍数（>1）', def: '2' },
  ],
}

const OP_CN: Record<string, string> = {
  above: '高于',
  below: '低于',
  cross_above: '上穿',
  cross_below: '下穿',
  golden: '金叉',
  death: '死叉',
}

const DIALECT_CN: Record<string, string> = { tdx: '通达信', ths: '同花顺' }

/** 标的来源选择：自选股勾选 + 手工粘贴，导出与信号扫描共用。 */
function SymbolPicker({
  watch,
  watchErr,
  picked,
  onPicked,
  manual,
  onManual,
}: {
  watch: WatchItem[]
  watchErr: string
  picked: string[]
  onPicked: (v: string[]) => void
  manual: string
  onManual: (v: string) => void
}) {
  const toggle = (sym: string) =>
    onPicked(picked.includes(sym) ? picked.filter((s) => s !== sym) : [...picked, sym])
  return (
    <>
      <label>
        自选股勾选（{picked.length}/{watch.length}）
        <div className="watch-pick">
          {watchErr && <span className="sub-note warn">自选股读取失败：{watchErr}</span>}
          {!watchErr && watch.length === 0 && (
            <span className="placeholder">自选股为空，可在行情面板添加或直接手工粘贴。</span>
          )}
          {watch.map((w) => (
            <label className="pick-item" key={w.symbol}>
              <input
                type="checkbox"
                checked={picked.includes(w.symbol)}
                onChange={() => toggle(w.symbol)}
              />
              <span>{w.symbol}</span>
              <span className="sub-note">{w.name}</span>
            </label>
          ))}
        </div>
      </label>
      <label>
        手工粘贴标的（逗号 / 空格 / 换行分隔）
        <textarea rows={3} value={manual} onChange={(e) => onManual(e.target.value)} placeholder="600000.SH 000001.SZ" />
      </label>
    </>
  )
}

export default function InteropPage() {
  const [targets, setTargets] = useState<InteropTargets | null>(null)
  const [watch, setWatch] = useState<WatchItem[]>([])
  const [watchErr, setWatchErr] = useState('')
  const [tdxDir, setTdxDir] = useState('')
  // 目录读不到 ≠ 未配置：前者是后端连不上，后者是用户真没填，两句提示不能写成一句
  const [tdxErr, setTdxErr] = useState('')
  const [pageErr, setPageErr] = useState('')

  // 导出
  const [blkName, setBlkName] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [manual, setManual] = useState('')
  const [chosenTargets, setChosenTargets] = useState<string[]>(['tdx'])
  const [writeToClient, setWriteToClient] = useState(false)
  const [bsePrefix, setBsePrefix] = useState('')
  const [exporting, setExporting] = useState(false)
  const [exportErr, setExportErr] = useState('')
  const [exportOut, setExportOut] = useState<BlockExportResult | null>(null)
  const [preview, setPreview] = useState<BlockDownload | null>(null)
  const exportRef = useRef<HTMLDivElement>(null)

  // 导入
  const [impTarget, setImpTarget] = useState('tdx')
  const [impContent, setImpContent] = useState('')
  const [impErr, setImpErr] = useState('')
  const [impOut, setImpOut] = useState<{ parsed: number; added: string[]; already_present: string[] } | null>(null)

  // 信号扫描
  const [kind, setKind] = useState('ma_cross')
  const [ruleParams, setRuleParams] = useState<Record<string, string>>({})
  const [scanning, setScanning] = useState(false)
  const [scanErr, setScanErr] = useState('')
  const [scanOut, setScanOut] = useState<SignalScanResult | null>(null)

  // 公式
  const [dialect, setDialect] = useState('tdx')
  const [formulaOut, setFormulaOut] = useState<FormulaResult | null>(null)
  const [formulaErr, setFormulaErr] = useState('')
  const [copyMsg, setCopyMsg] = useState('')

  const loadWatch = useCallback(() => {
    getWatchlist()
      .then((w) => {
        setWatch(w)
        setWatchErr('')
      })
      .catch((e: unknown) => setWatchErr(apiError(e)))
  }, [])

  const loadTdxDir = useCallback(() => {
    getSettings()
      .then((s) => {
        setTdxDir(s.tdx_install_dir)
        setTdxErr('')
      })
      .catch((e: unknown) => {
        setTdxErr(apiError(e))
        setTdxDir('')
      })
  }, [])

  useEffect(() => {
    getInteropTargets()
      .then(setTargets)
      .catch((e: unknown) => setPageErr(apiError(e)))
    loadWatch()
    loadTdxDir()
  }, [loadWatch, loadTdxDir])

  useEffect(() => {
    if (!targets || targets.rules.length === 0) return
    const k = targets.rules.some((r) => r.kind === kind) ? kind : targets.rules[0].kind
    if (k !== kind) setKind(k)
    const fields = RULE_FIELDS[k] ?? []
    setRuleParams((cur) => {
      const next: Record<string, string> = {}
      for (const f of fields) next[f.key] = cur[f.key] ?? f.def
      return next
    })
  }, [targets, kind])

  const symbolList = useMemo(() => {
    const pasted = manual.split(/[,，\s]+/).filter(Boolean)
    return Array.from(new Set([...picked, ...pasted]))
  }, [picked, manual])

  const refreshWatch = useCallback(() => loadWatch(), [loadWatch])

  // 「导出为板块」复用导出区块：把命中标的填进手工粘贴区并滚动过去
  const sendHitsToExport = () => {
    if (!scanOut || scanOut.hits.length === 0) return
    setManual(scanOut.hits.join(','))
    setPicked([])
    exportRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const doExport = async () => {
    setExportErr('')
    setExportOut(null)
    setPreview(null)
    if (!blkName.trim()) {
      setExportErr('请填写板块名（仅中英文、数字、下划线与短横）')
      return
    }
    if (symbolList.length === 0) {
      setExportErr('请勾选自选股或手工粘贴标的')
      return
    }
    setExporting(true)
    try {
      const out = await exportBlock({
        name: blkName.trim(),
        symbols: symbolList,
        targets: chosenTargets,
        write_to_client: writeToClient,
        bse_prefix: bsePrefix.trim(),
      })
      setExportOut(out)
    } catch (e) {
      setExportErr(apiError(e)) // 400 原文（如北交所市场位未确认）
    } finally {
      setExporting(false)
    }
  }

  const doPreview = async (name: string) => {
    setExportErr('')
    try {
      setPreview(await downloadBlock(name))
    } catch (e) {
      setExportErr(apiError(e))
    }
  }

  const saveFile = async (name: string) => {
    try {
      const d = await downloadBlock(name)
      const blob = new Blob([d.content], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setExportErr(apiError(e))
    }
  }

  const doImport = async () => {
    setImpErr('')
    setImpOut(null)
    if (!impContent.trim()) {
      setImpErr('请粘贴客户端板块文件内容')
      return
    }
    try {
      setImpOut(await importBlock(impTarget, impContent))
      refreshWatch()
    } catch (e) {
      setImpErr(apiError(e))
    }
  }

  const doScan = async () => {
    setScanErr('')
    setScanOut(null)
    if (symbolList.length === 0) {
      setScanErr('请勾选自选股或手工粘贴标的')
      return
    }
    setScanning(true)
    try {
      const typed: Record<string, string | number> = {}
      for (const f of RULE_FIELDS[kind] ?? []) {
        const raw = ruleParams[f.key] ?? f.def
        const num = Number(raw)
        typed[f.key] = f.options || !Number.isFinite(num) ? raw : num
      }
      setScanOut(await scanSignals({ kind, params: typed, symbols: symbolList }))
    } catch (e) {
      setScanErr(apiError(e))
    } finally {
      setScanning(false)
    }
  }

  const doFormula = async () => {
    setFormulaErr('')
    setFormulaOut(null)
    try {
      const typed: Record<string, string | number> = {}
      for (const f of RULE_FIELDS[kind] ?? []) {
        const raw = ruleParams[f.key] ?? f.def
        const num = Number(raw)
        typed[f.key] = f.options || !Number.isFinite(num) ? raw : num
      }
      setFormulaOut(await compileFormula(kind, typed, dialect))
    } catch (e) {
      setFormulaErr(apiError(e))
    }
  }

  const copyFormula = async () => {
    if (!formulaOut) return
    setCopyMsg('')
    try {
      await navigator.clipboard.writeText(formulaOut.formula)
      setCopyMsg('已复制到剪贴板')
    } catch {
      setCopyMsg('剪贴板不可用，请手动选中公式文本复制')
    }
  }

  if (!targets) {
    return (
      <section>
        <h2>软件互操作<HelpLink slug="interop" /></h2>
        <p className="placeholder">加载中…</p>
        {pageErr && <p className="badge badge-down">{pageErr}</p>}
      </section>
    )
  }

  const blockLabel = (id: string) => targets.blocks.find((b) => b.id === id)?.label ?? id

  return (
    <section>
      <h2>软件互操作<HelpLink slug="interop" /></h2>
      <p className="sub-note">导出目录：{targets.exports_dir}</p>

      <div className="card" ref={exportRef}>
        <h3>板块导出</h3>
        <label>
          板块名
          <input value={blkName} onChange={(e) => setBlkName(e.target.value)} placeholder="如 我的均线金叉" />
        </label>
        <SymbolPicker watch={watch} watchErr={watchErr} picked={picked} onPicked={setPicked} manual={manual} onManual={setManual} />
        <div className="form-grid">
          <div>
            <span className="metric-label">目标软件（可多选）</span>
            {targets.blocks.map((b: BlockTargetSpec) => (
              <label className="check-line" key={b.id}>
                <input
                  type="checkbox"
                  checked={chosenTargets.includes(b.id)}
                  onChange={() =>
                    setChosenTargets((cur) =>
                      cur.includes(b.id) ? cur.filter((x) => x !== b.id) : [...cur, b.id],
                    )
                  }
                />
                <span>
                  {b.label}
                  <span className="sub-note">
                    {b.ext} · {b.encoding} · {b.format}
                  </span>
                </span>
              </label>
            ))}
          </div>
          <div>
            <label className="check-line">
              <input
                type="checkbox"
                disabled={!chosenTargets.includes('tdx') || !tdxDir}
                checked={writeToClient && !!tdxDir}
                onChange={(e) => setWriteToClient(e.target.checked)}
              />
              <span>写入客户端目录（通达信 T0002\blocknew）</span>
            </label>
            {!tdxDir && tdxErr && (
              <p className="sub-note warn">
                通达信安装目录读不到：{tdxErr}
                <button className="mini" style={{ marginLeft: 6 }} onClick={loadTdxDir}>重试</button>
                <br />
                这不代表你没配置——配置很可能仍在设置页里，只是暂时读不到。
                连不上时也请先别改设置，避免覆盖掉原有目录。
              </p>
            )}
            {!tdxDir && !tdxErr && (
              <p className="sub-note warn">
                未填写通达信安装目录，无法直接写入客户端：请到「设置 → 软件目录」填 tdx_install_dir 后重试。
              </p>
            )}
            <label>
              北交所市场位（bse_prefix）
              <input
                value={bsePrefix}
                onChange={(e) => setBsePrefix(e.target.value)}
                placeholder="留空则北交所标的会被跳过"
              />
            </label>
            {!bsePrefix && (
              <p className="sub-note warn">
                北交所市场位各版本通达信口径不一致，留空时 .BJ 标的会被跳过并在结果中列出。
              </p>
            )}
          </div>
        </div>
        <button disabled={exporting} onClick={doExport}>
          {exporting ? '导出中…' : '导出板块文件'}
        </button>
        {exportErr && <p className="badge badge-down">{exportErr}</p>}
        {exportOut && (
          <div className="result-block">
            <p>
              共 {exportOut.count} 只标的，生成 {exportOut.files.length} 个文件
              {exportOut.skipped_bse.length > 0 && (
                <span className="warn">；北交所跳过 {exportOut.skipped_bse.length} 只：{exportOut.skipped_bse.join('、')}</span>
              )}
            </p>
            <table>
              <thead>
                <tr>
                  <th>目标</th>
                  <th>文件路径</th>
                  <th>字节</th>
                  <th>客户端路径</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {exportOut.files.map((f) => (
                  <tr key={`${f.target}-${f.name}`}>
                    <td>{blockLabel(f.target)}</td>
                    <td>{f.path}</td>
                    <td>{f.bytes}</td>
                    <td>{f.client_path || '—'}</td>
                    <td className="row-btns">
                      <button className="mini" onClick={() => doPreview(f.name)}>
                        预览
                      </button>
                      <button className="mini" onClick={() => saveFile(f.name)}>
                        下载
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="sub-note">{exportOut.note}</p>
            {preview && (
              <>
                <h4>
                  内容预览（{preview.name} · {preview.encoding} · {preview.bytes} 字节）
                </h4>
                <pre className="logs">{preview.content}</pre>
              </>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <h3>板块导入</h3>
        <label>
          来源格式
          <select value={impTarget} onChange={(e) => setImpTarget(e.target.value)}>
            {targets.blocks.map((b) => (
              <option key={b.id} value={b.id}>
                {b.label}（{b.format}）
              </option>
            ))}
          </select>
        </label>
        <label>
          粘贴板块内容
          <textarea rows={6} value={impContent} onChange={(e) => setImpContent(e.target.value)} placeholder={'1600000\n0000001'} />
        </label>
        <button onClick={doImport}>解析并导入自选股</button>
        {impErr && <p className="badge badge-down">{impErr}</p>}
        {impOut && (
          <div className="result-block">
            <p>
              解析 {impOut.parsed} 只 · 新增 {impOut.added.length} 只 · 已存在 {impOut.already_present.length} 只
            </p>
            {impOut.added.length > 0 && <p className="sub-note">新增：{impOut.added.join('、')}</p>}
            {impOut.already_present.length > 0 && (
              <p className="sub-note">已存在：{impOut.already_present.join('、')}</p>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <h3>信号选股（扫描本地数据）</h3>
        <label>
          规则类型
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {targets.rules.map((r) => (
              <option key={r.kind} value={r.kind}>
                {r.title}
              </option>
            ))}
          </select>
        </label>
        <div className="form-grid">
          {(RULE_FIELDS[kind] ?? []).map((f) => (
            <label key={f.key}>
              {f.label}
              {f.options ? (
                <select value={ruleParams[f.key] ?? f.def} onChange={(e) => setRuleParams((p) => ({ ...p, [f.key]: e.target.value }))}>
                  {f.options.map((o) => (
                    <option key={o} value={o}>
                      {OP_CN[o] ?? o}
                    </option>
                  ))}
                </select>
              ) : (
                <input value={ruleParams[f.key] ?? f.def} onChange={(e) => setRuleParams((p) => ({ ...p, [f.key]: e.target.value }))} />
              )}
            </label>
          ))}
        </div>
        <SymbolPicker watch={watch} watchErr={watchErr} picked={picked} onPicked={setPicked} manual={manual} onManual={setManual} />
        <div className="row-btns">
          <button disabled={scanning} onClick={doScan}>
            {scanning ? '扫描中…' : '扫描本地数据'}
          </button>
          <button className="mini" disabled={!scanOut || scanOut.hits.length === 0} onClick={sendHitsToExport}>
            命中结果导出为板块
          </button>
        </div>
        {scanErr && <p className="badge badge-down">{scanErr}</p>}
        {scanOut && (
          <div className="result-block">
            <p>
              规则：{scanOut.rule.title} · 有效扫描 {scanOut.scanned} 只 · 命中 {scanOut.hits.length} 只 ·
              本地无数据 {scanOut.no_local_data.length} 只
            </p>
            {scanOut.hits.length > 0 ? (
              <table>
                <thead>
                  <tr>
                    <th>标的</th>
                    <th>截至</th>
                    <th>判定上下文</th>
                  </tr>
                </thead>
                <tbody>
                  {scanOut.hits.map((s) => (
                    <tr key={s}>
                      <td>{s}</td>
                      <td>{scanOut.details[s]?.as_of ?? '—'}</td>
                      <td>{Object.entries(scanOut.details[s]?.context ?? {}).map(([k, v]) => `${k}=${String(v)}`).join(' · ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="placeholder">无命中标的。</p>
            )}
            {scanOut.no_local_data.length > 0 && (
              <p className="sub-note warn">
                以下标的本地无 K 线，未参与判定（请先到「数据管理」下载）：{scanOut.no_local_data.join('、')}
              </p>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <h3>选股公式导出</h3>
        <div className="form-grid">
          <label>
            公式方言
            <select value={dialect} onChange={(e) => setDialect(e.target.value)}>
              {targets.formula_dialects.map((d) => (
                <option key={d} value={d}>
                  {DIALECT_CN[d] ?? d}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="sub-note">参数沿用上方「信号选股」的规则与参数。</p>
        <div className="row-btns">
          <button onClick={doFormula}>生成公式</button>
          <button className="mini" disabled={!formulaOut} onClick={copyFormula}>
            复制到剪贴板
          </button>
        </div>
        {formulaErr && <p className="badge badge-down">{formulaErr}</p>}
        {copyMsg && <p className="badge badge-paper">{copyMsg}</p>}
        {formulaOut && (
          <div className="result-block">
            <p>
              {formulaOut.title} · {DIALECT_CN[formulaOut.dialect] ?? formulaOut.dialect}
            </p>
            <pre className="logs">{formulaOut.formula.replace(/\r/g, '')}</pre>
            <p className="sub-note">
              使用方法：在客户端公式管理器里新建「条件选股公式」，把上面文本整段粘贴进去保存，再执行选股。
            </p>
          </div>
        )}
      </div>
    </section>
  )
}
