"""Generate a story-driven, low-code presentation on using Claude Code.

Tone: conversational, visual, focused on the journey — not the trading strategy.
Output: claude-code-presentation.pptx in the project root.
"""
from pathlib import Path
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor


NAVY = RGBColor(0x1F, 0x2D, 0x4E)
DARK_GRAY = RGBColor(0x33, 0x33, 0x33)
MID_GRAY = RGBColor(0x6B, 0x6B, 0x6B)
LIGHT_GRAY = RGBColor(0xF5, 0xF5, 0xF5)
CODE_BG = RGBColor(0x27, 0x2B, 0x33)
CODE_FG = RGBColor(0xE0, 0xE4, 0xEA)
ACCENT = RGBColor(0x2E, 0x86, 0xAB)
GREEN = RGBColor(0x4C, 0xAF, 0x50)
RED = RGBColor(0xD9, 0x43, 0x3F)
AMBER = RGBColor(0xE6, 0xA1, 0x3A)
BADGE_BG = RGBColor(0xEC, 0xF3, 0xF9)


def title_bar(slide, title, subtitle=None):
    tb = slide.shapes.add_textbox(Inches(0.6), Inches(0.35), Inches(12.0), Inches(0.8))
    p = tb.text_frame.paragraphs[0]
    p.text = title
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = NAVY
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.6), Inches(1.15),
                                  Inches(1.2), Emu(40000))
    line.fill.solid(); line.fill.fore_color.rgb = ACCENT
    line.line.fill.background()
    if subtitle:
        sb = slide.shapes.add_textbox(Inches(0.6), Inches(1.25), Inches(12.0), Inches(0.5))
        p = sb.text_frame.paragraphs[0]
        p.text = subtitle
        p.font.size = Pt(15)
        p.font.italic = True
        p.font.color.rgb = MID_GRAY


def add_code_block(slide, left, top, width, height, code_lines):
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    box.fill.solid(); box.fill.fore_color.rgb = CODE_BG
    box.line.fill.background()
    tb = slide.shapes.add_textbox(left + Inches(0.2), top + Inches(0.15),
                                  width - Inches(0.4), height - Inches(0.3))
    tf = tb.text_frame
    tf.word_wrap = False
    for i, line in enumerate(code_lines):
        if i == 0: p = tf.paragraphs[0]
        else: p = tf.add_paragraph()
        if isinstance(line, tuple):
            color, text = line
            p.text = text
            p.font.color.rgb = color
        else:
            p.text = line
            p.font.color.rgb = CODE_FG
        p.font.name = "Menlo"
        p.font.size = Pt(12)


def add_callout(slide, left, top, width, height, text, color=AMBER):
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    box.fill.solid(); box.fill.fore_color.rgb = RGBColor(0xFF, 0xF8, 0xE8)
    box.line.color.rgb = color; box.line.width = Pt(1.25)
    tb = slide.shapes.add_textbox(left + Inches(0.2), top + Inches(0.1),
                                  width - Inches(0.4), height - Inches(0.2))
    p = tb.text_frame.paragraphs[0]
    p.text = text
    p.font.size = Pt(14)
    p.font.italic = True
    p.font.color.rgb = DARK_GRAY


def add_body_text(slide, left, top, width, height, paragraphs, size=16):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    for i, item in enumerate(paragraphs):
        if isinstance(item, tuple):
            level, text = item
        else:
            level, text = 0, item
        if i == 0: p = tf.paragraphs[0]
        else: p = tf.add_paragraph()
        prefix = "• " if level == 0 else "   – "
        p.text = prefix + text if text else ""
        p.level = level
        p.font.size = Pt(size) if level == 0 else Pt(size - 2)
        p.font.color.rgb = DARK_GRAY if level == 0 else MID_GRAY
        p.space_after = Pt(5)


def add_plain_text(slide, left, top, width, height, text, size=16, bold=False,
                   color=DARK_GRAY, italic=False, align=PP_ALIGN.LEFT, name=None):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.alignment = align
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.italic = italic
    p.font.color.rgb = color
    if name:
        p.font.name = name


def add_badge(slide, left, top, width, height, text, bg=BADGE_BG, fg=NAVY):
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    box.fill.solid(); box.fill.fore_color.rgb = bg
    box.line.color.rgb = fg; box.line.width = Pt(0.75)
    tb = slide.shapes.add_textbox(left, top + Inches(0.08), width, height - Inches(0.16))
    p = tb.text_frame.paragraphs[0]
    p.text = text
    p.alignment = PP_ALIGN.CENTER
    p.font.size = Pt(13); p.font.bold = True; p.font.color.rgb = fg


