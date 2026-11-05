import { Link } from 'react-router-dom'

/**
 * 页面标题行的「怎么用？」入口：跳应用内手册对应板块（/manual?s=<slug>）。
 * 表现层只有一个链接，手册正文全部来自后端 /api/manual——组件内不写任何说明文字，
 * 避免前端文案和后端手册漂移成两套事实。
 */
export default function HelpLink({ slug, label = '怎么用？' }: { slug: string; label?: string }) {
  return (
    <Link
      to={`/manual?s=${encodeURIComponent(slug)}`}
      className="mini"
      title={`打开「${slug}」板块的使用手册`}
      style={{
        marginLeft: 8,
        display: 'inline-block',
        color: '#1d4ed8',
        textDecoration: 'none',
        verticalAlign: 'middle',
        background: '#e8effd',
        borderColor: '#c7d8fb',
      }}
    >
      {label}
    </Link>
  )
}
