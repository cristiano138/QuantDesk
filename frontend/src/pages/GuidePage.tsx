/**
 * 新手指引（只读术语库浏览器）。
 *
 * 全部术语内容来自后端 /api/guide/*（services/guide.py 是唯一事实源），
 * 本页只负责：分类导航、搜索/过滤、详情阅读、术语间跳转与深链定位。
 * 页面本身不含任何下单/交易入口，文案只做导航说明。
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
import { NavLink, useSearchParams } from 'react-router-dom'
import {
  apiError,
  getGuideCategories,
  getGuideTerm,
  getGuideTerms,
  type GuideCategory,
  type GuideTerm,
} from '../api/client'
import HelpLink from '../components/HelpLink'

/** 「全部」分类的本地哨兵值（不发给后端：全量已在前端过滤）。 */
const ALL_CAT = 'all'

/**
 * 新手第一步的阅读顺序（只写 slug，名称一律从后端数据里取）。
 * slug 与 services/guide.py 词表一致；后端若改名，这里会自然跳过而不报错。
 */
const LEARNING_SLUGS = [
  'a-share',
  'hk-share',
  'trading-hours',
  'last-price',
  'volume-amount',
  'outer-inner',
  'five-level',
  'tick-data',
  'adjust-forward',
  'side-code',
]

/** 空结果时的建议词（用户搜的口语词，不是术语正文）。 */
const SUGGEST_WORDS = ['外盘', '换手率', '前复权', '市盈率', '涨跌停', '碎股']

const MARK_STYLE: CSSProperties = {
  background: '#fff3bf',
  color: 'inherit',
  borderRadius: 3,
  padding: '0 1px',
}

const PRE_WRAP: CSSProperties = { whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.8 }

/** 搜索范围：与后端 guide.terms() 的检索字段保持一致。 */
function hayOf(t: GuideTerm): string {
  return [t.term, t.aliases.join(' '), t.one_liner, t.explain, t.example, t.where_to_see]
    .join(' ')
    .toLowerCase()
}

/** 命中次数（等价于 Python 的 str.count，用于复刻后端的排序权重）。 */
function countHits(hay: string, key: string): number {
  let n = 0
  let at = hay.indexOf(key)
  while (at >= 0) {
    n += 1
    at = hay.indexOf(key, at + key.length)
  }
  return n
}

/** 把命中词标出来：<mark> + 行内底色，不引入任何依赖。 */
function highlight(text: string, needle: string): ReactNode {
  if (!needle || !text) return text
  const lower = text.toLowerCase()
  const key = needle.toLowerCase()
  const out: ReactNode[] = []
  let from = 0
  let at = lower.indexOf(key)
  let i = 0
  while (at >= 0) {
    if (at > from) out.push(text.slice(from, at))
    i += 1
    out.push(
      <mark key={`hit-${i}`} style={MARK_STYLE}>
        {text.slice(at, at + key.length)}
      </mark>,
    )
    from = at + key.length
    at = lower.indexOf(key, from)
  }
  if (from < text.length) out.push(text.slice(from))
  return out
}

