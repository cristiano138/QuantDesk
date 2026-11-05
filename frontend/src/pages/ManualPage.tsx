/**
 * 应用内使用手册（/manual）。
 *
 * 手册正文全部来自后端 /api/manual/*（每个板块一篇：准备什么 → 怎么做 → 怎么读 →
 * 常见误解 → 提示原文怎么办 → 问答）。本页只做导航、搜索与呈现，正文一条都不写在
 * 前端，避免「前端文案」和「后端手册」漂成两套事实；本页无任何下单/交易入口。
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  apiError,
  getManualDoc,
  getManualSections,
  searchManual,
  type ManualDoc,
  type ManualHit,
  type ManualSection,
} from '../api/client'

/** 左侧目录的分组顺序（后端新增分组时自动追加在后面，不会被吞掉）。 */
const GROUP_ORDER = ['入门', '看盘', '研究', '交易', '数据与设置', '自助排障']

/** 站内路径白名单：只有真实存在的路由才渲染成可点链接（data/parquet 这类不是路由）。 */
const APP_ROUTES = [
  '/watch', '/market', '/backtest', '/monitor', '/channels',
  '/interop', '/data', '/guide', '/compliance', '/settings', '/manual',
]

const LINK_STYLE: CSSProperties = { color: '#1d4ed8', textDecoration: 'underline' }
const PRE_WRAP: CSSProperties = { whiteSpace: 'pre-wrap', lineHeight: 1.8 }
const H4_STYLE: CSSProperties = { margin: '16px 0 6px', fontSize: 13, color: '#111827' }

/** 后端数组字段兜底：契约缺字段时按空处理，而不是让整页崩成白屏。 */
function arr<T>(v: T[] | undefined | null): T[] {
  return Array.isArray(v) ? v : []
}

/**
 * 把正文里的站内路径（/settings、/manual?s=watch 这种）变成可点链接。
 * 判据是「基路径在白名单里」——否则 /parquet、/api/manual 会被误当链接。
 */
function linkify(text: string, known: Set<string>, keyBase: string): ReactNode {
  if (!text) return text
  const re = /\/[a-z][a-z0-9_-]*(?:\?[A-Za-z0-9_=&%+@.,-]*)?/gi
  const out: ReactNode[] = []
  let last = 0
  let i = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    let raw = m[0]
    // 后面还跟着 / → 命中的只是更长路径的一段（/api/manual/sections 里别抠出 /manual）
    if (text[m.index + raw.length] === '/') continue
    // 查询串末尾吃到的 ASCII 标点退回正文（/data, 不该变成 /data, 的一部分）
    const trail = /[.,;]+$/.exec(raw)
    if (trail) raw = raw.slice(0, raw.length - trail[0].length)
    const base = raw.split('?')[0]
    const key = base.toLowerCase() // 白名单是小写路由；手册里写成 /Watch 也该能点
    if (!known.has(key)) continue
    if (m.index > last) out.push(text.slice(last, m.index))
    out.push(
      <Link key={`${keyBase}-lk-${i++}`} to={key + raw.slice(base.length)} style={LINK_STYLE}>
        {raw}
      </Link>,
    )
    last = m.index + raw.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out.length === 0 ? text : out
}

/** 分区标题统一走这个组件，保证每篇手册的阅读顺序一致。 */
function BlockTitle({ children }: { children: ReactNode }) {
  return <h4 style={H4_STYLE}>{children}</h4>
}