def make_slide(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])


# ---------- slides ----------

def slide_title(prs):
    s = make_slide(prs)
    add_plain_text(s, Inches(0.6), Inches(2.1), Inches(12), Inches(1.6),
                   "From Zero to a Cloud-Deployed Trading Bot",
                   size=44, bold=True, color=NAVY)
    add_plain_text(s, Inches(0.6), Inches(3.9), Inches(12), Inches(0.8),
                   "Eight days of actually building — with Claude Code as my co-pilot.",
                   size=20, color=MID_GRAY, italic=True)
    add_plain_text(s, Inches(0.6), Inches(6.7), Inches(12), Inches(0.4),
                   "April 2026  •  McCombs MBA", size=12, color=MID_GRAY)


def slide_installation(prs):
    s = make_slide(prs)
    title_bar(s, "Step 0: Just installing it felt like a decision",
              "Before any trading — a choice of tool.")
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(7.2), Inches(5.0), [
        "One-line install from Anthropic's site. Ran in my terminal.",
        "Claude Code isn't a chatbot — it's an agent that lives in your shell.",
        "It reads your files, runs commands, SSHs into servers, manages cloud infra.",
        "My first instinct: open it wherever I happened to be. That turned out to matter.",
    ], size=16)
    add_code_block(s, Inches(8.0), Inches(1.8), Inches(4.8), Inches(2.6), [
        (RGBColor(0x8F, 0xD6, 0xFF), "$ claude"),
        "",
        (CODE_FG, "Welcome to Claude Code."),
        (CODE_FG, "How can I help you today?"),
        "",
        (AMBER, "(and then I typed my"),
        (AMBER, " first prompt...)"),
    ])


def slide_documents_problem(prs):
    s = make_slide(prs)
    title_bar(s, "The 'buried in Documents' problem",
              "Where you start Claude Code matters more than you'd think.")
    # Left: narrative
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(6.6), Inches(5.0), [
        "I started Claude Code several folders deep inside ~/Documents/.",
        "Seemed natural — that's where I keep everything.",
        "But: macOS restricts writes to ~/Documents for unsigned tools.",
        "Claude hit 'Operation not permitted' errors on file edits.",
        "Even with permission, the folder was cluttered — Claude couldn't reason cleanly about scope.",
        "Unprompted, Claude suggested: 'Let's move to a dedicated project folder outside Documents.'",
    ], size=15)
    # Right: visual path tree showing the deep nesting
    add_code_block(s, Inches(7.6), Inches(1.8), Inches(5.2), Inches(4.2), [
        (RGBColor(0x8F, 0xD6, 0xFF), "~/"),
        (CODE_FG, "└── Documents/"),
        (CODE_FG, "    └── MBA/"),
        (CODE_FG, "        └── spring-2026/"),
        (CODE_FG, "            └── projects/"),
        (CODE_FG, "                └── experiments/"),
        (CODE_FG, "                    └── trading/"),
        (CODE_FG, "                        └── my-bot/"),
        (AMBER, "                            ← I started here"),
        "",
        (RED, "# Claude: 'This won't work.'"),
    ])
    add_callout(s, Inches(0.6), Inches(6.7), Inches(12.2), Inches(0.5),
                "A senior developer's first instinct would have been the same as Claude's. "
                "That's the bar.")


def slide_moved_to_clean_root(prs):
    s = make_slide(prs)
    title_bar(s, "The move that fixed everything",
              "One line. Massive difference.")
    # Simple before-after visual
    # Before
    add_plain_text(s, Inches(0.6), Inches(1.9), Inches(6.0), Inches(0.4),
                   "BEFORE", size=14, bold=True, color=RED)
    add_code_block(s, Inches(0.6), Inches(2.4), Inches(6.0), Inches(1.8), [
        (RGBColor(0x8F, 0xD6, 0xFF), "cwd:"),
        (CODE_FG, "~/Documents/MBA/spring-2026/"),
        (CODE_FG, "   projects/experiments/"),
        (CODE_FG, "   trading/my-bot/"),
        (RED, "status: blocked on permissions"),
    ])
    # After
    add_plain_text(s, Inches(7.2), Inches(1.9), Inches(6.0), Inches(0.4),
                   "AFTER", size=14, bold=True, color=GREEN)
    add_code_block(s, Inches(7.2), Inches(2.4), Inches(6.0), Inches(1.8), [
        (RGBColor(0x8F, 0xD6, 0xFF), "cwd:"),
        (CODE_FG, "~/trading-bot/"),
        "",
        "",
        (GREEN, "status: full write access, clean scope"),
    ])
    add_body_text(s, Inches(0.6), Inches(4.6), Inches(12.2), Inches(2.5), [
        "Took about 3 minutes: make a new folder in home, move files, restart Claude there.",
        "Immediate unlock: file edits, git init, dependencies install, everything 'just worked.'",
        "Lesson: when using an AI that edits files for you, give it a dedicated workspace.",
    ], size=15)


