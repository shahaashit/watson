// Tiny, dependency-free renderer for the small subset of markdown the model
// emits in answers: headings, bold, bullet lists, and horizontal rules.
// Builds React elements (no dangerouslySetInnerHTML).

// Order matters: link regex must come before #N so `[#5](url)`-style isn't
// accidentally split. Each alternative is a self-contained token.
// - Link text allows ONE level of nested brackets like "[[Video module] Fix X](url)"
//   so titles with bracketed prefixes (common in ClickUp/MRs) still render.
// - Bare URL fallback catches anything the model emits unwrapped (or with broken
//   markdown), so an MR/task link is always clickable.
// Build a fresh RegExp per call so recursion (e.g. bold-wrapped links) doesn't
// trip lastIndex.
const LINK_PART = '\\[(?:[^\\[\\]]|\\[[^\\]]*\\])+\\]\\([^)]+\\)'
const BARE_URL  = 'https?:\\/\\/[^\\s)\\]]+'
const INLINE_PATTERN = `${LINK_PART}|\\*\\*[^*]+\\*\\*|\`[^\`]+\`|#\\d+|${BARE_URL}`

function _shortUrlLabel(url) {
  try {
    const u = new URL(url)
    const path = u.pathname.replace(/\/$/, '').split('/').pop()
    if (u.host.includes('clickup.com')) return `ClickUp task ${path}`
    if (u.pathname.includes('/merge_requests/')) return `MR !${path}`
    return u.host + (u.pathname.length > 30 ? u.pathname.slice(0, 27) + '…' : u.pathname)
  } catch {
    return url.length > 60 ? url.slice(0, 57) + '…' : url
  }
}

function renderInline(text, keyBase) {
  const regex = new RegExp(`(${INLINE_PATTERN})`, 'g')
  const parts = []
  let last = 0
  let m
  let i = 0
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    const key = `${keyBase}-${i}`
    if (tok.startsWith('[')) {
      const lm = tok.match(/^\[((?:[^\[\]]|\[[^\]]*\])+)\]\(([^)]+)\)$/)
      if (lm) {
        parts.push(
          <a key={key} href={lm[2]} target="_blank" rel="noreferrer" className="md-link">
            {lm[1]}
          </a>
        )
      } else {
        parts.push(tok)
      }
    } else if (tok.startsWith('**')) {
      // recursively render inner content so `**[link](url)**` still becomes a link
      parts.push(<strong key={key}>{renderInline(tok.slice(2, -2), `${key}-b`)}</strong>)
    } else if (tok.startsWith('`')) {
      parts.push(<code key={key} className="md-code">{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('http')) {
      parts.push(
        <a key={key} href={tok} target="_blank" rel="noreferrer" className="md-link">
          {_shortUrlLabel(tok)}
        </a>
      )
    } else {
      parts.push(<span key={key} className="md-ref">{tok}</span>)
    }
    last = m.index + tok.length
    i++
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

export default function Markdown({ text }) {
  const lines = (text || '').split('\n')
  const blocks = []
  let list = null

  const flushList = () => {
    if (list) {
      blocks.push(<ul key={`ul-${blocks.length}`} className="md-list">{list}</ul>)
      list = null
    }
  }

  lines.forEach((raw, idx) => {
    const line = raw.trimEnd()
    const bullet = line.match(/^\s*[-*]\s+(.*)$/)
    const heading = line.match(/^(#{1,4})\s+(.*)$/)

    if (bullet) {
      list = list || []
      list.push(<li key={`li-${idx}`}>{renderInline(bullet[1], `li-${idx}`)}</li>)
      return
    }
    flushList()

    if (!line.trim()) return
    if (/^---+$/.test(line.trim())) {
      blocks.push(<hr key={`hr-${idx}`} className="md-hr" />)
      return
    }
    if (heading) {
      const level = heading[1].length
      blocks.push(
        <div key={`h-${idx}`} className={`md-h md-h${level}`}>
          {renderInline(heading[2], `h-${idx}`)}
        </div>
      )
      return
    }
    blocks.push(<p key={`p-${idx}`} className="md-p">{renderInline(line, `p-${idx}`)}</p>)
  })
  flushList()
  return <div className="md">{blocks}</div>
}