/** 左侧目录：按 group 分组，条目带一句话摘要，点击即写 ?s= 深链。 */
function SectionNav({
  groups,
  active,
  total,
  onSelect,
}: {
  groups: [string, ManualSection[]][]
  active: string
  total: number
  onSelect: (slug: string) => void
}) {
  return (
    <div className="card nav-list">
      <h3>手册目录（{total} 篇）</h3>
      {groups.map(([group, items]) => (
        <div key={group}>
          <p className="sub-note" style={{ fontWeight: 600, margin: '10px 0 4px' }}>
            {group}
          </p>
          <ul>
            {items.map((s) => (
              <li key={s.slug}>
                <button className={s.slug === active ? 'active' : ''} onClick={() => onSelect(s.slug)}>
                  {s.title}
                  <span className="sub-note" style={{ display: 'block' }}>
                    {s.summary}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
      <p className="sub-note">
        正文来自内置使用手册；找不到某个板块时，点上方搜索框搜关键词更省事。
      </p>
    </div>
  )
}

/** 搜索面板：≥1 字符即搜（300ms 防抖），结果显示「板块 + 命中字段 + 上下文」。 */
function SearchPanel({
  rawQ,
  needle,
  hits,
  loading,
  error,
  onInput,
  onClear,
  onPick,
}: {
  rawQ: string
  needle: string
  hits: ManualHit[] | null
  loading: boolean
  error: string
  onInput: (v: string) => void
  onClear: () => void
  onPick: (slug: string) => void
}) {
  return (
    <div className="card">
      <div className="filter-row">
        <label style={{ maxWidth: 420, flex: 1 }}>
          搜手册（板块名、界面字段、提示原文都能搜）
          <input
            value={rawQ}
            onChange={(e) => onInput(e.target.value)}
            placeholder="例如：立即刷新、内外盘、涨停 656.70、待确认、非官方软件"
          />
        </label>
        <button className="mini" onClick={onClear} disabled={rawQ === ''}>
          清空
        </button>
      </div>
      {needle !== '' && (
        loading ? (
          <p className="sub-note">正在检索手册…</p>
        ) : error ? (
          <p className="badge badge-down">搜索失败：{error}</p>
        ) : hits && hits.length > 0 ? (
          <>
            <p className="sub-note">
              「{needle}」命中 <b>{hits.length}</b> 处，点一条直接翻到那一篇。
            </p>
            <ul style={{ listStyle: 'none', padding: 0, margin: '6px 0 0' }}>
              {hits.map((h, i) => (
                <li key={`${h.slug}-${h.field}-${i}`} style={{ marginBottom: 6 }}>
                  <button
                    className="mini"
                    style={{ textAlign: 'left', width: '100%' }}
                    onClick={() => onPick(h.slug)}
                  >
                    <b>{h.title}</b>
                    <span className="tag" style={{ marginLeft: 6 }}>{h.field}</span>
                    <span className="sub-note" style={{ display: 'block' }}>{h.snippet}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="sub-note warn">
            手册里没有「{needle}」的说法。换界面上一字不差的那个词再搜（比如按钮名、提示原文），
            或者从左边目录按板块翻。
          </p>
        )
      )}
    </div>
  )
}

/** 右侧正文：七个分区按固定顺序呈现。 */
function DocView({
  doc,
  known,
  loading,
  error,
  onRetry,
}: {
  doc: ManualDoc | null
  known: Set<string>
  loading: boolean
  error: string
  onRetry: () => void
}) {
  if (loading) return <p className="placeholder">正在加载这一篇手册…</p>
  if (error) {
    return (
      <div className="alert-banner">
        <p>
          <b>这篇手册没取到：</b>
          {error}
        </p>
        <p>
          <button className="mini" onClick={onRetry}>
            重试
          </button>
          <span className="sub-note">
            正文来自内置使用手册；连上系统后即可恢复，本页不会用空内容顶替。
          </span>
        </p>
      </div>
    )
  }
  if (!doc) {
    return (
      <p className="placeholder">
        从左边目录选一个板块，就能看到那一篇：用之前要准备什么、一步步点哪里、界面上的数字怎么读。
      </p>
    )
  }

  const prereqs = arr(doc.prerequisites)
  const steps = arr(doc.steps)
  const readouts = arr(doc.readouts)
  const gotchas = arr(doc.gotchas)
  const errors = arr(doc.errors)
  const faq = arr(doc.faq)
  const links = arr(doc.links)

  return (
    <div className="card">
      <div className="row-btns">
        <h3 style={{ margin: 0 }}>{doc.title}</h3>
        {doc.status && (
          <span className={`badge ${doc.status === '已实测' ? 'badge-paper' : 'badge-live'}`}>
            {doc.status}
          </span>
        )}
        {doc.route && known.has(doc.route.split('?')[0]) && (
          <Link to={doc.route} className="mini" style={LINK_STYLE}>
            打开这个页面
          </Link>
        )}
      </div>
      {doc.nav && <p className="sub-note">对应左侧导航入口：{doc.nav}</p>}

      {doc.intro && (
        <p style={PRE_WRAP} className="prereq">
          {linkify(doc.intro, known, 'intro')}
        </p>
      )}

      {prereqs.length > 0 && (
        <>
          <BlockTitle>用之前要准备好</BlockTitle>
          <p className="sub-note">每一条都写了「在哪设置」和「怎么自查」——自查通过再往下做，别凭感觉。</p>
          {prereqs.map((p, i) => (
            <div className="result-block" key={`pre-${i}`}>
              <b style={{ fontSize: 13 }}>{p.what}</b>
              <p className="sub-note">为什么要它：{linkify(p.why, known, `pre-why-${i}`)}</p>
              <p className="sub-note">在哪设置：{linkify(p.where, known, `pre-where-${i}`)}</p>
              <p className="sub-note">怎么判断自己满足了：{linkify(p.ready, known, `pre-ready-${i}`)}</p>
            </div>
          ))}
        </>
      )}

      {steps.length > 0 && (
        <>
          <BlockTitle>一步步怎么做</BlockTitle>
          <ol className="impact-list">
            {steps.map((s, i) => (
              <li key={`step-${i}`}>
                {linkify(s.do, known, `step-do-${i}`)}
                {s.expect && <span className="sub-note" style={{ display: 'block' }}>应该看到：{linkify(s.expect, known, `step-expect-${i}`)}</span>}
              </li>
            ))}
          </ol>
        </>
      )}

      {readouts.length > 0 && (
        <>
          <BlockTitle>界面数字怎么读</BlockTitle>
          <table>
            <thead>
              <tr><th style={{ width: '32%' }}>字段 / 位置</th><th>含义</th></tr>
            </thead>
            <tbody>
              {readouts.map((r, i) => (
                <tr key={`ro-${i}`}>
                  <td>{linkify(r.field, known, `ro-f-${i}`)}</td>
                  <td>{linkify(r.meaning, known, `ro-m-${i}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {gotchas.length > 0 && (
        <>
          <BlockTitle>以为是这样其实不是</BlockTitle>
          <table>
            <thead>
              <tr><th style={{ width: '42%' }}>看起来像</th><th>实际是</th></tr>
            </thead>
            <tbody>
              {gotchas.map((g, i) => (
                <tr key={`go-${i}`}>
                  <td className="warn">{linkify(g.myth, known, `go-myth-${i}`)}</td>
                  <td>{linkify(g.truth, known, `go-truth-${i}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {errors.length > 0 && (
        <>
          <BlockTitle>看到这句提示是什么意思</BlockTitle>
          <p className="sub-note">提示原文按界面上的字照抄；「怎么办」里的路径可以直接点。</p>
          {errors.map((e, i) => (
            <div className="result-block" key={`err-${i}`}>
              <span className="dialog-red">{e.text}</span>
              <p className="sub-note">意思：{linkify(e.meaning, known, `err-meaning-${i}`)}</p>
              <p className="sub-note">怎么办：{linkify(e.fix, known, `err-fix-${i}`)}</p>
            </div>
          ))}
        </>
      )}

      {faq.length > 0 && (
        <>
          <BlockTitle>常见问答</BlockTitle>
          {faq.map((f, i) => (
            <div key={`faq-${i}`} style={{ marginBottom: 8 }}>
              <p style={{ margin: '2px 0', fontSize: 13, fontWeight: 600 }}>问：{f.q}</p>
              <p style={{ margin: '2px 0 6px', fontSize: 13 }} className="prereq">
                答：{linkify(f.a, known, `faq-a-${i}`)}
              </p>
            </div>
          ))}
        </>
      )}

      {links.length > 0 && (
        <>
          <BlockTitle>相关入口</BlockTitle>
          <div className="row-btns">
            {links.map((l, i) =>
              l.to.startsWith('/') ? (
                <Link key={`lk-${i}`} to={l.to} className="mini" style={LINK_STYLE}>
                  {l.label}
                </Link>
              ) : (
                <span key={`lk-${i}`} className="badge">
                  {l.label}
                  {l.to ? `：${l.to}` : ''}
                </span>
              ),
            )}
          </div>
        </>
      )}
    </div>
  )
}

export default function ManualPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const slug = searchParams.get('s') ?? ''

  const [sections, setSections] = useState<ManualSection[] | null>(null)
  const [sectionsErr, setSectionsErr] = useState('')
  const [sectionsRetry, setSectionsRetry] = useState(0)

  const [doc, setDoc] = useState<ManualDoc | null>(null)
  const [docErr, setDocErr] = useState('')
  const [docLoading, setDocLoading] = useState(false)
  const [docRetry, setDocRetry] = useState(0)

  const [rawQ, setRawQ] = useState('')
  const [needle, setNeedle] = useState('')
  const [hits, setHits] = useState<ManualHit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchErr, setSearchErr] = useState('')

  const searchSeq = useRef(0)
  const docCache = useRef<Map<string, ManualDoc>>(new Map())

  /**
   * 目录一次拿全（十几篇，体积小），正文按 slug 单取 + 本地缓存。
   * 依赖只有「重试」：setSearchParams 每次 URL 变都会换身份，挂进依赖里会变成
   * 每翻一个板块就重取一次目录。
   */
  useEffect(() => {
    let alive = true
    setSectionsErr('')
    getManualSections()
      .then((s) => {
        if (alive) setSections(s)
      })
      .catch((e: unknown) => {
        if (alive) setSectionsErr(apiError(e))
      })
    return () => {
      alive = false
    }
  }, [sectionsRetry])

  /** 深链没带 ?s= 时落到目录第一篇（通常是 start-here），不给新手一个空白页。 */
  useEffect(() => {
    if (!sections || sections.length === 0) return
    if (searchParams.get('s')) return
    setSearchParams((cur) => {
      const next = new URLSearchParams(cur)
      if (!next.get('s')) next.set('s', sections[0].slug)
      return next
    }, { replace: true })
  }, [sections, searchParams, setSearchParams])

  /** 正文只在 slug 变化时取；失败如实上屏并给重试，不用空文档顶替。 */
  useEffect(() => {
    if (!slug) {
      setDoc(null)
      setDocErr('')
      setDocLoading(false)
      return
    }
    const cached = docCache.current.get(slug)
    if (cached) {
      setDoc(cached)
      setDocErr('')
      setDocLoading(false)
      return
    }
    let alive = true
    setDocLoading(true)
    setDocErr('')
    getManualDoc(slug)
      .then((d) => {
        if (!alive) return
        docCache.current.set(d.slug || slug, d)
        setDoc(d)
        setDocLoading(false)
      })
      .catch((e: unknown) => {
        if (!alive) return
        setDoc(null)
        setDocErr(apiError(e))
        setDocLoading(false)
      })
    return () => {
      alive = false
    }
  }, [slug, docRetry])

  /** 输入防抖 300ms 后才搜（≥1 个字符），并丢弃过期响应。 */
  useEffect(() => {
    const id = window.setTimeout(() => setNeedle(rawQ.trim()), 300)
    return () => window.clearTimeout(id)
  }, [rawQ])

  useEffect(() => {
    if (!needle) {
      setHits(null)
      setSearchErr('')
      setSearching(false)
      return
    }
    const seq = ++searchSeq.current
    setSearching(true)
    setSearchErr('')
    searchManual(needle)
      .then((r) => {
        if (seq !== searchSeq.current) return
        setHits(arr(r.hits))
      })
      .catch((e: unknown) => {
        if (seq !== searchSeq.current) return
        setHits(null)
        setSearchErr(apiError(e))
      })
      .finally(() => {
        if (seq === searchSeq.current) setSearching(false)
      })
  }, [needle])

  const known = useMemo(() => {
    const set = new Set(APP_ROUTES)
    for (const s of sections ?? []) if (s.route) set.add(s.route.split('?')[0])
    return set
  }, [sections])

  /** 目录分组：固定顺序在前，后端新增的分组名追加在后。 */
  const groups = useMemo<[string, ManualSection[]][]>(() => {
    const src = sections ?? []
    const rest = Array.from(new Set(src.map((s) => s.group || '其他')))
    const order = [...GROUP_ORDER.filter((g) => rest.includes(g)), ...rest.filter((g) => !GROUP_ORDER.includes(g))]
    return order.map((g) => [g, src.filter((s) => (s.group || '其他') === g)] as [string, ManualSection[]])
  }, [sections])

  const selectSection = useCallback(
    (next: string) => {
      setSearchParams(
        (cur) => {
          const p = new URLSearchParams(cur)
          if (next) p.set('s', next)
          else p.delete('s')
          return p
        },
        { replace: true },
      )
      // 搜索面板只是辅助：定了板块就收起来，不挡正文
      setRawQ('')
      window.scrollTo({ top: 0 })
    },
    [setSearchParams],
  )

  if (sectionsErr) {
    return (
      <section>
        <h2>使用手册</h2>
        <div className="alert-banner">
          <p>
            <b>手册内容当前读取不到</b>——请确认软件已正常启动（双击 启动.bat）。
          </p>
          <p>失败原文：{sectionsErr}</p>
          <p>
            <button className="mini" onClick={() => { setSections(null); setSectionsRetry((v) => v + 1) }}>
              重试
            </button>
            <span className="sub-note">（目录取不到时本页不会显示空列表假装没内容）</span>
          </p>
        </div>
      </section>
    )
  }

  if (!sections) {
    return (
      <section>
        <h2>使用手册</h2>
        <p className="placeholder">正在加载手册目录…</p>
      </section>
    )
  }

  if (sections.length === 0) {
    return (
      <section>
        <h2>使用手册</h2>
        <p className="placeholder">
          手册目录为空：手册内容还没有准备好，不是本页加载失败。
          <button className="mini" style={{ marginLeft: 8 }} onClick={() => { setSections(null); setSectionsRetry((v) => v + 1) }}>
            重新取一次
          </button>
        </p>
      </section>
    )
  }

  return (
    <section>
      <h2>使用手册</h2>
      <p className="sub-note" style={{ marginBottom: 12 }}>
        每个板块一篇：用之前要准备什么、一步步点哪里、界面上的数字怎么读、看到某句提示该怎么办。
        不熟悉的板块先读对应这一篇再动手；正文与界面口径同源。
      </p>

      <div className="manual-layout">
        <SectionNav groups={groups} active={slug} total={sections.length} onSelect={selectSection} />

        <div style={{ minWidth: 0 }}>
          <SearchPanel
            rawQ={rawQ}
            needle={needle}
            hits={hits}
            loading={searching}
            error={searchErr}
            onInput={setRawQ}
            onClear={() => setRawQ('')}
            onPick={selectSection}
          />
          <DocView
            doc={doc}
            known={known}
            loading={docLoading}
            error={docErr}
            onRetry={() => setDocRetry((v) => v + 1)}
          />
        </div>
      </div>
    </section>
  )
}
