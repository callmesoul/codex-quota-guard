/** Escapes text for safe insertion into an HTML string. */
export function esc(value: unknown): string {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** Inline markdown: code spans, bold, italic, links. */
export function inlineMd(source: unknown): string {
  let out = esc(source)
  out = out.replace(/`([^`]+)`/g, (_m, code: string) => `<code>${code}</code>`)
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  out = out.replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_m, text: string, url: string) =>
      `<a href="${url}" target="_blank" rel="noreferrer">${text}</a>`,
  )
  return out
}

const BLOCK_START = /^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+[.)]\s|>\s)/

/**
 * Minimal block markdown renderer for HANDOFF.md. Deliberately small: the
 * handoff template only uses headings, lists, tables, fences and quotes.
 */
export function renderMd(source: unknown): string {
  const clean = String(source || '')
    .replace(/\r\n/g, '\n')
    .replace(/<!--[\s\S]*?-->/g, '')
  const lines = clean.split('\n')
  const out: string[] = []
  let index = 0
  let listTag: 'ul' | 'ol' | null = null

  const closeList = () => {
    if (listTag) {
      out.push(`</${listTag}>`)
      listTag = null
    }
  }

  while (index < lines.length) {
    const line = lines[index]

    const fence = line.match(/^```(\w*)\s*$/)
    if (fence) {
      closeList()
      const lang = fence[1]
      const body: string[] = []
      index += 1
      while (index < lines.length && !/^```/.test(lines[index])) {
        body.push(lines[index])
        index += 1
      }
      index += 1
      out.push(`<pre><code data-lang="${esc(lang)}">${esc(body.join('\n'))}</code></pre>`)
      continue
    }

    if (/^#{1,6}\s/.test(line)) {
      closeList()
      const level = line.match(/^#+/)![0].length
      const tag = level <= 2 ? 'h2' : level === 3 ? 'h3' : 'h4'
      out.push(`<${tag}>${inlineMd(line.replace(/^#+\s*/, ''))}</${tag}>`)
      index += 1
      continue
    }

    if (
      /^\s*\|/.test(line) &&
      index + 1 < lines.length &&
      /^\s*\|[\s:\-|]+\|?\s*$/.test(lines[index + 1])
    ) {
      closeList()
      const rows: string[] = []
      while (index < lines.length && /^\s*\|/.test(lines[index])) {
        rows.push(lines[index])
        index += 1
      }
      const cells = (row: string) =>
        row
          .trim()
          .replace(/^\|/, '')
          .replace(/\|$/, '')
          .split('|')
          .map((cell) => cell.trim())
      out.push(
        '<table><thead><tr>' +
          cells(rows[0])
            .map((cell) => `<th>${inlineMd(cell)}</th>`)
            .join('') +
          '</tr></thead><tbody>' +
          rows
            .slice(2)
            .map(
              (row) =>
                `<tr>${cells(row)
                  .map((cell) => `<td>${inlineMd(cell)}</td>`)
                  .join('')}</tr>`,
            )
            .join('') +
          '</tbody></table>',
      )
      continue
    }

    const bullet = line.match(/^\s*([-*+])\s+(.*)$/)
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (bullet || ordered) {
      const want = bullet ? 'ul' : 'ol'
      if (listTag !== want) {
        closeList()
        out.push(`<${want}>`)
        listTag = want
      }
      out.push(`<li>${inlineMd(bullet ? bullet[2] : ordered![1])}</li>`)
      index += 1
      continue
    }

    if (/^\s*>\s?/.test(line)) {
      closeList()
      const body: string[] = []
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        body.push(lines[index].replace(/^\s*>\s?/, ''))
        index += 1
      }
      out.push(`<blockquote>${inlineMd(body.join(' '))}</blockquote>`)
      continue
    }

    if (/^\s*$/.test(line)) {
      closeList()
      index += 1
      continue
    }

    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      closeList()
      out.push('<hr>')
      index += 1
      continue
    }

    closeList()
    const paragraph = [line]
    index += 1
    while (
      index < lines.length &&
      !/^\s*$/.test(lines[index]) &&
      !BLOCK_START.test(lines[index])
    ) {
      paragraph.push(lines[index])
      index += 1
    }
    out.push(`<p>${paragraph.map(inlineMd).join('<br>')}</p>`)
  }

  closeList()
  return out.join('\n')
}