def slide_directory_before(prs):
    s = make_slide(prs)
    title_bar(s, "What my project root looked like at Day 3",
              "Flat, chaotic. I couldn't find anything.")
    add_plain_text(s, Inches(0.6), Inches(1.7), Inches(12), Inches(0.4),
                   "~/trading-bot/  (20 items in root)",
                   size=14, italic=True, color=MID_GRAY)
    add_code_block(s, Inches(0.6), Inches(2.15), Inches(12.2), Inches(4.8), [
        "trading-bot/",
        "├── README.md",
        "├── backtest.py",
        "├── backtest_crypto.py",
        "├── backtest_meanrev.py",
        "├── backtest_regime.py",
        "├── backtest_scanner.py",
        "├── backtest_volbreak.py",
        "├── config.crypto.coinbase.yaml",
        "├── config.crypto.live.yaml",
        "├── config.crypto.paper.yaml",
        "├── config.live.yaml",
        "├── config.paper.yaml",
        "├── config.scanner.live.yaml",
        "├── config.scanner.paper.yaml",
        "├── dry_run.py",
        "├── requirements.txt",
        "├── scan.py",
        "├── src/",
        "├── logs/         # 36 files, half legacy",
        "├── start_bots.sh",
        "└── stop_bots.sh",
    ])


def slide_directory_after(prs):
    s = make_slide(prs)
    title_bar(s, "One conversation later — clean hierarchy",
              "Claude reorganized it and updated every file path in the codebase.")
    add_plain_text(s, Inches(0.6), Inches(1.7), Inches(12), Inches(0.4),
                   "~/trading-bot/  (8 items in root)",
                   size=14, italic=True, color=MID_GRAY)
    add_code_block(s, Inches(0.6), Inches(2.15), Inches(12.2), Inches(4.6), [
        "trading-bot/",
        "├── README.md",
        "├── requirements.txt",
        "├── start_bots.sh",
        "├── stop_bots.sh",
        "├── src/                  # bot source code",
        "├── configs/              # all YAML configs",
        "├── backtests/            # backtest scripts",
        "├── tools/                # dashboards, helpers",
        "└── logs/",
        "    ├── state/            # *.state.json + *.pid",
        "    ├── daily/            # YYYY-MM-DD.{bot}.log",
        "    ├── manual/           # stdout/stderr captures",
        "    ├── archive/          # legacy logs",
        "    └── summary.log       # one-line-per-day summary",
    ])
    add_callout(s, Inches(0.6), Inches(6.85), Inches(12.2), Inches(0.5),
                "The real flex: every import, config reference, and shell script got "
                "updated to match — no broken paths.", color=GREEN)


