// A small, safe Markdown subset for chat messages (no imports; the admin UI and the tests load it as is).
//
// Everything is HTML-escaped first; only the tags below are ever produced, never one taken from the text:
//   blocks: paragraphs (a blank line), line breaks inside them, "- " / "* " / "+ " and "1. " lists,
//           ``` fenced code, "# " headings (shown as a bold line, not a big title: it is a chat)
//   inline: **bold**, *italic* / _italic_ (not inside words: reply_handle stays as typed), `code`,
//           [text](https://…) links (http and https only; they open in a new tab)
// Anything else (tables, images, HTML, nested lists) stays as the text that was typed.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

const UL = /^\s{0,3}[-*+]\s+(.*)$/;
const OL = /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/;
const FENCE = /^\s{0,3}```/;
const HEAD = /^ {0,3}#{1,6}[ \t]/; // only a prefix test: the text is cut out by hand (a regex here backtracked cubically)
// Every regex below is bounded or anchored so it cannot backtrack across a long line: an agent's message renders on
// the owner's page, and one crafted line must never stall it (tests time hostile lines of 8 KB and 100 KB).

// "## Title ##" -> "Title": leading hashes, then closing hashes only when a space precedes them
function headingText(line) {
  let t = line.trim();
  let i = 0;
  while (t[i] === "#") i += 1;
  t = t.slice(i).trim();
  let j = t.length;
  while (j > 0 && t[j - 1] === "#") j -= 1;
  if (j < t.length && (j === 0 || t[j - 1] === " " || t[j - 1] === "\t")) t = t.slice(0, j).trimEnd();
  return t;
}

// inline marks on one line of raw text
function inline(raw) {
  const kept = []; // links and code spans, set aside so no other mark reaches into them
  const keep = (html) => `\u0000${kept.push(html) - 1}\u0000`;
  // links first, their label shown as typed (no marks inside a link)
  let s = raw.replace(/\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)/g, (all, label, url) =>
    /^https?:\/\/\S+$/i.test(url)
      ? keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`)
      : all);
  s = s.replace(/`([^`\n]{1,2000})`/g, (_, code) => keep(`<code>${esc(code)}</code>`));
  s = esc(s);
  s = s.replace(/\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s(])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w/])/g, "$1<em>$2</em>"); // not in paths or URLs
  // a code span may hold a link placeholder: resolve until none is left (each round removes one level)
  for (let round = 0; round <= kept.length && s.includes("\u0000"); round += 1) {
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => kept[Number(i)] ?? "");
  }
  return s;
}

export function chatMarkdown(text) {
  const lines = String(text ?? "").replace(/\u0000/g, "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (FENCE.test(line)) {
      flush();
      const body = [];
      for (i += 1; i < lines.length && !FENCE.test(lines[i]); i += 1) body.push(lines[i]);
      i += 1; // the closing fence (or the end of the message)
      out.push(`<pre><code>${esc(body.join("\n"))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flush(); i += 1; continue; }
    const head = HEAD.test(line) ? headingText(line) : "";
    if (head) { flush(); out.push(`<p class="md-h">${inline(head)}</p>`); i += 1; continue; }
    const kind = UL.test(line) ? "ul" : OL.test(line) ? "ol" : null;
    if (kind) {
      flush();
      const re = kind === "ul" ? UL : OL;
      const first = kind === "ol" ? Number(line.match(OL)[1]) : 1;
      const items = [];
      for (; i < lines.length && re.test(lines[i]); i += 1) {
        const m = lines[i].match(re);
        items.push(`<li>${inline(kind === "ul" ? m[1] : m[2])}</li>`);
      }
      out.push(`<${kind}${first !== 1 ? ` start="${first}"` : ""}>${items.join("")}</${kind}>`);
      continue;
    }
    para.push(line);
    i += 1;
  }
  flush();
  return out.join("");
}

// The same text without its marks, for one-line previews (the folder list, reply quotes). Bounded regexes only.
export function chatPlain(text) {
  return String(text ?? "").replace(/\u0000/g, "")
    .replace(/^\s{0,3}```.*$/gm, "")
    .replace(/^\s{0,3}#{1,6}[ \t]+/gm, "")
    .replace(/^\s{0,3}[-*+][ \t]+/gm, "")
    .replace(/\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)/g, "$1")
    .replace(/`([^`\n]{1,2000})`/g, "$1")
    .replace(/\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*/g, "$1")
    .replace(/(^|[\s(])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w/])/g, "$1$2")
    .replace(/(^|[^\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])/g, "$1$2")
    .replace(/\s+/g, " ").trim();
}
