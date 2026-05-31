// Extracts the article via Readability and converts the resulting HTML to Markdown.
// Returned to background.js via the message channel.

(function () {
  function htmlToMarkdown(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    return walk(doc.body).trim().replace(/\n{3,}/g, "\n\n");
  }

  function escapeMd(s) {
    return s.replace(/([\\`*_{}\[\]()#+\-.!>|])/g, "\\$1");
  }

  function walk(node, ctx) {
    ctx = ctx || { listDepth: 0 };
    let out = "";
    for (const child of node.childNodes) {
      out += render(child, ctx);
    }
    return out;
  }

  function render(node, ctx) {
    if (node.nodeType === Node.TEXT_NODE) {
      // Collapse whitespace inside text nodes.
      return node.nodeValue.replace(/\s+/g, " ");
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return "";

    const tag = node.tagName.toLowerCase();

    switch (tag) {
      case "h1": return "\n\n# " + inline(node) + "\n\n";
      case "h2": return "\n\n## " + inline(node) + "\n\n";
      case "h3": return "\n\n### " + inline(node) + "\n\n";
      case "h4": return "\n\n#### " + inline(node) + "\n\n";
      case "h5": return "\n\n##### " + inline(node) + "\n\n";
      case "h6": return "\n\n###### " + inline(node) + "\n\n";

      case "p":   return "\n\n" + inline(node).trim() + "\n\n";
      case "br":  return "  \n";
      case "hr":  return "\n\n---\n\n";

      case "strong":
      case "b":   return "**" + inline(node) + "**";
      case "em":
      case "i":   return "*" + inline(node) + "*";
      case "code":
        if (node.parentElement && node.parentElement.tagName.toLowerCase() === "pre") {
          return node.textContent;
        }
        return "`" + node.textContent + "`";
      case "pre": {
        const code = node.textContent.replace(/\n+$/, "");
        return "\n\n```\n" + code + "\n```\n\n";
      }
      case "blockquote": {
        const inner = walk(node, ctx).trim().replace(/\n{2,}/g, "\n\n");
        return "\n\n" + inner.split("\n").map(l => l ? "> " + l : ">").join("\n") + "\n\n";
      }
      case "a": {
        const href = node.getAttribute("href") || "";
        const text = inline(node).trim() || href;
        if (!href) return text;
        return "[" + text + "](" + href + ")";
      }
      case "img": {
        const src = node.getAttribute("src") || "";
        const alt = node.getAttribute("alt") || "";
        if (!src) return "";
        return "![" + alt + "](" + src + ")";
      }
      case "ul":
      case "ol": {
        ctx.listDepth++;
        const items = Array.from(node.children).filter(c => c.tagName.toLowerCase() === "li");
        const ordered = tag === "ol";
        const indent = "  ".repeat(ctx.listDepth - 1);
        const lines = items.map((li, i) => {
          const marker = ordered ? (i + 1) + ". " : "- ";
          // Render the li content; nested lists will recurse and add their own indents.
          const body = walk(li, ctx).trim().replace(/\n+/g, "\n" + indent + "  ");
          return indent + marker + body;
        });
        ctx.listDepth--;
        return "\n\n" + lines.join("\n") + "\n\n";
      }
      case "li":  return inline(node); // handled by ul/ol
      case "figure":
      case "figcaption":
      case "div":
      case "section":
      case "article":
        return walk(node, ctx);
      case "script":
      case "style":
      case "noscript":
        return "";
      default:
        return walk(node, ctx);
    }
  }

  function inline(node) {
    // Render children, but treat as inline (no surrounding block newlines).
    let out = "";
    for (const child of node.childNodes) {
      out += render(child, { listDepth: 0 });
    }
    return out.replace(/\s+/g, " ").trim();
  }

  function slugify(s, max) {
    return (s || "untitled")
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, max || 60) || "untitled";
  }

  function isoTimestamp(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return d.getUTCFullYear() + "-" +
      pad(d.getUTCMonth() + 1) + "-" +
      pad(d.getUTCDate()) + "T" +
      pad(d.getUTCHours()) + ":" +
      pad(d.getUTCMinutes()) + ":" +
      pad(d.getUTCSeconds()) + "Z";
  }

  function filenameTimestamp(d) {
    return isoTimestamp(d).replace(/[:.]/g, "-");
  }

  function escapeYaml(s) {
    if (!s) return "";
    return String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function buildFrontmatter(meta) {
    const lines = ["---"];
    lines.push(`title: "${escapeYaml(meta.title)}"`);
    lines.push(`source: "${escapeYaml(meta.url)}"`);
    if (meta.byline)   lines.push(`author: "${escapeYaml(meta.byline)}"`);
    if (meta.siteName) lines.push(`site: "${escapeYaml(meta.siteName)}"`);
    lines.push(`clipped: "${meta.clipped}"`);
    if (meta.tags && meta.tags.length) {
      const inner = meta.tags.map(t => `"${escapeYaml(t)}"`).join(", ");
      lines.push(`tags: [${inner}]`);
    }
    lines.push("---");
    return lines.join("\n");
  }

  function extractMetaTags() {
    const out = new Set();
    const push = (s) => {
      if (!s) return;
      String(s)
        .split(/[,;|]/)
        .map(t => t.trim())
        .filter(Boolean)
        .filter(t => t.length <= 64)
        .forEach(t => out.add(t));
    };
    // Common spots authors put tags / keywords.
    document.querySelectorAll('meta[name="keywords"]').forEach(m => push(m.content));
    document.querySelectorAll('meta[name="news_keywords"]').forEach(m => push(m.content));
    document.querySelectorAll('meta[property="article:tag"]').forEach(m => push(m.content));
    document.querySelectorAll('meta[property="og:article:tag"]').forEach(m => push(m.content));
    document.querySelectorAll('a[rel~="tag"]').forEach(a => push(a.textContent));
    return [...out];
  }

  function mergeTags(userTags, autoTags) {
    const seen = new Set();
    const out = [];
    for (const tag of [...(userTags || []), ...(autoTags || [])]) {
      const key = String(tag).toLowerCase();
      if (!key || seen.has(key)) continue;
      seen.add(key);
      out.push(String(tag));
    }
    return out;
  }

  function extract() {
    if (typeof Readability === "undefined") {
      return { error: "Readability not loaded" };
    }
    let article;
    try {
      const documentClone = document.cloneNode(true);
      article = new Readability(documentClone).parse();
    } catch (e) {
      return { error: "Readability failed: " + e.message };
    }
    if (!article) {
      return { error: "Could not extract readable content from this page." };
    }

    const now = new Date();
    const title = (article.title || document.title || "Untitled").trim();
    const userTags = Array.isArray(window.__clipshipUserTags) ? window.__clipshipUserTags : [];
    const meta = {
      title,
      url: location.href,
      byline: (article.byline || "").trim(),
      siteName: (article.siteName || "").trim(),
      clipped: isoTimestamp(now),
      tags: mergeTags(userTags, extractMetaTags()),
    };

    const body = htmlToMarkdown(article.content || "");
    const content = buildFrontmatter(meta) + "\n\n" + body + "\n";
    const filename = `${filenameTimestamp(now)}-${slugify(title)}.md`;

    return { filename, content, title };
  }

  // Run once on injection and return the result via the bridge.
  window.__clipshipResult = extract();
})();