def slide_alpaca(prs):
    s = make_slide(prs)
    title_bar(s, "Opening account #1 — Alpaca (stocks)",
              "Paper trading first, then real money.")
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(12.2), Inches(5.0), [
        "Alpaca gives you TWO accounts: paper (fake money) + live (real money).",
        "Each has its OWN API keys. Claude walked me through which goes where.",
        "Hit a wall on IP whitelisting — Claude explained the options in plain English.",
        "Funded live with $500 via ACH from Chase. Paper starts at $100,000.",
    ], size=16)
    # Two-account visual
    # Paper
    pbox = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                              Inches(0.8), Inches(4.8), Inches(5.6), Inches(1.8))
    pbox.fill.solid(); pbox.fill.fore_color.rgb = BADGE_BG
    pbox.line.color.rgb = NAVY
    add_plain_text(s, Inches(0.8), Inches(4.95), Inches(5.6), Inches(0.4),
                   "PAPER ACCOUNT", size=13, bold=True, color=NAVY, align=PP_ALIGN.CENTER)
    add_plain_text(s, Inches(0.8), Inches(5.4), Inches(5.6), Inches(0.5),
                   "$99,997.81 simulated", size=18, bold=True, color=GREEN, align=PP_ALIGN.CENTER)
    add_plain_text(s, Inches(0.8), Inches(6.0), Inches(5.6), Inches(0.5),
                   "for parallel testing at bigger size",
                   size=12, italic=True, color=MID_GRAY, align=PP_ALIGN.CENTER)
    # Live
    lbox = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                              Inches(6.9), Inches(4.8), Inches(5.6), Inches(1.8))
    lbox.fill.solid(); lbox.fill.fore_color.rgb = BADGE_BG
    lbox.line.color.rgb = RED
    add_plain_text(s, Inches(6.9), Inches(4.95), Inches(5.6), Inches(0.4),
                   "LIVE ACCOUNT", size=13, bold=True, color=RED, align=PP_ALIGN.CENTER)
    add_plain_text(s, Inches(6.9), Inches(5.4), Inches(5.6), Inches(0.5),
                   "$601.56 real", size=18, bold=True, color=GREEN, align=PP_ALIGN.CENTER)
    add_plain_text(s, Inches(6.9), Inches(6.0), Inches(5.6), Inches(0.5),
                   "($100 seed + $500 ACH deposit)",
                   size=12, italic=True, color=MID_GRAY, align=PP_ALIGN.CENTER)


def slide_coinbase(prs):
    s = make_slide(prs)
    title_bar(s, "Opening account #2 — Coinbase (crypto)",
              "The deposit saga that nearly cost me 8% in fees.")
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(7.2), Inches(5.0), [
        "Coinbase kept pushing me toward wire transfer. Wire fee: ~$25.",
        "On a $300 deposit, that's 8% lost before I've made a trade.",
        "Claude: 'Do NOT wire. Push ACH from Chase instead. Zero fee.'",
        "Plaid-linked Chase to Coinbase in 2 minutes.",
        "Deposit landed as USD, but my bot trades USDC pairs.",
        "Claude: 'Convert USD → USDC in the Coinbase UI. Free. Instant.'",
        "Final balance: $408.94 USDC, clean and ready to trade.",
    ])
    add_callout(s, Inches(8.2), Inches(2.0), Inches(4.6), Inches(3.8),
                "Without Claude, I would have wired it.\n\n"
                "The 8% fee would have erased months of expected returns "
                "before the bot took its first trade.", color=RED)


def slide_guardrails(prs):
    s = make_slide(prs)
    title_bar(s, "What we built together (the short version)",
              "I described what I wanted. Claude designed and implemented the guardrails.")
    # 6 guardrail badges in a 3x2 grid
    guardrails = [
        ("Kill switch",     "Flip a config flag to pause all trading."),
        ("Equity sizing",   "Position size auto-scales with account balance."),
        ("Training wheels", "Hard cap on trade size for the first 20 trades."),
        ("Growth cap",      "New size can never exceed 1.5x previous size."),
        ("DD circuit",      "7-day drawdown >10% freezes sizing for a week."),
        ("Silent alerts",   "Desktop notification only on BUY/SELL/FREEZE."),
    ]
    for i, (name, desc) in enumerate(guardrails):
        row, col = divmod(i, 3)
        x = Inches(0.6 + col * 4.1)
        y = Inches(1.9 + row * 2.0)
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, Inches(3.9), Inches(1.7))
        box.fill.solid(); box.fill.fore_color.rgb = BADGE_BG
        box.line.color.rgb = ACCENT; box.line.width = Pt(1.25)
        add_plain_text(s, x, y + Inches(0.15), Inches(3.9), Inches(0.5),
                       name, size=17, bold=True, color=NAVY, align=PP_ALIGN.CENTER)
        add_plain_text(s, x + Inches(0.25), y + Inches(0.75), Inches(3.4), Inches(0.9),
                       desc, size=12, color=DARK_GRAY, align=PP_ALIGN.CENTER)
    add_callout(s, Inches(0.6), Inches(6.2), Inches(12.2), Inches(0.7),
                "I knew these concepts from finance class. Claude turned them into "
                "working code I trust with real money.")


