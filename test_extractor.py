"""
test_extractor.py — tests for chapter extraction.

The fixtures are deliberately awkward: a page wrapped in navigation and
comments, a login wall that looks like a chapter at first glance, a page whose
content is only in JavaScript. Those are the cases that produce garbage
output, and garbage output is worse than an error because the client does not
find out until they have shipped it.
"""

from __future__ import annotations

import re

import pytest

from extractor import (
    ExtractionError,
    Chapter,
    parse_chapter,
    probe,
    tidy,
)

LOREM = (
    "The rain had not stopped since the river crossing. Maren counted the "
    "lanterns along the causeway and lost track at the eleventh, which was "
    "the same number she had lost track at the night before."
)


def _paragraphs(n: int = 6) -> str:
    return "\n".join(f"<p>{LOREM} Paragraph {i}.</p>" for i in range(n))


# A realistic WTR-Lab-shaped page: heavy chrome, chapter inside.
CHAPTER_PAGE = f"""<!doctype html>
<html><head>
  <title>Chapter 12 The Drowned Road | WTR-Lab</title>
  <script>window.ads = {{slot: 1}};</script>
  <style>.ad {{ display:block }}</style>
</head>
<body>
  <nav class="site-navigation"><ul><li>Home</li><li>Novels</li><li>Chapters</li></ul></nav>
  <div class="ad banner-advert">BUY NOW 50% OFF</div>
  <article>
    <h1 class="entry-title">Chapter 12: The Drowned Road</h1>
    <div class="entry-content">
      {_paragraphs(8)}
    </div>
  </article>
  <div class="comments"><p>First!</p><p>Nice chapter</p></div>
  <div id="disqus_thread"></div>
  <footer>&copy; WTR-Lab</footer>
  <script>console.log("tracking")</script>
</body></html>"""


# Content is present but in an unlabelled div: only scoring can find it.
UNLABELLED_PAGE = f"""<!doctype html>
<html><body>
  <div id="wrapper">
    <div class="left-column">{'<p>Nav item</p>' * 40}</div>
    <div class="whatever-2024">
      {_paragraphs(10)}
    </div>
  </div>
</body></html>"""


# Looks like a page, contains no chapter: the login-wall case.
LOGIN_WALL = """<!doctype html>
<html><body>
  <h1>Members only</h1>
  <p>Please sign in to continue reading this chapter.</p>
  <form><input name="user"><input name="pass" type="password"></form>
</body></html>"""


EMPTY_PAGE = "<!doctype html><html><body></body></html>"


# --- the main path ---------------------------------------------------------

def test_extracts_chapter_with_number_and_title():
    ch = parse_chapter(CHAPTER_PAGE, "https://wtr-lab.example/ch12")
    assert ch.number == "12"
    assert "Drowned Road" in (ch.title or "")
    assert ch.word_count > 80
    assert ch.paragraphs >= 8


def test_strips_navigation_comments_and_scripts():
    ch = parse_chapter(CHAPTER_PAGE, "https://wtr-lab.example/ch12")
    text = ch.text.lower()
    # Chrome must not leak into the chapter.
    assert "buy now" not in text
    assert "nice chapter" not in text          # comment body
    assert "site-navigation" not in text       # nav markup
    assert "wtr-lab" not in ch.text.lower()    # footer
    assert "console.log" not in ch.text        # script body
    assert "tracking" not in ch.text.lower()   # tracking script
    assert "first!" not in ch.text.lower()     # first comment


def test_uses_preferred_selector_when_available():
    ch = parse_chapter(CHAPTER_PAGE, "https://wtr-lab.example/ch12")
    assert "entry-content" in ch.method


def test_finds_unlabelled_content_by_scoring():
    ch = parse_chapter(UNLABELLED_PAGE, "https://wtr-lab.example/x")
    assert "scored" in ch.method
    # The real chapter must win over the 40-paragraph nav column.
    assert "Paragraph 9" in ch.text
    assert "Nav item" not in ch.text


# --- the failure cases -----------------------------------------------------

def test_login_wall_raises_rather_than_returning_junk():
    with pytest.raises(ExtractionError):
        parse_chapter(LOGIN_WALL, "https://wtr-lab.example/ch12")