/** 左侧分类导航：10 类 + 「全部」，每项带数量与后端给的 summary。 */
function CategoryNav({
  cats,
  total,
  active,
  invalidCat,
  onSelect,
}: {
  cats: GuideCategory[]
  total: number
  active: string
  invalidCat: string
  onSelect: (id: string) => void
}) {
  return (
    <div className="card strategy-tree">
      <h3>分类</h3>
      <ul>
        <li>
          <button className={active === ALL_CAT ? 'active' : ''} onClick={() => onSelect(ALL_CAT)}>
            全部（{total}）
            <span className="sub-note" style={{ display: 'block' }}>
              涵盖{cats.map((c) => c.title).join('、')}。
            </span>
          </button>
        </li>
        {cats.map((c) => (
          <li key={c.id}>
            <button className={active === c.id ? 'active' : ''} onClick={() => onSelect(c.id)}>
              {c.title}（{c.count}）
              <span className="sub-note" style={{ display: 'block' }}>
                {c.summary}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {invalidCat && (
        <p className="sub-note warn">分类「{invalidCat}」不在术语库里，已按「全部」展示。</p>
      )}
      <p className="sub-note">本页只有名词解释，没有任何下单入口。</p>
    </div>
  )
}

/** 「学习路径」：仅在全部分类且未输入关键词时出现，点击直接进详情。 */
function LearningPath({
  items,
  active,
  onSelect,
}: {
  items: GuideTerm[]
  active: string
  onSelect: (slug: string) => void
}) {
  if (items.length === 0) return null
  return (
    <div className="card">
      <h3>学习路径：先按顺序读这 {items.length} 个</h3>
      <p className="sub-note">
        新手第一步按这个顺序点一遍，实时盯盘页第一屏看到的词基本都能对上；点任意一条即打开右侧详情。
      </p>
      <div className="row-btns">
        {items.map((t, i) => (
          <button
            key={t.slug}
            className={`mini${t.slug === active ? ' active' : ''}`}
            style={t.slug === active ? { background: '#e8effd', borderColor: '#2563eb', color: '#1d4ed8' } : undefined}
            onClick={() => onSelect(t.slug)}
          >
            {i + 1}. {t.term}
          </button>
        ))}
      </div>
      <p className="sub-note mono">
        {items.map((t) => t.slug).join(' → ')}
      </p>
    </div>
  )
}

/** 中间栏：术语列表（命中词高亮）+ 空结果引导。 */
function TermList({
  rows,
  total,
  needle,
  active,
  catTitle,
  onSelect,
  onSuggest,
}: {
  rows: GuideTerm[]
  total: number
  needle: string
  active: string
  catTitle: (id: string) => string
  onSelect: (slug: string) => void
  onSuggest: (word: string) => void
}) {
  if (rows.length === 0) {
    return (
      <div className="card">
        <h3>术语列表</h3>
        <p className="placeholder">
          没有匹配的术语，换个词试试（例如“市盈率”“停牌”）。
        </p>
        <div className="row-btns">
          {SUGGEST_WORDS.map((w) => (
            <button key={w} className="mini" onClick={() => onSuggest(w)}>
              {w}
            </button>
          ))}
        </div>
        <p className="sub-note">
          也可以清掉关键词、切回「全部」分类，从「学习路径」那 10 个术语开始读。
        </p>
      </div>
    )
  }
  return (
    <div className="card strategy-tree">
      <h3>术语列表（{rows.length} / {total}）</h3>
      <ul>
        {rows.map((t) => (
          <li key={t.slug}>
            <button className={t.slug === active ? 'active' : ''} onClick={() => onSelect(t.slug)}>
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <b>{highlight(t.term, needle)}</b>
                <span className={`badge ${t.level === '入门' ? 'badge-paper' : 'badge-live'}`}>
                  {t.level}
                </span>
                <span className="tag">{catTitle(t.category)}</span>
              </span>
              <span className="sub-note" style={{ display: 'block' }}>
                {highlight(t.one_liner, needle)}
              </span>
              {t.aliases.length > 0 && (
                <span className="sub-note" style={{ display: 'block' }}>
                  别名：
                  {t.aliases.map((a, i) => (
                    <span key={`al-${t.slug}-${i}`}>
                      {i > 0 ? '、' : ''}
                      {highlight(a, needle)}
                    </span>
                  ))}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** 右侧详情：字段全部来自单条接口（含 related_terms）。 */
function TermDetail({
  term,
  loading,
  error,
  slug,
  needle,
  catTitle,
  onRetry,
  onOpenRelated,
}: {
  term: GuideTerm | null
  loading: boolean
  error: string
  slug: string
  needle: string
  catTitle: (id: string) => string
  onRetry: () => void
  onOpenRelated: (slug: string) => void
}) {
  if (loading) return <p className="placeholder">正在加载术语详情…</p>
  if (error) {
    return (
      <div className="alert-banner">
        <p>
          <b>术语详情加载失败：</b>
          {error}
        </p>
        <p>
          <button className="mini" onClick={onRetry}>
            重试
          </button>
          <span className="sub-note">
            （也可以从左侧列表另点一个术语）
          </span>
        </p>
      </div>
    )
  }
  if (!term) {
    return (
      <p className="placeholder">
        {slug ? '正在准备详情…' : '从左侧分类里点一个术语，这里显示完整解释：是什么、怎么算、举例、容易踩的坑、在本工作台哪里能看到，以及可以顺藤摸瓜的相关术语。'}
      </p>
    )
  }

  const rel = term.related_terms ?? []
  const relOnlySlug = term.related.filter((s) => !rel.some((r) => r.slug === s))

  return (
    <div className="card">
      <div className="row-btns">
        <h3 style={{ margin: 0 }}>{highlight(term.term, needle)}</h3>
        <span className={`badge ${term.level === '入门' ? 'badge-paper' : 'badge-live'}`}>
          {term.level}
        </span>
        <span className="tag">{catTitle(term.category)}</span>
      </div>
      <p className="sub-note mono">
        {term.slug}｜直接分享本页链接即可定位：/guide?c={term.category}&amp;t={term.slug}
      </p>

      {term.aliases.length > 0 && (
        <p style={{ margin: '6px 0' }}>
          {term.aliases.map((a, i) => (
            <span key={`dal-${term.slug}-${i}`} className="tag">
              {highlight(a, needle)}
            </span>
          ))}
        </p>
      )}

      <p style={{ fontSize: 15, fontWeight: 600, color: '#111827', margin: '8px 0 10px' }}>
        {highlight(term.one_liner, needle)}
      </p>

      <h4>解释</h4>
      <p style={PRE_WRAP}>{highlight(term.explain, needle)}</p>

      {term.example && (
        <div className="result-block" style={{ borderLeft: '3px solid #2563eb' }}>
          <h4 style={{ margin: '0 0 4px' }}>举例</h4>
          <p style={PRE_WRAP}>{highlight(term.example, needle)}</p>
        </div>
      )}

      {term.pitfalls.length > 0 && (
        <>
          <h4>新手容易踩的坑</h4>
          <ul className="warn" style={{ fontSize: 13, lineHeight: 1.8, margin: '4px 0 10px', paddingLeft: 20 }}>
            {term.pitfalls.map((p, i) => (
              <li key={`pit-${i}`}>{highlight(p, needle)}</li>
            ))}
          </ul>
        </>
      )}

      {term.where_to_see && (
        <>
          <h4>在本工作台哪里能看到</h4>
          <p className="sub-note" style={{ margin: '2px 0 6px' }}>
            {highlight(term.where_to_see, needle)}
          </p>
          <p className="sub-note">
            回
            <NavLink to="/watch" style={{ color: '#1d4ed8', margin: '0 2px' }}>
              实时盯盘
            </NavLink>
            页对照着看更容易懂。
          </p>
        </>
      )}

      {(rel.length > 0 || relOnlySlug.length > 0) && (
        <>
          <h4>相关术语</h4>
          <p className="sub-note">顺着这些词往下点，能把整本术语库读完。</p>
          <div className="row-btns">
            {rel.map((r) => (
              <button key={r.slug} className="mini" onClick={() => onOpenRelated(r.slug)}>
                {highlight(r.term, needle)}
              </button>
            ))}
            {/* 兜底：列表接口只给 slug、不带名称，这里原样显示 slug 仍可点 */}
            {relOnlySlug.map((s) => (
              <button key={s} className="mini mono" onClick={() => onOpenRelated(s)}>
                {s}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

export default function GuidePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const catParam = searchParams.get('c') ?? ''
  const termParam = searchParams.get('t') ?? ''

  const [cats, setCats] = useState<GuideCategory[] | null>(null)
  const [all, setAll] = useState<GuideTerm[] | null>(null)
  const [loadErr, setLoadErr] = useState('')
  const [retry, setRetry] = useState(0)

  const [rawQ, setRawQ] = useState('')
  const [needle, setNeedle] = useState('')
  const [onlyBasic, setOnlyBasic] = useState(false)

  const [detail, setDetail] = useState<GuideTerm | null>(null)
  const [detailErr, setDetailErr] = useState('')
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailRetry, setDetailRetry] = useState(0)
  // 全库仅 101 条，详情按 slug 缓存：回看不重复请求
  const detailCache = useRef<Map<string, GuideTerm>>(new Map())

  /** 首屏一次拿全量（分类 + 全部术语），之后搜索/过滤都在本地做，输入即时响应。 */
  useEffect(() => {
    let alive = true
    setLoadErr('')
    Promise.all([getGuideCategories(), getGuideTerms()])
      .then(([c, t]) => {
        if (!alive) return
        setCats(c)
        setAll(t)
      })
      .catch((e: unknown) => {
        if (alive) setLoadErr(apiError(e)) // 后端 detail 原文回显，不静默空白
      })
    return () => {
      alive = false
    }
  }, [retry])

  /** 输入防抖 ~200ms：停顿后才过滤，不打后端。 */
  useEffect(() => {
    const id = window.setTimeout(() => setNeedle(rawQ.trim()), 200)
    return () => window.clearTimeout(id)
  }, [rawQ])

  const knownIds = useMemo(() => new Set((cats ?? []).map((c) => c.id)), [cats])
  const category = catParam !== '' && knownIds.has(catParam) ? catParam : ALL_CAT
  const invalidCat =
    cats !== null && catParam !== '' && catParam !== ALL_CAT && !knownIds.has(catParam) ? catParam : ''

  const catTitle = useCallback(
    (id: string) => (cats ?? []).find((c) => c.id === id)?.title ?? id,
    [cats],
  )

  /** 本地过滤：分类 → 级别 → 关键词（命中数多的排前，复刻后端 guide.terms() 的权重）。 */
  const rows = useMemo(() => {
    const src = all ?? []
    const scoped = category === ALL_CAT ? src : src.filter((t) => t.category === category)
    const leveled = onlyBasic ? scoped.filter((t) => t.level === '入门') : scoped
    const key = needle.toLowerCase()
    if (!key) return leveled
    const scored = leveled
      .map((t) => {
        const hay = hayOf(t)
        return { t, score: countHits(hay, key) + (t.term.toLowerCase().includes(key) ? 5 : 0) }
      })
      .filter((x) => x.score > 0)
    scored.sort((a, b) => b.score - a.score)
    return scored.map((x) => x.t)
  }, [all, category, onlyBasic, needle])

  const learning = useMemo(
    () =>
      LEARNING_SLUGS.map((s) => (all ?? []).find((t) => t.slug === s)).filter(
        (t): t is GuideTerm => t !== undefined,
      ),
    [all],
  )

  const writeParams = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(searchParams)
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === '') next.delete(k)
        else next.set(k, v)
      }
      setSearchParams(next)
    },
    [searchParams, setSearchParams],
  )

  const selectCategory = useCallback((id: string) => writeParams({ c: id === ALL_CAT ? null : id }), [writeParams])
  const selectTerm = useCallback((slug: string) => writeParams({ t: slug || null }), [writeParams])
  const applyWord = useCallback((w: string) => {
    setRawQ(w)
    setNeedle(w)
  }, [])
  const clearSearch = useCallback(() => {
    setRawQ('')
    setNeedle('')
  }, [])

  /**
   * 详情只在「选中 slug」变化时取一次：related_terms（带中文名）只有单条接口才返回，
   * 列表接口里的 related 只是 slug，所以进详情必须走 getGuideTerm。
   */
  useEffect(() => {
    if (!termParam) {
      setDetail(null)
      setDetailErr('')
      setDetailLoading(false)
      return
    }
    const cached = detailCache.current.get(termParam)
    if (cached) {
      setDetail(cached)
      setDetailErr('')
      setDetailLoading(false)
      return
    }
    let alive = true
    setDetailLoading(true)
    setDetailErr('')
    getGuideTerm(termParam)
      .then((d) => {
        if (!alive) return
        detailCache.current.set(d.slug, d)
        setDetail(d)
        setDetailLoading(false)
      })
      .catch((e: unknown) => {
        if (!alive) return
        setDetail(null)
        setDetailErr(apiError(e))
        setDetailLoading(false)
      })
    return () => {
      alive = false
    }
  }, [termParam, detailRetry])

  if (loadErr) {
    return (
      <section>
        <h2>新手指引<HelpLink slug="guide" /></h2>
        <div className="alert-banner">
          <p>
            <b>术语库加载失败：</b>
            {loadErr}
          </p>
          <p>
            <button className="mini" onClick={() => setRetry((v) => v + 1)}>
              重试
            </button>
            <span className="sub-note">（连上系统后即可恢复）</span>
          </p>
        </div>
      </section>
    )
  }

  if (!cats || !all) {
    return (
      <section>
        <h2>新手指引<HelpLink slug="guide" /></h2>
        <p className="placeholder">正在加载术语库…</p>
      </section>
    )
  }

  const showPath = category === ALL_CAT && needle === ''

  return (
    <section>
      <h2>新手指引<HelpLink slug="guide" /></h2>
      <p className="sub-note" style={{ marginBottom: 12 }}>
        零基础看盘词典：全库 {all.length} 条术语、{cats.length} 个分类。左边选分类，中间点术语，右边看解释。
        内容来自内置术语库，本页不做任何交易相关操作。
      </p>

      <div className="guide-layout">
        <CategoryNav
          cats={cats}
          total={all.length}
          active={category}
          invalidCat={invalidCat}
          onSelect={selectCategory}
        />

        <div style={{ minWidth: 0 }}>
          <div className="card">
            <div className="filter-row">
              <label style={{ maxWidth: 360, flex: 1 }}>
                搜索术语
                <input
                  value={rawQ}
                  onChange={(e) => setRawQ(e.target.value)}
                  placeholder="搜术语：外盘、换手率、前复权、港股通…"
                />
              </label>
              <button className="mini" onClick={clearSearch} disabled={rawQ === ''}>
                清空搜索
              </button>
              <div className="tabs" style={{ margin: 0 }}>
                <button className={!onlyBasic ? 'active' : ''} onClick={() => setOnlyBasic(false)}>
                  全部级别
                </button>
                <button className={onlyBasic ? 'active' : ''} onClick={() => setOnlyBasic(true)}>
                  只看入门
                </button>
              </div>
            </div>
            <p className="sub-note">
              {needle !== '' ? (
                <>
                  关键词「{needle}」命中 <b>{rows.length}</b> 条（按命中次数排序，词名命中优先）
                </>
              ) : (
                <>
                  当前范围 <b>{rows.length}</b> 条术语{onlyBasic ? '（已筛「入门」级）' : '（全部级别）'}
                </>
              )}
            </p>
          </div>

          {showPath && <LearningPath items={learning} active={termParam} onSelect={selectTerm} />}

          <TermList
            rows={rows}
            total={all.length}
            needle={needle}
            active={termParam}
            catTitle={catTitle}
            onSelect={selectTerm}
            onSuggest={applyWord}
          />
        </div>

        <div style={{ minWidth: 0 }}>
          <TermDetail
            term={detail}
            loading={detailLoading}
            error={detailErr}
            slug={termParam}
            needle={needle}
            catTitle={catTitle}
            onRetry={() => setDetailRetry((v) => v + 1)}
            onOpenRelated={selectTerm}
          />
        </div>
      </div>
    </section>
  )
}