def slide_cloud(prs):
    s = make_slide(prs)
    title_bar(s, "Moving to the cloud",
              "Because my MacBook can't be on 24/7 — and neither can I.")
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(6.0), Inches(4.5), [
        "Problem: macOS sleep was skipping end-of-day market scans.",
        "Claude recommended: DigitalOcean, NYC3 region, $6/mo.",
        "I created the droplet; Claude SSH'd in and did the rest.",
        "Under 45 minutes from 'let's do it' to three bots running 24/7.",
        "Security: firewall, SSH-key-only, fail2ban, hardened out of the box.",
    ], size=15)
    # Architecture diagram
    lap = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                             Inches(7.2), Inches(2.0), Inches(1.8), Inches(1.0))
    lap.fill.solid(); lap.fill.fore_color.rgb = LIGHT_GRAY
    lap.line.color.rgb = NAVY
    add_plain_text(s, Inches(7.2), Inches(2.25), Inches(1.8), Inches(0.5),
                   "MacBook", size=14, bold=True, color=NAVY, align=PP_ALIGN.CENTER)
    arr1 = s.shapes.add_connector(1, Inches(8.1), Inches(3.1), Inches(8.1), Inches(3.6))
    arr1.line.color.rgb = ACCENT; arr1.line.width = Pt(2)
    add_plain_text(s, Inches(8.3), Inches(3.15), Inches(2.5), Inches(0.4),
                   "ssh", size=11, italic=True, color=MID_GRAY)
    drop = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                              Inches(6.8), Inches(3.7), Inches(2.6), Inches(1.3))
    drop.fill.solid(); drop.fill.fore_color.rgb = NAVY
    drop.line.fill.background()
    add_plain_text(s, Inches(6.8), Inches(3.85), Inches(2.6), Inches(0.4),
                   "DigitalOcean Droplet", size=13, bold=True,
                   color=RGBColor(0xFF, 0xFF, 0xFF), align=PP_ALIGN.CENTER)
    add_plain_text(s, Inches(6.8), Inches(4.3), Inches(2.6), Inches(0.5),
                   "Ubuntu + systemd\n(always on)",
                   size=11, color=RGBColor(0xCC, 0xDD, 0xFF), align=PP_ALIGN.CENTER)
    arr2 = s.shapes.add_connector(1, Inches(7.5), Inches(5.1), Inches(6.8), Inches(5.9))
    arr2.line.color.rgb = ACCENT; arr2.line.width = Pt(2)
    arr3 = s.shapes.add_connector(1, Inches(8.7), Inches(5.1), Inches(9.6), Inches(5.9))
    arr3.line.color.rgb = ACCENT; arr3.line.width = Pt(2)
    al = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                            Inches(6.0), Inches(5.9), Inches(1.8), Inches(0.8))
    al.fill.solid(); al.fill.fore_color.rgb = LIGHT_GRAY
    al.line.color.rgb = NAVY
    add_plain_text(s, Inches(6.0), Inches(6.05), Inches(1.8), Inches(0.5),
                   "Alpaca", size=13, bold=True, color=NAVY, align=PP_ALIGN.CENTER)
    cb = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                            Inches(8.9), Inches(5.9), Inches(1.8), Inches(0.8))
    cb.fill.solid(); cb.fill.fore_color.rgb = LIGHT_GRAY
    cb.line.color.rgb = NAVY
    add_plain_text(s, Inches(8.9), Inches(6.05), Inches(1.8), Inches(0.5),
                   "Coinbase", size=13, bold=True, color=NAVY, align=PP_ALIGN.CENTER)


def slide_sip_bug(prs):
    s = make_slide(prs)
    title_bar(s, "The moment that taught me humility",
              "Five days of silent failure. Found because I didn't trust Claude's answer.")
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(7.0), Inches(4.5), [
        "For 5 days, the scanner reported 'zero trading opportunities.'",
        "Claude's explanation: 'the market is just calm — normal variance.'",
        "My gut: something's off.",
        "I asked Claude to verify the full pipeline with live API calls.",
        "Result: the bot had been blind since Day 1 — silent 403 errors on every data fetch.",
        "Root cause: a subscription-tier gotcha we both missed at setup.",
        "Fixed in one line. Scanner started working the next trading day.",
    ], size=15)
    add_callout(s, Inches(8.0), Inches(1.8), Inches(4.8), Inches(4.5),
                "Lesson: AI is often confidently wrong.\n\n"
                "Your skepticism is the last guardrail.\n\n"
                "When something feels off, force a live verification. "
                "Don't trust explanations of your system — probe the system itself.",
                color=RED)


