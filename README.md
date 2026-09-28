WTR-Lab Chapter Bot — Python Telegram bot that turns a WTR-Lab chapter URL
into a clean .txt file. Runs on Android Termux and on a Linux VPS.

WHAT IT DOES
  /chapter <url>  fetch one chapter, return a .txt file
  /probe <url>    report whether a page needs a real browser or not

Built with aiogram 3, aiohttp and BeautifulSoup. Python 3.10+.

--------------------------------------------------------------------------
WHY THIS IS NOT THE OBVIOUS VERSION
--------------------------------------------------------------------------

Three things in the brief are harder than they look, and I have handled all
three rather than leaving them for you to discover.

1. THE SSRF PROBLEM (this is the important one)

   The bot fetches a URL supplied by whoever messages it. Left unguarded, that
   is a server-side request forgery gadget with a text file attached: someone
   sends the bot http://169.254.169.254/latest/meta-data/ and if it runs on
   your VPS with cloud credentials attached, it reads them and mails them over
   Telegram.

   url_guard.py blocks that before any connection is opened. It checks the
   scheme, resolves the hostname, and refuses any private, loopback, link-local
   or reserved address on IPv4 or IPv6. It checks every address a name resolves
   to, not just the first, because a name that returns one public and one
   private address is a rebinding attempt. It refuses ports that exist to
   expose something internal (22, 3306, 6379, 27017 and friends). And it
   re-validates on every redirect hop, because "public URL that 302s to
   10.0.0.1" is the standard way around a URL check.

   There are 20 tests for this file alone, including a specific test for the
   cloud metadata endpoint.

2. MOST EXTRACTORS RETURN GARBAGE

   A WTR-Lab chapter sits inside a nav bar, a sidebar, a comment thread, an
   ad slot and a footer. Naive BeautifulSoup returns all of it, which is
   technically extraction and practically useless.

   So the extractor scores candidate containers and picks the one that most
   looks like prose: length, paragraph count, whether it is named like content,
   and a penalty for long unbroken runs that indicate a minified script rather
   than a chapter. Named selectors are tried first, because when a site tells
   you where the content is, that beats any heuristic. When a wrapper div
   contains both the chapter and the navigation, the tighter descendant wins
   rather than the wrapper that just summed both.

   There is a test for exactly that case.

3. IT MAY NOT NEED AN HTTP CLIENT AT ALL

   You asked in the brief whether the chapter pages render content directly in
   HTML or need JavaScript. I built /probe to answer that instead of guessing:

     /probe <url>
     -> HTML size, visible character count, fetch time
     -> "server-rendered: aiohttp is sufficient"
        or
        -> "JavaScript-rendered: Playwright is required for this URL"

   It is deliberately built to tell you when you need a browser. If a page
   renders in JavaScript, aiohttp returns an empty shell and you get a .txt
   file full of nothing. Better to know that before you run it on 200 chapters.

   When it detects that case the extractor raises a specific error naming
   Playwright, rather than quietly returning an empty file that looks like a
   successful run.

--------------------------------------------------------------------------
RUNNING IT
--------------------------------------------------------------------------

1. GET A TOKEN

   Message @BotFather on Telegram -> /newbot -> follow the prompts -> copy the
   token. It looks like 123456789:AAH... Do not commit it.

2. INSTALL

   Linux VPS (Debian/Ubuntu):
     sudo apt install python3 python3-venv
     python3 -m venv .venv && source .venv/bin/activate

   Android Termux:
     pkg install python
     python -m venv .venv && source .venv/bin/activate

   Then, from the project directory:
     pip install -r requirements.txt

3. SET THE TOKEN

     export TELEGRAM_BOT_TOKEN='123456789:AAH-your-token'
     export DELIVERY_DIR='./deliveries'     # optional, defaults to this

4. RUN

     python bot.py

   It logs a startup line and then polls Telegram.

--------------------------------------------------------------------------
RUNNING IT AS A SERVICE
--------------------------------------------------------------------------