def test_empty_page_raises():
    with pytest.raises(ExtractionError):
        parse_chapter(EMPTY_PAGE, "https://wtr-lab.example/ch12")


def test_javascript_only_page_raises_with_the_right_message():
    """This is the case the client's brief asked about directly.

    A page that renders in the browser has no chapter text in the HTML, and the
    honest answer is to say so rather than return an empty .txt that looks like
    a successful run.
    """
    js_only = """<!doctype html><html><body>
      <div id="root"></div>
      <script>ReactDOM.render(<App/>, root)</script>
    </body></html>"""
    with pytest.raises(ExtractionError) as e:
        parse_chapter(js_only, "https://wtr-lab.example/ch12")
    assert "javascript" in str(e.value).lower()


# --- filenames -------------------------------------------------------------

@pytest.mark.parametrize("bad, forbidden", [
    ("Chapter 12: A/B?", "/"),
    ("Chapter 1 <script>", "<"),
    ('Chapter 2 "quoted"', '"'),
    ("Chapter 3: a|b", "|"),
])
def test_filename_is_windows_safe(bad, forbidden):
    """The client's target is Android Termux, but files get moved to Windows."""
    ch = Chapter(number=None, title=bad, text=LOREM)
    assert forbidden not in ch.filename
    assert not re.search(r'[\\/:*?"<>|]', ch.filename)
    assert ch.filename.endswith(".txt")


def test_filename_with_number_and_title():
    ch = Chapter(number="12", title="The Drowned Road", text=LOREM)
    assert ch.filename == "chapter_12_The Drowned Road.txt"


def test_filename_falls_back_when_title_blank():
    assert Chapter(title="   ", text=LOREM).filename == "chapter.txt"


def test_filename_is_length_bounded():
    ch = Chapter(title="x" * 500, text=LOREM)
    assert len(ch.filename) <= 84  # 80 chars + .txt


# --- text tidying ----------------------------------------------------------

def test_tidy_collapses_spaces_but_keeps_paragraphs():
    got = tidy("a   b\t\tc\r\n\r\n\r\n\r\nd")
    assert got == "a b c\n\nd"


def test_tidy_strips_nonbreaking_space():
    assert tidy("a b") == "a b"


def test_tidy_leaves_single_newlines():
    assert tidy("a\nb\nc") == "a\nb\nc"


# --- probe -----------------------------------------------------------------

@pytest.fixture
def public_page(monkeypatch):
    """Stub out the network so probe() can be tested offline.

    probe() calls validate() before fetching, and validate() does real DNS. The
    stub returns a known-public address for any host, which keeps these tests
    about the probe logic rather than about the resolver.
    """

    class _FakeDNS:
        def __call__(self, host, port, *args, **kwargs):
            import socket as s
            return [(s.AF_INET, s.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("url_guard.socket.getaddrinfo", _FakeDNS())
    return True


def test_probe_flags_server_rendered(public_page, monkeypatch):
    async def fake_fetch(session, resolved, url):
        return CHAPTER_PAGE

    monkeypatch.setattr("extractor._fetch", fake_fetch)
    import asyncio
    result = asyncio.run(probe("http://wtr-lab.example/ch12"))
    assert result["rendered_by_javascript"] is False
    assert "sufficient" in result["verdict"]
    assert result["has_chapter_marker"] is True


def test_probe_flags_javascript_rendered(public_page, monkeypatch):
    js_only = "<!doctype html><html><body><div id=root></div><script>boot()</script></body></html>"

    async def fake_fetch(session, resolved, url):
        return js_only

    monkeypatch.setattr("extractor._fetch", fake_fetch)
    import asyncio
    result = asyncio.run(probe("http://wtr-lab.example/ch12"))
    assert result["rendered_by_javascript"] is True
    assert "Playwright" in result["verdict"]


# --- the deliverable is real, not a stub -----------------------------------

def test_extracted_text_is_actually_prose():
    ch = parse_chapter(CHAPTER_PAGE, "https://wtr-lab.example/ch12")
    # Real words, real sentences, not markup and not filler.
    assert re.search(r"\bthe rain had not stopped\b", ch.text, re.IGNORECASE)
    assert "<" not in ch.text
    assert "{" not in ch.text
    assert len(ch.text.split()) > 80