def slide_where_we_are(prs):
    s = make_slide(prs)
    title_bar(s, "Where I am today",
              "Three bots running 24/7 on infrastructure I don't operate myself.")
    rows = [
        ("Scanner Live (Alpaca)",    "$601.56",     "trading real money since Day 3"),
        ("Scanner Paper (Alpaca)",   "$99,997.81",  "parallel test at bigger size"),
        ("Crypto Trend (Coinbase)",  "$408.94",     "BTC + ETH, hourly polling"),
    ]
    y = Inches(1.9)
    add_plain_text(s, Inches(0.6), y, Inches(4.5), Inches(0.4),
                   "Bot", size=14, bold=True, color=NAVY)
    add_plain_text(s, Inches(5.5), y, Inches(2.5), Inches(0.4),
                   "Equity", size=14, bold=True, color=NAVY)
    add_plain_text(s, Inches(8.2), y, Inches(4.8), Inches(0.4),
                   "Notes", size=14, bold=True, color=NAVY)
    y += Inches(0.45)
    div = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.6), y, Inches(12.2), Emu(15000))
    div.fill.solid(); div.fill.fore_color.rgb = ACCENT
    div.line.fill.background()
    y += Inches(0.15)
    for name, equity, notes in rows:
        add_plain_text(s, Inches(0.6), y, Inches(4.5), Inches(0.4), name, size=14)
        add_plain_text(s, Inches(5.5), y, Inches(2.5), Inches(0.4), equity,
                       size=14, bold=True, color=GREEN)
        add_plain_text(s, Inches(8.2), y, Inches(4.8), Inches(0.4), notes,
                       size=13, color=MID_GRAY)
        y += Inches(0.5)
    add_body_text(s, Inches(0.6), Inches(4.8), Inches(12.2), Inches(2.2), [
        "~$1,000 total deployed. Automated sizing, DD circuit breaker, manual kill switch.",
        "Silent notifications on BUY/SELL, daily summary log, terminal dashboard.",
        "Lines of code I wrote by hand: close to zero.",
        "Hours spent: about 30 over 8 days.",
    ], size=15)


def slide_takeaways(prs):
    s = make_slide(prs)
    title_bar(s, "What I actually learned",
              "Not about trading. About working with an AI that writes production code.")
    add_body_text(s, Inches(0.6), Inches(1.8), Inches(12.0), Inches(5.5), [
        "Treat Claude Code like a senior dev, not a search engine. Push back. Negotiate. Disagree.",
        "The tool owns the typing. You own the judgment — what to build, when to ship, what risk to accept.",
        "Silent failures are the dangerous ones. When something feels off, verify end-to-end.",
        "Memory across sessions is a superpower. Teach it your constraints once — it remembers.",
        "The workspace matters. Set up a clean project folder before you start coding.",
        "Infrastructure is accessible now. Cloud deployment, hardening, monitoring — all achievable.",
        "The ROI isn't the artifact. It's the workflow I'll use for every future project.",
    ], size=15)


def slide_closing(prs):
    s = make_slide(prs)
    add_plain_text(s, Inches(0.6), Inches(2.4), Inches(12.0), Inches(1.6),
                   "\"AI didn't do the thinking.",
                   size=36, bold=True, color=NAVY, italic=True)
    add_plain_text(s, Inches(0.6), Inches(3.4), Inches(12.0), Inches(1.6),
                   "It let me do more of it, faster.\"",
                   size=36, bold=True, color=NAVY, italic=True)
    add_plain_text(s, Inches(0.6), Inches(5.4), Inches(12.0), Inches(0.6),
                   "— what I actually learned in 8 days",
                   size=16, color=MID_GRAY)
    add_plain_text(s, Inches(0.6), Inches(6.5), Inches(12.0), Inches(0.4),
                   "Questions?", size=18, italic=True,
                   color=ACCENT, align=PP_ALIGN.CENTER)


def main():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    slide_title(prs)
    slide_installation(prs)
    slide_documents_problem(prs)
    slide_moved_to_clean_root(prs)
    slide_directory_before(prs)
    slide_directory_after(prs)
    slide_alpaca(prs)
    slide_coinbase(prs)
    slide_guardrails(prs)
    slide_cloud(prs)
    slide_sip_bug(prs)
    slide_where_we_are(prs)
    slide_takeaways(prs)
    slide_closing(prs)

    out = Path(__file__).parent.parent / "claude-code-presentation.pptx"
    prs.save(str(out))
    print(f"Generated: {out}")
    print(f"Slide count: {len(prs.slides)}")


if __name__ == "__main__":
    main()