VPS, systemd. Create /etc/systemd/system/wtrl-bot.service:

  [Unit]
  Description=WTR-Lab chapter bot
  After=network-online.target

  [Service]
  Type=simple
  User=friday
  WorkingDirectory=/opt/wtrl
  EnvironmentFile=/opt/wtrl/.env
  ExecStart=/opt/wtrl/.venv/bin/python /opt/wtrl/bot.py
  Restart=always
  RestartSec=10
  # The bot only needs to reach the network and write its deliveries folder.
  NoNewPrivileges=true
  PrivateTmp=true
  ProtectSystem=strict
  ProtectHome=read-only
  ReadWritePaths=/opt/wtrl/deliveries

  [Install]
  WantedBy=multi-user.target

Then:

  sudo systemctl daemon-reload
  sudo systemctl enable --now wtrl-bot
  sudo journalctl -u wtrl-bot -f

Termux does not have systemd. Use termux-wake-lock plus a plain loop:

  termux-wake-lock
  while true; do .venv/bin/python bot.py; sleep 5; done

Put that in a script and run it with nohup so it survives Termux closing.
Note that Android may kill it under memory pressure; the wake lock plus the
restart loop is what makes it stick.

--------------------------------------------------------------------------
API DEVELOPMENT / STRUCTURE
--------------------------------------------------------------------------

  url_guard.py    no network. Decides whether a URL may be fetched.
  extractor.py    fetches (aiohttp) and parses (BeautifulSoup).
  bot.py          Telegram interface only. No parsing logic.

The split is deliberate: the risky part (url_guard) is pure and has no
dependencies beyond the standard library, so it is trivial to test and trivial
to review. bot.py contains no parsing, so a bug in extraction can never become
a message-handling bug. The fetch call pins the IP that url_guard validated
rather than re-resolving, which closes the DNS-rebinding window.

Errors are written for someone reading them on a phone. "That page is served
by JavaScript, so the text is not in the HTML" tells a user what to do next. A
traceback does not.

--------------------------------------------------------------------------
TESTS
--------------------------------------------------------------------------

  $ pip install pytest
  $ python -m pytest -q

  52 passed in 0.63s

No network access is required or performed. DNS is stubbed in the guard tests,
so the suite is deterministic and runs the same on your laptop and on the VPS.

Coverage by area:
  SSRF guard              20 tests  schemes, loopback, private ranges,
                                    link-local, IPv6, metadata endpoint,
                                    rebinding, ports, redirects, input limits
  extraction               24 tests  named selectors, scored fallback,
                                    wrapper-vs-descendant, login walls,
                                    JS-only pages, Windows-safe filenames,
                                    whitespace, prose sanity
  probe                     2 tests  server-rendered vs JS-rendered verdict
  bot logic                 6 tests  filename safety, chapter floor, tidy

--------------------------------------------------------------------------
FOLLOWING YOUR BRIEF, POINT BY POINT
--------------------------------------------------------------------------

  aiogram, URL-only input validation      done, plus SSRF guarding
  aiohttp + BeautifulSoup                  done
  browser fallback where required          /probe reports when it is
                                           required; I have not wired
                                           Playwright in because it does not
                                           appear necessary and adds weight.
                                           Say the word and it is a small
                                           addition behind the same interface.
  clean chapter extraction                 done, scored
  TXT generation                           done, Windows-safe names
  progress updates                         message is edited through each
                                           stage so the user sees movement
  commented source                         commented where the reasoning is
                                           non-obvious
  BotFather steps                          above
  Termux instructions                      above
  systemd service                          above
  environment setup                        .env.example

--------------------------------------------------------------------------
OPEN QUESTION I COULD NOT ANSWER FROM HERE
--------------------------------------------------------------------------

I have not been able to fetch a real WTR-Lab chapter page from this machine,
so the extraction heuristics are tuned against realistic fixture pages rather
than against your live HTML. Point /probe at one live chapter URL and send me
what it says. If it comes back "server-rendered" the current code handles it.
If it comes back "JavaScript-rendered" I will add the Playwright fallback
before you run this on real work, which is the right order to find that out in.

--------------------------------------------------------------------------
PRICE
--------------------------------------------------------------------------

600-1500 INR. Quoted at the top of your range rather than the bottom, because
the SSRF guard and the probe are not in your original spec and I would rather
be paid for them than have them look like padding.
