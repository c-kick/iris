// A small, safe Markdown subset for chat messages (no imports; the admin UI and the tests load it as is).
//
// Everything is HTML-escaped first; only the tags below are ever produced, never one taken from the text:
//   blocks: paragraphs (a blank line), line breaks inside them, "- " / "* " / "+ " and "1. " lists,
//           ``` fenced code, "# " headings (shown as a bold line, not a big title: it is a chat)
//   inline: **bold**, *italic* / _italic_ (not inside words: reply_handle stays as typed), `code`,
//           [text](https://…) links and bare http(s) addresses (http and https only; they open in a new tab)
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

// @Name as the server finds a mention (chat.py MENTION_RE): after a space, an opening bracket or quote, or at the start;
// not on a quoted line (starting with > or |), whose mentions are someone else's
const QUOTED = /^\s*[>|]/;
const MENTION = /(?<![^\s(\[{<"'])@([A-Za-z][A-Za-z0-9]{0,19})(?![A-Za-z0-9])/g;
// colour(name) -> the agent colour index of the member called name (0..15), or undefined: then it stays plain text
const mentionIndex = (colour, name) => {
  const ci = colour ? colour(name) : undefined;
  return Number.isInteger(ci) && ci >= 0 && ci < 16 ? ci : undefined;
};
const mentionHtml = (tag, ci, name) => `<${tag} class="mention" style="--who: var(--a${ci})">@${esc(name)}</${tag}>`;

const linkHtml = (url, label) => `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;

// inline marks on one line of raw text
function inline(raw, colour, quoted = QUOTED.test(raw)) { // quoted: decided on the whole line, as on the server
  const kept = []; // links and code spans, set aside so no other mark reaches into them
  const keep = (html) => `\u0000${kept.push(html) - 1}\u0000`;
  // links first, their label shown as typed (no marks inside a link)
  let s = raw.replace(/\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)/g, (all, label, url) =>
    /^https?:\/\/\S+$/i.test(url)
      ? keep(linkHtml(url, label))
      : all);
  // a code span that holds only a URL is a link too, still shown as code
  s = s.replace(/`([^`\n]{1,2000})`/g, (_, code) => keep(/^https?:\/\/[^\s<>"'`]+$/i.test(code)
    ? `<code>${linkHtml(code, code)}</code>` : `<code>${esc(code)}</code>`));
  // a bare http(s) address is a link; punctuation that ends the sentence is not part of it
  // (after a letter or digit it is part of a word, not an address; after "_" or "*" it is inside emphasis)
  s = s.replace(/(?<![A-Za-z0-9])https?:\/\/[^\s<>"'`\u0000]{1,2000}/gi, (url) => {
    let tail = "";
    const count = (c) => url.split(c).length - 1;
    // closing emphasis marks and sentence punctuation are not part of it; a ")" that closes a "(" in it is
    for (let k = 0; k < 16 && /[.,;:!?)\]*_]$/.test(url); k += 1) {
      if (url.endsWith(")") && count("(") >= count(")")) break;
      tail = url.slice(-1) + tail; url = url.slice(0, -1);
    }
    return /^https?:\/\/./i.test(url) ? keep(linkHtml(url, url)) + tail : url + tail;
  });
  // a mention of a member in its agent colour (links and code spans are set aside already, so none inside them)
  if (colour && !quoted) {
    s = s.replace(MENTION, (all, name) => {
      const ci = mentionIndex(colour, name);
      return ci === undefined ? all : keep(mentionHtml("span", ci, name));
    });
  }
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

export function chatMarkdown(text, colour) {
  const lines = String(text ?? "").replace(/\u0000/g, "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${para.map((l) => inline(l, colour)).join("<br>")}</p>`);
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
    if (head) { flush(); out.push(`<p class="md-h">${inline(head, colour, QUOTED.test(line))}</p>`); i += 1; continue; }
    const kind = UL.test(line) ? "ul" : OL.test(line) ? "ol" : null;
    if (kind) {
      flush();
      const re = kind === "ul" ? UL : OL;
      const first = kind === "ol" ? Number(line.match(OL)[1]) : 1;
      const items = [];
      for (; i < lines.length && re.test(lines[i]); i += 1) {
        const m = lines[i].match(re);
        items.push(`<li>${inline(kind === "ul" ? m[1] : m[2], colour, QUOTED.test(lines[i]))}</li>`);
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

// The composer's text, escaped, with each mention of a member marked in its agent colour: drawn behind the textarea,
// so it must keep every character where the textarea has it (no other marks, no added text).
export function chatMentionMarks(text, colour) {
  return String(text ?? "").split("\n").map((raw) => {
    if (QUOTED.test(raw)) return esc(raw);
    let out = "";
    let last = 0;
    for (const m of raw.matchAll(MENTION)) {
      const ci = mentionIndex(colour, m[1]);
      if (ci === undefined) continue;
      out += esc(raw.slice(last, m.index)) + mentionHtml("mark", ci, m[1]);
      last = m.index + m[0].length;
    }
    return out + esc(raw.slice(last));
  }).join("\n");
}
