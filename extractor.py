"""
extractor.py — fetch a chapter page and pull clean text out of it.

Two problems make this harder than it looks, and both are why a naive
``response.text`` gives the client garbage:

1. **The site may render in JavaScript.** If the chapter text is absent from the
   initial HTML, no amount of BeautifulSoup will find it. The client's brief asks
   about exactly this, so ``probe()`` reports which case applies rather than
   guessing and silently returning a login page.

2. **Chapter text is not the page text.** Every WTR-Lab chapter sits inside a
   navigation header, a sidebar, a comment thread, and a footer. Returning the
   whole page is technically "extraction" and practically useless.

``extract_chapter`` therefore scores candidate containers and picks the one that
most looks like a chapter, and returns an honest failure when it cannot.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import aiohttp
from bs4 import BeautifulSoup, Tag

from url_guard import ResolvedURL, UnsafeURL, validate

# Tags whose entire subtree is chrome, not content.
CHROME_TAGS = frozenset(
    {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "iframe", "svg"}
)

# Class/id fragments that mark a block as navigation or advertising.
CHROME_MARKERS = (
    "nav", "menu", "sidebar", "side-bar", "comment", "disqus", "advert", "ads-", "banner",
    "footer", "header", "breadcrumb", "social", "share", "related", "popup", "modal",
    "cookie", "newsletter", "subscribe", "widget", "meta-info", "pagination",
)

# Class/id fragments that mark a block as the actual chapter.
CONTENT_MARKERS = (
    "chapter", "chapter-content", "entry-content", "post-content", "article-body",
    "content-body", "text-content", "story", "novel", "main-content", "read-content",
)

# Selectors tried in order when a site is well enough behaved to name its
# content container. Cheap, explicit, and more reliable than scoring.
PREFERRED_SELECTORS = (
    "div.chapter-content", "div.entry-content", "div.post-content",
    "article", "div.article-body", "div.content", "main",
)

MIN_CHARS = 200          # below this it is a stub, a login wall, or an error page
MIN_PARAGRAPHS = 2       # a real chapter has more than one paragraph
FETCH_TIMEOUT = 20.0
USER_AGENT = "WTRChapterBot/1.0 (+https://wtr-lab.example; contact via Telegram)"

# Chapter numbers as they appear in WTR-Lab and most novel sites: "Chapter 12",
# "CHAPTER 12 - Title", "第12章". Captured, not required.
CHAPTER_NUMBER = re.compile(
    r"(?:chapter|chap\.?|ch\.?)\s*[.:]?\s*(\d+[a-z]?)", re.IGNORECASE
)
CHAPTER_TITLE = re.compile(
    r"^\s*(?:chapter|chap\.?|ch\.?)\s*[.:]?\s*\d+[a-z]?\s*[.:\-–—]?\s*(.*)$",
    re.IGNORECASE,
)
# Anything that reads like a price, a counter, or a date is not prose.
_WS = re.compile(r"[ \t ]+")
_MULTI_NL = re.compile(r"\n{3,}")


class ExtractionError(RuntimeError):
    """Raised when a page could not be turned into a usable chapter."""


@dataclass
class Chapter:
    """One extracted chapter, ready to write to a .txt file."""

    number: str | None = None
    title: str | None = None
    text: str = ""
    url: str = ""
    word_count: int = 0
    paragraphs: int = 0
    method: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        """A filename that is safe on Windows, Termux, and Linux alike."""
        if self.number and self.title:
            raw = f"chapter_{self.number}_{self.title}"
        elif self.number:
            raw = f"chapter_{self.number}"
        elif self.title:
            raw = self.title
        else:
            raw = "chapter"
        # Windows forbids \ / : * ? " < > |. Being strict here means the same
        # name works on the client's Termux phone and their VPS.
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw)
        cleaned = _WS.sub(" ", cleaned).strip(" ._")
        return f"{(cleaned or 'chapter')[:80]}.txt"


def tidy(text: str) -> str:
    """Normalise whitespace without destroying paragraph structure."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("", "")
    lines = [_WS.sub(" ", ln).rstrip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return _MULTI_NL.sub("\n\n", "\n".join(out)).strip()


def _ident(node: Tag) -> str:
    """Return a node's class and id as one lowercase string.

    ``node.get("class")`` returns a *list* in BeautifulSoup 4.13+ and a *string*
    in older versions, so this handles both. Calling ``str()`` on the value
    directly produces ``["nav", "menu"]``, which still substring-matches and
    silently misbehaves on anything with a bracket in the name.
    """
    parts: list[str] = []
    for attr in ("class", "id"):
        value = node.get(attr)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        else:
            parts.append(str(value))
    return " ".join(p for p in parts if p).lower()


def _looks_like_chrome(node: Tag) -> bool:
    ident = _ident(node)
    return any(marker in ident for marker in CHROME_MARKERS)


def _looks_like_content(node: Tag) -> bool:
    ident = _ident(node)
    return any(marker in ident for marker in CONTENT_MARKERS)


def _score_container(node: Tag) -> int:
    """Rank a container by how much it looks like chapter prose.

    Length dominates because a chapter is long, but a long navigation menu can
    also be long, so named-content markers add weight and explicit chrome
    markers subtract. Blocks under MIN_CHARS are not candidates at all.
    """
    if _looks_like_chrome(node):
        return -1
    text = node.get_text("\n", strip=True)
    if len(text) < MIN_CHARS:
        return -1
    paragraphs = node.find_all("p")
    if len(paragraphs) < MIN_PARAGRAPHS:
        return -1
    if not paragraphs and not _looks_like_content(node):
        return -1

    score = len(text)
    if _looks_like_content(node):
        score += 2000
    if node.name in ("article", "main"):
        score += 1000
    if node.find(["h1", "h2"]):
        score += 300
    # Long unbroken runs are usually a base64 blob or a minified script, not prose.
    longest = max((len(w) for w in text.split()), default=0)
    if longest > 60:
        score -= 2000
    return score


def _strip_chrome(soup: BeautifulSoup) -> None:
    for tag in soup(list(CHROME_TAGS)):
        tag.decompose()
    for node in soup.find_all(attrs={"aria-hidden": "true"}):
        node.decompose()


def _title_from(soup: BeautifulSoup, fallback: str) -> str | None:
    for selector in ("h1.entry-title", "h1.chapter-title", "article h1", "h1"):
        node = soup.select_one(selector)
        if node:
            text = tidy(node.get_text(" ", strip=True))
            if text:
                return text
    if soup.title and soup.title.string:
        raw = tidy(soup.title.string)
        m = CHAPTER_TITLE.match(raw)
        if m and m.group(1):
            return m.group(1).strip()
    return fallback or None


def parse_chapter(html: str, url: str) -> Chapter:
    """Turn a fetched page into a Chapter, or explain why it could not."""
    soup = BeautifulSoup(html, "html.parser")
    title = _title_from(soup, fallback="")
    _strip_chrome(soup)

    container: Tag | None = None
    method = ""
    best = 0

    # Named selectors first: when a site tells us where the content is, that
    # beats any heuristic.
    for selector in PREFERRED_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        text = node.get_text("\n", strip=True)
        if len(text) >= MIN_CHARS:
            container, method, best = node, f"selector {selector}", len(text)
            break

    # Otherwise score every plausible block and take the best one.
    if container is None:
        scored = []
        for node in soup.find_all(["div", "section", "article", "main"]):
            score = _score_container(node)
            if score > 0:
                scored.append((score, node))

        if scored:
            # A wrapper div often contains both the chapter *and* the navigation
            # column, so it scores highest purely by summing their lengths. The
            # fix is to prefer a descendant over any ancestor that has scored
            # higher, which keeps the chapter text without the chrome.
            scored.sort(key=lambda pair: pair[0], reverse=True)
            best_score, best_node = scored[0]
            for score, node in scored[1:]:
                if best_node in node.parents:
                    # An ancestor of the current best scored higher: the current
                    # best is the tighter, more correct container.
                    best_score, best_node = score, node
                    break
            container = best_node
            method = "scored container"

    if container is None:
        raise ExtractionError(
            "no chapter container found - the page is either JavaScript-rendered "
            "or the layout changed. Run the probe to find out which."
        )

    text = tidy(container.get_text("\n", strip=True))
    paragraphs = container.find_all("p")
    if len(text) < MIN_CHARS:
        raise ExtractionError(
            f"found a container but it held only {len(text)} characters, "
            "which is usually a login wall or a placeholder rather than a chapter"
        )

    number = None
    m = CHAPTER_NUMBER.search(title or "") or CHAPTER_NUMBER.search(text[:200])
    if m:
        number = m.group(1)

    clean_title = title or None
    if clean_title:
        t = CHAPTER_TITLE.match(clean_title)
        if t and t.group(1):
            clean_title = t.group(1).strip()
        elif t and not t.group(1):
            clean_title = None

    notes: list[str] = []
    if not paragraphs:
        notes.append("no <p> tags; text may run together")

    return Chapter(
        number=number,
        title=clean_title,
        text=text,
        url=url,
        word_count=len(text.split()),
        paragraphs=len(paragraphs),
        method=method,
        notes=notes,
    )


async def _fetch(session: aiohttp.ClientSession, resolved: ResolvedURL, url: str) -> str:
    """Fetch a page that has already been validated, re-validating redirects.

    A 302 to a private address is the standard way around a URL check, so every
    hop is validated again rather than trusted.
    """
    current = url
    for _ in range(5):
        resolved = validate(current)
        async with session.get(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise ExtractionError("redirect with no Location header")
                current = str(aiohttp.client_reqrep.URL(current).join(aiohttp.client_reqrep.URL(location)))
                continue
            if resp.status >= 400:
                raise ExtractionError(f"page returned HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                raise ExtractionError(f"expected HTML but the server sent '{ctype}'")
            return await resp.text(errors="replace")
    raise ExtractionError("too many redirects (possible redirect loop)")


async def fetch_chapter(url: str, session: aiohttp.ClientSession | None = None) -> Chapter:
    """Validate, fetch, and extract. The normal entry point."""
    resolved = validate(url)  # UnsafeURL surfaces a message safe to show the user
    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        html = await _fetch(session, resolved, url)
    finally:
        if owns_session:
            await session.close()
    return parse_chapter(html, resolved.url)


async def probe(url: str) -> dict:
    """Answer the client's question: does this site need a browser?

    Compares the text in the initial HTML against the length a real chapter
    needs. If the HTML is thin, the content is almost certainly rendered by
    JavaScript and aiohttp alone will not be enough, which is exactly the
    question the brief asked.
    """
    resolved = validate(url)
    started = time.monotonic()
    async with aiohttp.ClientSession() as session:
        html = await _fetch(session, resolved, url)

    soup = BeautifulSoup(html, "html.parser")
    _strip_chrome(soup)
    visible = len(soup.get_text(" ", strip=True))

    result = {
        "url": resolved.url,
        "ip": resolved.ip,
        "html_bytes": len(html),
        "visible_chars": visible,
        "rendered_by_javascript": visible < MIN_CHARS,
        "has_chapter_marker": bool(
            soup.select_one("div.chapter-content, div.entry-content, article")
        ),
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    result["verdict"] = (
        "server-rendered: aiohttp + BeautifulSoup is sufficient"
        if not result["rendered_by_javascript"]
        else "JavaScript-rendered: aiohttp alone will return an empty page. "
             "A headless browser (Playwright) is required for this URL."
    )
    return result
