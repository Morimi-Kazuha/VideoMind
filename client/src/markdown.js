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
  const linkedText = linkVideoTimestamps(localizeAnalysisLabels(cleanText))

  const template = document.createElement('template')
  template.innerHTML = marked.parse(linkedText)
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
  return markdown.replace(
    /\[((?:\d{1,2}:)?\d{1,2}:\d{2})(?: in [^\]]+)?\](?!\()/g,
    (match, timestamp) => {
      const parts = timestamp.split(':').map(Number)
      const seconds =
        parts.length === 3
          ? parts[0] * 3600 + parts[1] * 60 + parts[2]
          : parts[0] * 60 + parts[1]
      return `${match}(#video-t=${seconds})`
    },
  )
}
