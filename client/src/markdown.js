import { marked } from 'marked'

const ALLOWED_TAGS = new Set([
  'H1',
  'H2',
  'H3',
  'H4',
  'H5',
  'H6',
  'P',
  'BR',
  'HR',
  'BLOCKQUOTE',
  'UL',
  'OL',
  'LI',
  'STRONG',
  'EM',
  'DEL',
  'CODE',
  'PRE',
  'A',
  'TABLE',
  'THEAD',
  'TBODY',
  'TR',
  'TH',
  'TD',
])

export function renderMarkdown(markdown) {
  if (!markdown) return ''

  const cleanText = stripPrivateReasoning(markdown)
  if (!cleanText.trim()) return ''
  const tokens = transformTimestampTokens(marked.lexer(localizeAnalysisLabels(cleanText)))

  const template = document.createElement('template')
  template.innerHTML = marked.parser(tokens)
  template.content.querySelectorAll('*').forEach(sanitizeNode)
  return template.innerHTML
}

export function stripPrivateReasoning(markdown) {
  let cleanText = markdown
    .replace(/<think>[\s\S]*?<\/think>/gi, '')
    .replace(/<think>[\s\S]*$/gi, '')
  if (cleanText.includes('</think>'))
    cleanText = cleanText.split('</think>').pop()
  return cleanText
}

export function localizeAnalysisLabels(markdown) {
  return markdown
    .replace(/^## Conclusions\s*$/gim, '## 核心结论')
    .replace(/^## Evidence\s*$/gim, '## 视频证据')
    .replace(/^## Suggestions\s*$/gim, '## 建议')
    .replace(/^Source:/gim, '源视频：')
    .replace(/^Duration:/gim, '时长：')
    .replace(/^Embedding mode:/gim, '嵌入模式：')
}

function sanitizeNode(node) {
  if (!ALLOWED_TAGS.has(node.tagName)) {
    node.replaceWith(document.createTextNode(node.textContent || ''))
    return
  }

  for (const attribute of [...node.attributes]) {
    const allowed =
      node.tagName === 'A' &&
      (attribute.name === 'href' || attribute.name === 'title')
    if (!allowed) node.removeAttribute(attribute.name)
  }
  if (node.tagName !== 'A') return

  const href = node.getAttribute('href') || ''
  if (!/^(https?:|mailto:|\/|#)/i.test(href)) node.removeAttribute('href')
  node.setAttribute('rel', 'noopener noreferrer')
  if (!href.startsWith('#video-t=')) node.setAttribute('target', '_blank')
}

export function linkVideoTimestamps(markdown) {
  return marked.parser(transformTimestampTokens(marked.lexer(markdown)))
}

export function transformTimestampTokens(tokens) {
  const skip = new Set(['link', 'image', 'code', 'codespan', 'escape'])
  const visit = list => {
    const output = []
    const protectedHtml = []
    for (const token of list) {
      if (token.type === 'html') {
        for (const match of token.raw.matchAll(/<\s*(\/?)\s*(a|pre|code)\b[^>]*>/gi)) {
          if (match[1]) {
            const index = protectedHtml.lastIndexOf(match[2].toLowerCase())
            if (index >= 0) protectedHtml.splice(index)
          }
          else if (!match[0].endsWith('/>')) protectedHtml.push(match[2].toLowerCase())
        }
        output.push(token)
        continue
      }
      if (skip.has(token.type) || protectedHtml.length) { output.push(token); continue }
      if (token.tokens) token.tokens = visit(token.tokens)
      if (token.items) token.items = visit(token.items)
      if (token.header) for (const cell of token.header) cell.tokens = visit(cell.tokens)
      if (token.rows) for (const row of token.rows) for (const cell of row) cell.tokens = visit(cell.tokens)
      if (token.type !== 'text' || token.tokens) { output.push(token); continue }
      let cursor = 0
      for (const match of token.text.matchAll(/\[(\d+:\d{2}(?::\d{2})?)(?: in [^\]]+|[–-]\d+:\d{2}(?::\d{2})?)?\]/g)) {
        const parts = match[1].split(':').map(Number)
        const seconds = parts.length === 3 ? parts[0] * 3600 + parts[1] * 60 + parts[2] : parts[0] * 60 + parts[1]
        if (parts.at(-1) >= 60 || (parts.length === 3 && parts[1] >= 60)
          || parts.some(value => !Number.isSafeInteger(value)) || !Number.isSafeInteger(seconds)) continue
        if (match.index > cursor) output.push({ type: 'text', raw: token.text.slice(cursor, match.index), text: token.text.slice(cursor, match.index) })
        output.push({ type: 'link', raw: match[0], href: `#video-t=${seconds}`, title: null,
          text: match[0], tokens: [{ type: 'text', raw: match[0], text: match[0] }] })
        cursor = match.index + match[0].length
      }
      if (!cursor) output.push(token)
      else if (cursor < token.text.length) output.push({ type: 'text', raw: token.text.slice(cursor), text: token.text.slice(cursor) })
    }
    return output
  }
  const transformed = visit(tokens)
  transformed.links = tokens.links
  return transformed
}
