#!/usr/bin/env python3
"""prvideo - build a narrated PR explainer video from a storyboard.json.

Standard library only. External tools: Google Chrome/Chromium (slides and
browser screenshots), ffmpeg/ffprobe (audio + video), a Kokoro-FastAPI server
(text to speech, OpenAI-compatible /v1/audio/speech).

  prvideo.py check   STORYBOARD            validate + estimate duration
  prvideo.py slides  STORYBOARD            storyboard -> build/frames/*.png
  prvideo.py tts     STORYBOARD            narration  -> build/audio/*.wav (cached)
  prvideo.py video   STORYBOARD            frames + audio -> <out>.mp4 + .srt
  prvideo.py all     STORYBOARD            check, slides, tts, video

  prvideo.py capture WORKDIR --name N [--display CMD] [--redact RE] -- CMD...
                                           run a demo step, keep its real output
  prvideo.py browser-start WORKDIR [--url URL]
                                           open the demo browser (user logs in here)
  prvideo.py signin WORKDIR --url URL --credentials-file FILE
                                           unattended jobs: the runner signs the demo browser in
  prvideo.py shot WORKDIR --out shots/x.png [--url URL] [--js EXPR] [--wait S]
                                           screenshot the demo browser: 1280x720 viewport,
                                           captured at 1.5x (a 1920x1080 PNG)

Everything is written next to the storyboard: build/ is disposable, demo/ and
shots/ hold captured evidence and are inputs.

Env: KOKORO_URL (http://localhost:8880), PRVIDEO_TTS_DIR (compose project that
starts Kokoro with `make up`), CHROME (browser binary), PRVIDEO_CDP_PORT (9333).
"""
import argparse
import base64
import datetime
import glob
import hashlib
import html
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KOKORO_URL = os.environ.get("KOKORO_URL", "http://localhost:8880").rstrip("/")
TTS_DIR = os.environ.get("PRVIDEO_TTS_DIR", "/Users/amarin/work/experiments/tts")
CDP_PORT = int(os.environ.get("PRVIDEO_CDP_PORT", "9333"))

W, H = 1920, 1080
# Demo browser viewport. A page laid out for 1920 px has text too small to read once the
# screenshot sits inside a slide, so lay it out at 1280x720 and capture it at 1.5x: the PNG
# still has 1920x1080 pixels, but everything on the page is 1.5 times larger.
SHOT_W, SHOT_H, SHOT_SCALE = 1280, 720, 1.5
FPS = 30
RATE = 48000          # all audio is normalised to 48 kHz mono s16
LEAD, TAIL = 0.5, 0.8  # silence before/after the narration of each slide
WORDS_PER_SEC = 2.5

SLIDE_TYPES = {
    "title": ["title"],
    "section": ["title"],
    "bullets": ["title", "bullets"],
    "components": ["title", "components"],
    "diagram": ["title"],
    "code": ["title", "code"],
    "terminal": ["title"],
    "screenshot": ["title", "image"],
    "html": ["html"],
}
STATUSES = ("new", "changed", "removed", "unchanged")


def die(msg):
    sys.exit(f"ERROR: {msg}")


def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


# --------------------------------------------------------------------------- storyboard

class Board:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError) as e:
            die(f"cannot read storyboard {path}: {e}")
        self.slides = self.data.get("slides") or []
        self.build = os.path.join(self.dir, "build")
        seen = set()
        for i, s in enumerate(self.slides):
            sid = re.sub(r"[^a-z0-9]+", "-", str(s.get("id") or s.get("type", "slide")).lower()).strip("-")
            while sid in seen:
                sid += "x"
            seen.add(sid)
            s["_key"] = f"{i + 1:02d}-{sid}"

    def sub(self, name):
        d = os.path.join(self.build, name)
        os.makedirs(d, exist_ok=True)
        return d

    def resolve(self, rel):
        return rel if os.path.isabs(rel) else os.path.join(self.dir, rel)

    def out_base(self):
        name = self.data.get("output") or "pr-explainer"
        return self.resolve(re.sub(r"\.mp4$", "", name))


def load_capture(board, ref):
    p = board.resolve(ref)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        die(f"cannot read capture {ref}: {e}")


def terminal_entries(board, s):
    """Normalise a terminal slide to [{command, output, exit_code, ...}]."""
    entries = s.get("entries") or [s]
    out = []
    for e in entries:
        e = dict(e)
        if e.get("capture"):
            cap = load_capture(board, e["capture"])
            e.setdefault("command", cap.get("command", ""))
            e["output"] = cap.get("output", "")
            e["exit_code"] = cap.get("exit_code", 0)
        lines = (e.get("output") or "").rstrip("\n").split("\n") if e.get("output") else []
        if e.get("lines"):
            a, b = e["lines"]
            head = ["..."] if a > 1 else []
            tail = ["..."] if b < len(lines) else []
            lines = head + lines[a - 1:b] + tail
        mx = e.get("max_lines")
        if mx and len(lines) > mx:
            lines = lines[:mx] + [f"... ({len(lines) - mx} more lines)"]
        e["_lines"] = lines
        out.append(e)
    return out


def check(board):
    errors, warns = [], []
    if not board.slides:
        errors.append("storyboard has no slides")
    total_words = 0
    for s in board.slides:
        k, t = s["_key"], s.get("type")
        if t not in SLIDE_TYPES:
            errors.append(f"{k}: unknown type {t!r} (one of {', '.join(SLIDE_TYPES)})")
            continue
        for field in SLIDE_TYPES[t]:
            if not s.get(field):
                errors.append(f"{k}: type {t} needs '{field}'")
        nar = (s.get("narration") or "").strip()
        words = len(nar.split())
        total_words += words
        if not nar and t != "section":
            warns.append(f"{k}: no narration (slide will be silent for {s.get('duration', 3)}s)")
        if words > 95:
            warns.append(f"{k}: narration is {words} words (~{words / WORDS_PER_SEC:.0f}s on one still frame); split the slide")
        bad = sorted(set(re.findall(r"`|_|->|=>|https?://|/[a-z]+/|[{}<>|]|\*\*", nar)))
        if bad:
            warns.append(f"{k}: narration has text a voice reads badly {bad}; write it the way it is spoken")
        if t == "bullets" and len(s.get("bullets", [])) > 6:
            warns.append(f"{k}: {len(s['bullets'])} bullets; keep it to 6 or fewer")
        if t == "components":
            for c in s.get("components", []):
                if c.get("status", "unchanged") not in STATUSES:
                    errors.append(f"{k}: component {c.get('name')!r} has status {c.get('status')!r}; use {STATUSES}")
        if t == "code" and len(s.get("code", "").split("\n")) > 26:
            warns.append(f"{k}: code is over 26 lines; show only the lines that carry the idea")
        if t == "diagram":
            graphs = [s[g] for g in ("before", "after", "graph") if s.get(g)]
            if not graphs and not s.get("svg"):
                errors.append(f"{k}: diagram needs before+after, graph, or svg")
            for g in graphs:
                ids = {n for layer in g.get("layers", []) for n in layer}
                for e in g.get("edges", []):
                    for end in ("from", "to"):
                        if e.get(end) not in ids:
                            errors.append(f"{k}: edge endpoint {e.get(end)!r} is not in any layer")
                for grp in g.get("groups", []):
                    for n in grp.get("nodes", []):
                        if n not in ids:
                            errors.append(f"{k}: group {grp.get('label')!r} names unknown node {n!r}")
        if t == "terminal":
            for e in terminal_entries(board, s):
                if not e.get("capture") and e.get("output"):
                    warns.append(f"{k}: inline terminal output; prefer a capture file so the output is provably real")
                if e.get("exit_code") not in (0, None) and not s.get("expect_failure"):
                    warns.append(f"{k}: captured command exited {e['exit_code']}; set expect_failure if that is the point")
            n = sum(len(e["_lines"]) + 1 for e in terminal_entries(board, s))
            if n > 30:
                warns.append(f"{k}: {n} terminal lines; use 'lines' or 'max_lines' to show the part that matters")
        if t == "screenshot" and s.get("image") and not os.path.exists(board.resolve(s["image"])):
            errors.append(f"{k}: image not found: {s['image']}")
    est = total_words / WORDS_PER_SEC + len(board.slides) * (LEAD + TAIL)
    print(f"{len(board.slides)} slides, {total_words} narration words, estimated {est / 60:.1f} min")
    for w in warns:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    if errors:
        die(f"{len(errors)} storyboard error(s)")
    return warns


# --------------------------------------------------------------------------- slide rendering

def inline(text):
    t = html.escape(str(text), quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)


def graph_has_sub(g):
    return any(m.get("sub") for m in g.get("nodes", {}).values())


def graph_svg(g, width, height, tall=None):
    """Layered top-to-bottom layout: one row per layer, arrows between boxes."""
    layers = g.get("layers", [])
    meta = g.get("nodes", {})
    top, bottom = 34, 34
    wide = width > 1000
    gap, max_nw = (70, 440) if wide else (44, 330)
    # Boxes take at most ~58% of a row's pitch so arrows and their labels keep room.
    pitch = (height - top - bottom) / max(len(layers), 1)
    nh = min(104 if (graph_has_sub(g) if tall is None else tall) else 78, int(pitch * 0.58))
    pos = {}
    for li, layer in enumerate(layers):
        n = len(layer)
        nw = min(max_nw, (width - 60 - (n - 1) * gap) / n)
        total = n * nw + (n - 1) * gap
        y = top + li * (height - top - bottom - nh) / (len(layers) - 1) if len(layers) > 1 else (height - nh) / 2
        for ni, nid in enumerate(layer):
            pos[nid] = ((width - total) / 2 + ni * (nw + gap), y, nw, li)

    color = {"new": "var(--new)", "changed": "var(--changed)", "removed": "var(--removed)"}
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
         "<defs>"]
    for name, c in [("d", "#8fa0d8")] + list(color.items()):
        o.append(f'<marker id="a-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" '
                 f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>')
    o.append("</defs>")

    for grp in g.get("groups", []):
        boxes = [pos[n] for n in grp.get("nodes", []) if n in pos]
        if not boxes:
            continue
        x0 = min(b[0] for b in boxes) - 24
        y0 = min(b[1] for b in boxes) - 36
        x1 = max(b[0] + b[2] for b in boxes) + 24
        y1 = max(b[1] for b in boxes) + nh + 20
        o.append(f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{x1 - x0:.0f}" height="{y1 - y0:.0f}" rx="18" '
                 f'fill="#ffffff08" stroke="#5f6b91" stroke-width="2" stroke-dasharray="8 8"/>')
        o.append(f'<text x="{x0 + 16:.0f}" y="{y0 + 24:.0f}" fill="#94a0c4" font-size="18" '
                 f'font-family="-apple-system,Helvetica,Arial" letter-spacing="1.5">{html.escape(grp.get("label", "").upper())}</text>')

    clamp = lambda v, lo, hi: max(lo, min(hi, v))
    labels = []
    for e in g.get("edges", []):
        a, b = pos.get(e.get("from")), pos.get(e.get("to"))
        if not a or not b:
            continue
        acx, bcx = a[0] + a[2] / 2, b[0] + b[2] / 2
        if a[3] == b[3]:
            left_to_right = a[0] < b[0]
            x1, x2 = (a[0] + a[2], b[0]) if left_to_right else (a[0], b[0] + b[2])
            y1 = y2 = a[1] + nh / 2
        else:
            down = a[3] < b[3]
            x1 = clamp(bcx, a[0] + 30, a[0] + a[2] - 30)
            x2 = clamp(acx, b[0] + 30, b[0] + b[2] - 30)
            y1, y2 = (a[1] + nh, b[1]) if down else (a[1], b[1] + nh)
        st = e.get("status")
        stroke = color.get(st, "#8fa0d8")
        dash = ' stroke-dasharray="10 8" opacity=".6"' if st == "removed" else ""
        both = f' marker-start="url(#a-{st if st in color else "d"})"' if e.get("both") else ""
        o.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{stroke}" stroke-width="3.5"'
                 f'{dash} marker-end="url(#a-{st if st in color else "d"})"{both}/>')
        if e.get("label"):
            labels.append((x1 + (x2 - x1) * 0.42, y1 + (y2 - y1) * 0.42, e["label"], stroke))

    for nid, (x, y, nw, _) in pos.items():
        m = meta.get(nid, {})
        st = m.get("status")
        stroke = color.get(st, "#3d4c85")
        fill = {"new": "#123a2f", "changed": "#3a2e14", "removed": "#3a1720"}.get(st, "#182247")
        extra = ' stroke-dasharray="10 8" opacity=".62"' if st == "removed" else ""
        shape = m.get("shape", "box")
        if shape == "db":
            ry = 10
            o.append(f'<path d="M{x:.0f},{y + ry:.0f} a{nw / 2:.0f},{ry} 0 0 1 {nw:.0f},0 v{nh - 2 * ry} '
                     f'a{nw / 2:.0f},{ry} 0 0 1 -{nw:.0f},0 z" fill="{fill}" stroke="{stroke}" stroke-width="3.5"{extra}/>')
            o.append(f'<path d="M{x:.0f},{y + ry:.0f} a{nw / 2:.0f},{ry} 0 0 0 {nw:.0f},0" fill="none" '
                     f'stroke="{stroke}" stroke-width="3.5"{extra}/>')
            fy, fh = y + ry + 2, nh - ry - 4
        else:
            rx = nh / 2 if shape == "actor" else 16
            o.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{nw:.0f}" height="{nh}" rx="{rx:.0f}" fill="{fill}" '
                     f'stroke="{stroke}" stroke-width="3.5"{extra}/>')
            fy, fh = y, nh
        sub = f'<span class="s">{inline(m["sub"])}</span>' if m.get("sub") else ""
        o.append(f'<foreignObject class="g-node {shape} {st or ""}" x="{x:.0f}" y="{fy:.0f}" width="{nw:.0f}" height="{fh:.0f}">'
                 f'<div xmlns="http://www.w3.org/1999/xhtml"><span class="l">{inline(m.get("label", nid))}</span>{sub}</div>'
                 f'</foreignObject>')
        if st in ("new", "removed"):
            o.append(f'<rect x="{x + 10:.0f}" y="{y - 15:.0f}" width="84" height="28" rx="14" fill="{stroke}"/>'
                     f'<text x="{x + 52:.0f}" y="{y + 5:.0f}" text-anchor="middle" font-size="16" font-weight="700" '
                     f'fill="#08111f" font-family="-apple-system,Helvetica,Arial">{"NEW" if st == "new" else "GONE"}</text>')

    for cx, cy, text, stroke in labels:
        tw = len(text) * 10.2 + 22
        o.append(f'<rect x="{cx - tw / 2:.0f}" y="{cy - 16:.0f}" width="{tw:.0f}" height="32" rx="8" fill="#0b1020" '
                 f'stroke="{stroke}" stroke-width="1.5"/>'
                 f'<text x="{cx:.0f}" y="{cy + 6:.0f}" text-anchor="middle" font-size="18" fill="#dfe6ff" '
                 f'font-family="-apple-system,Helvetica,Arial">{html.escape(text)}</text>')
    o.append("</svg>")
    return "".join(o)


def pre_lines(lines, highlight=(), diff=False, cls="out"):
    rx = [re.compile(h) for h in highlight if isinstance(h, str)]
    nums = {h for h in highlight if isinstance(h, int)}
    o = []
    for i, line in enumerate(lines, 1):
        c = [cls]
        if diff:
            if line.startswith("@@"):
                c.append("hunk")
            elif line.startswith("+"):
                c.append("plus")
            elif line.startswith("-"):
                c.append("minus")
        if line.startswith("...") and cls == "out":
            c.append("gap")
        if i in nums or any(r.search(line) for r in rx):
            c.append("hl")
        o.append(f'<span class="ln {" ".join(c)}">{html.escape(line, quote=False) or " "}</span>')
    return "".join(o)


def slide_body(board, s):
    t = s["type"]
    note = f'<div class="note">{inline(s["note"])}</div>' if s.get("note") else ""
    if t == "title":
        meta = "".join(f"<span>{m}</span>" for m in (inline(x) for x in s.get("meta", [])))
        meta = re.sub(r"<span>(\+[\d,]+)</span>", r'<span class="add">\1</span>', meta)
        meta = re.sub(r"<span>([-−][\d,]+)</span>", r'<span class="del">\1</span>', meta)
        return (f'<div class="title-main">{inline(s["title"])}</div>'
                f'<div class="title-sub">{inline(s.get("subtitle", ""))}</div><div class="title-meta">{meta}</div>')
    if t == "section":
        num = f'<div class="section-num">{inline(s["number"])}</div>' if s.get("number") else ""
        return (f'{num}<div class="title-main">{inline(s["title"])}</div>'
                f'<div class="title-sub">{inline(s.get("subtitle", ""))}</div>')
    if t == "bullets":
        items = []
        for b in s["bullets"]:
            if isinstance(b, dict):
                subs = "".join(f"<li>{inline(x)}</li>" for x in b.get("sub", []))
                items.append(f'<li>{inline(b.get("text", ""))}<ul>{subs}</ul></li>')
            else:
                items.append(f"<li>{inline(b)}</li>")
        return f'<ul class="bullets">{"".join(items)}</ul>{note}'
    if t == "components":
        comps = s["components"]
        cols = s.get("columns") or (2 if len(comps) <= 4 else 3 if len(comps) <= 9 else 4)
        cards = []
        for c in comps:
            st = c.get("status", "unchanged")
            what = f'<div class="what">{inline(c["change"])}</div>' if c.get("change") else ""
            cards.append(f'<div class="card {st}"><div class="name"><span>{inline(c["name"])}</span>'
                         f'<span class="badge">{st}</span></div>'
                         f'<div class="role">{inline(c.get("role", ""))}</div>{what}</div>')
        return f'<div class="cards" style="--cols:{cols}">{"".join(cards)}</div>{note}'
    if t == "diagram":
        if s.get("svg"):
            inner = f'<div class="panels"><div class="panel">{s["svg"]}</div></div>'
        elif s.get("graph"):
            inner = f'<div class="panels"><div class="panel">{graph_svg(s["graph"], 1680, 600)}</div></div>'
        else:
            tall = graph_has_sub(s["before"]) or graph_has_sub(s["after"])   # same box size on both sides
            inner = (f'<div class="panels"><div class="panel before"><h2>{inline(s.get("before_label", "Before"))}</h2>'
                     f'{graph_svg(s["before"], 800, 560, tall)}</div>'
                     f'<div class="panel after"><h2>{inline(s.get("after_label", "After this PR"))}</h2>'
                     f'{graph_svg(s["after"], 800, 560, tall)}</div></div>')
        legend = ('<div class="legend"><span><i style="border-color:var(--new)"></i>new</span>'
                  '<span><i style="border-color:var(--changed)"></i>changed</span>'
                  '<span><i style="border-color:var(--removed);border-style:dashed"></i>removed</span></div>')
        cap = f'<div class="caption">{inline(s["caption"])}</div>' if s.get("caption") else ""
        return inner + legend + cap
    if t == "code":
        diff = s.get("language") == "diff"
        body = pre_lines(s["code"].rstrip("\n").split("\n"), s.get("highlight", []), diff=diff)
        bar = f'<div class="bar"><i></i><i></i><i></i>{html.escape(s.get("file", ""))}</div>'
        return f'<div class="window">{bar}<pre>{body}</pre></div>{note}'
    if t == "terminal":
        parts = []
        for e in terminal_entries(board, s):
            if e.get("command"):
                parts.append(pre_lines(e["command"].split("\n"), cls="cmd"))
            failed = e.get("exit_code") not in (0, None) and not s.get("expect_failure")
            parts.append(pre_lines(e["_lines"], e.get("highlight", s.get("highlight", [])), cls="out fail" if failed else "out"))
        bar = f'<div class="bar"><i></i><i></i><i></i>{html.escape(s.get("shell", "terminal"))}</div>'
        return f'<div class="window">{bar}<pre>{"".join(parts)}</pre></div>{note}'
    if t == "screenshot":
        src = "file://" + urllib.request.pathname2url(board.resolve(s["image"]))
        bar = f'<div class="bar"><i></i><i></i><i></i><span class="url">{html.escape(s.get("url", ""))}</span></div>'
        return f'<div class="window browser">{bar}<div class="shot"><img src="{src}"></div></div>{note}'
    return s.get("html", "")


FIT_JS = """<script>
(function () {
  var fit = document.querySelector('.fit'); if (!fit) return;
  function over() {
    var els = [fit].concat([].slice.call(fit.querySelectorAll('pre')));
    return els.some(function (e) { return e.scrollHeight > e.clientHeight + 2 || e.scrollWidth > e.clientWidth + 2; });
  }
  var s = 1;
  while (over() && s > 0.56) { s -= 0.04; fit.style.setProperty('--fit', s.toFixed(2)); }
  var bad = over();
  if (bad) document.body.setAttribute('data-overflow', '1');
  document.title = (bad ? 'OVERFLOW' : 'OK') + ' fit=' + s.toFixed(2);
})();
</script>"""


def slide_html(board, s, index, css):
    t = s["type"]
    n = len(board.slides)
    header = ""
    if t not in ("title", "section", "html") or (t == "html" and s.get("title")):
        step = f'<span class="step">{inline(s["step"])}</span>' if s.get("step") else ""
        kicker = f'<div class="kicker">{inline(s["section"])}</div>' if s.get("section") else ""
        header = f'<header>{kicker}<h1>{step}{inline(s.get("title", ""))}</h1></header>'
    pr = board.data.get("pr", {})
    left = " · ".join(x for x in (pr.get("repo"), f"#{pr['number']}" if pr.get("number") else None) if x)
    footer = (f'<footer><span>{html.escape(left)}</span><span>{index + 1} / {n}</span>'
              f'<div class="bar"><i style="width:{(index + 1) / n * 100:.1f}%"></i></div></footer>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>slide</title><style>{css}</style></head>'
            f'<body class="{t}">{header}<div class="fit">{slide_body(board, s)}</div>{footer}{FIT_JS}</body></html>')


def find_chrome():
    cands = [os.environ.get("CHROME"),
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    cands += [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
    cands += sorted(glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")))
    cands += sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")))
    for c in cands:
        if c and os.path.exists(c):
            return c
    die("no Chrome/Chromium found; set CHROME=/path/to/browser")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HeadlessChrome:
    """One throwaway headless browser driven over DevTools. (`chrome --screenshot`
    is not used: branded Chrome takes the picture and then never exits.)"""

    def __enter__(self):
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="prvideo-chrome-")
        self.proc = subprocess.Popen(
            [find_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             "--no-default-browser-check", "--allow-file-access-from-files", f"--user-data-dir={self.profile}",
             f"--remote-debugging-port={self.port}", f"--window-size={W},{H}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            targets = cdp_targets(self.port)
            if targets:
                break
            time.sleep(0.25)
        else:
            self.__exit__()
            die("headless Chrome did not start")
        self.cdp = CDP(targets[0]["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        return self.cdp

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def render_one(cdp, html_path, png_path):
    cdp.goto("file://" + urllib.request.pathname2url(html_path))
    cdp.evaluate("Promise.all([document.fonts.ready].concat([].slice.call(document.images).map(function (i) {"
                 " return i.complete ? 0 : new Promise(function (r) { i.onload = i.onerror = r; }); }))).then(function () { return 1; })")
    status = cdp.evaluate("document.title") or ""
    cdp.screenshot(png_path)
    m = re.match(r"(OVERFLOW|OK) fit=([\d.]+)", status)
    if not m:
        return "could not read fit status"
    if m.group(1) == "OVERFLOW":
        return "OVERFLOW: content does not fit even at the smallest scale; trim it"
    if float(m.group(2)) < 0.75:
        return f"content was shrunk to {float(m.group(2)):.0%}; it will be hard to read, trim it"
    return None


def slides(board):
    with open(os.path.join(SKILL_DIR, "assets", "theme.css"), encoding="utf-8") as f:
        css = f.read()
    hdir, fdir = board.sub("slides"), board.sub("frames")
    for stale in glob.glob(os.path.join(fdir, "*.png")):
        os.remove(stale)
    problems = 0
    with HeadlessChrome() as cdp:
        for i, s in enumerate(board.slides):
            hp = os.path.join(hdir, s["_key"] + ".html")
            with open(hp, "w", encoding="utf-8") as f:
                f.write(slide_html(board, s, i, css))
            issue = render_one(cdp, hp, os.path.join(fdir, s["_key"] + ".png"))
            print(f"  {'WARN' if issue else 'OK  '} {s['_key']}{'  ' + issue if issue else ''}")
            problems += bool(issue)
    print(f"frames: {fdir}" + (f"  ({problems} slide(s) need attention)" if problems else ""))


# --------------------------------------------------------------------------- text to speech

def kokoro_healthy():
    try:
        with urllib.request.urlopen(f"{KOKORO_URL}/health", timeout=5) as r:
            return json.load(r).get("status") == "healthy"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_kokoro():
    if kokoro_healthy():
        return
    if os.path.exists(os.path.join(TTS_DIR, "Makefile")):
        print(f"Kokoro is not answering at {KOKORO_URL}; starting it with `make -C {TTS_DIR} up` ...", flush=True)
        r = run(["make", "-C", TTS_DIR, "up"])
        if r.returncode:
            die(f"could not start Kokoro: {r.stderr[-400:]}")
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if kokoro_healthy():
                return
            time.sleep(3)
    die(f"Kokoro TTS is not healthy at {KOKORO_URL}. Start it (make -C {TTS_DIR} up) or pass --engine say")


def spoken(board, text):
    for word, say in (board.data.get("pronounce") or {}).items():
        text = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", say, text)
    return re.sub(r"\s+", " ", text).strip()


def synth(engine, text, voice, speed, raw_path):
    if engine == "say":
        aiff = raw_path + ".aiff"
        r = run(["say", "-r", str(int(175 * speed)), "-o", aiff, text] + (["-v", voice] if voice else []))
        if r.returncode:
            die(f"say failed: {r.stderr}")
        return aiff
    body = json.dumps({"model": "kokoro", "input": text, "voice": voice, "speed": speed,
                       "response_format": "wav", "stream": False}).encode()
    req = urllib.request.Request(f"{KOKORO_URL}/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        die(f"Kokoro HTTP {e.code}: {e.read()[:300]!r}")
    with open(raw_path, "wb") as f:
        f.write(data)
    return raw_path


def tts(board, engine="kokoro"):
    adir = board.sub("audio")
    voice = board.data.get("voice", "am_michael" if engine == "kokoro" else "")
    speed = float(board.data.get("speed", 1.0))
    if engine == "kokoro":
        ensure_kokoro()
    keep = set()
    for s in board.slides:
        text = spoken(board, s.get("narration") or "")
        if not text:
            continue
        v = s.get("voice", voice)
        wav = os.path.join(adir, s["_key"] + ".wav")
        stamp = wav + ".sha"
        digest = hashlib.sha256(json.dumps([engine, v, speed, text]).encode()).hexdigest()
        keep.update((wav, stamp))
        if os.path.exists(wav) and os.path.exists(stamp) and open(stamp).read() == digest:
            print(f"  --   {s['_key']}  cached  {wav_seconds(wav):5.1f}s")
            continue
        t0 = time.monotonic()
        raw = synth(engine, text, v, speed, os.path.join(adir, s["_key"] + ".raw.wav"))
        # Normalise: fixes streaming WAV headers and gives every clip one format.
        r = run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", wav])
        os.remove(raw)
        if r.returncode:
            die(f"ffmpeg could not normalise {raw}: {r.stderr}")
        with open(stamp, "w") as f:
            f.write(digest)
        print(f"  OK   {s['_key']}  voice={v}  {wav_seconds(wav):5.1f}s audio in {time.monotonic() - t0:4.1f}s", flush=True)
    for f in glob.glob(os.path.join(adir, "*")):
        if f not in keep:
            os.remove(f)


def wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


# --------------------------------------------------------------------------- video

def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def cues(text):
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > 95:
            cut = max(sentence.rfind(", ", 0, 95), sentence.rfind(" ", 0, 95))
            if cut <= 0:
                break
            out.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if sentence:
            out.append(sentence)
    return out


def video(board):
    fdir, adir = os.path.join(board.build, "frames"), os.path.join(board.build, "audio")
    silence = lambda sec: b"\x00\x00" * int(RATE * sec)
    pcm, concat, srt, chapters = [], [], [], []
    t, n_cue, last_section = 0.0, 0, None
    for s in board.slides:
        png = os.path.join(fdir, s["_key"] + ".png")
        if not os.path.exists(png):
            die(f"missing frame {png}; run `slides` first")
        wav = os.path.join(adir, s["_key"] + ".wav")
        narr = (s.get("narration") or "").strip()
        if narr:
            if not os.path.exists(wav):
                die(f"missing audio {wav}; run `tts` first")
            with wave.open(wav, "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
                    die(f"{wav} is not {RATE} Hz mono s16")
                speech = w.readframes(w.getnframes())
            clip = silence(LEAD) + speech + silence(float(s.get("pause", TAIL)))
            # subtitles: spread the sentences over the speech, weighted by length
            parts = cues(narr)
            span, total = len(speech) / 2 / RATE, sum(len(p) for p in parts) or 1
            at = t + LEAD
            for p in parts:
                d = span * len(p) / total
                n_cue += 1
                srt.append(f"{n_cue}\n{srt_time(at)} --> {srt_time(at + d - 0.05)}\n{p}\n")
                at += d
        else:
            clip = silence(float(s.get("duration", 3)))
        dur = len(clip) / 2 / RATE
        section = s.get("section") or (s.get("title") if s["type"] in ("title", "section") else None)
        if section and section != last_section:
            chapters.append((t, section))
            last_section = section
        pcm.append(clip)
        concat.append((png, dur))
        t += dur

    base = board.out_base()
    full_wav = os.path.join(board.build, "narration.wav")
    with wave.open(full_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(pcm))
    list_path = os.path.join(board.build, "frames.txt")
    with open(list_path, "w") as f:
        for png, dur in concat:
            f.write(f"file '{png}'\nduration {dur:.4f}\n")
        f.write(f"file '{concat[-1][0]}'\n")
    with open(base + ".srt", "w", encoding="utf-8") as f:
        f.write("\n".join(srt))
    meta_path = os.path.join(board.build, "chapters.txt")
    esc = lambda x: re.sub(r"([=;#\\\n])", r"\\\1", x)
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(f";FFMETADATA1\ntitle={esc(board.data.get('title', 'PR explainer'))}\n")
        for i, (start, name) in enumerate(chapters):
            end = chapters[i + 1][0] if i + 1 < len(chapters) else t
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(start * 1000)}\nEND={int(end * 1000)}\ntitle={esc(name)}\n")

    cmd = ["ffmpeg", "-y", "-v", "error", "-stats",
           "-f", "concat", "-safe", "0", "-i", list_path, "-i", full_wav]
    maps = ["-map", "0:v", "-map", "1:a"]
    if srt:
        cmd += ["-i", base + ".srt"]
        maps += ["-map", "2:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    cmd += ["-i", meta_path, "-map_metadata", "3" if srt else "2"] + maps
    cmd += ["-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage", "-crf", "20",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", str(RATE), "-c:a", "aac", "-b:a", "160k",
            "-t", f"{t:.3f}", "-movflags", "+faststart", base + ".mp4"]
    r = subprocess.run(cmd)
    if r.returncode:
        die("ffmpeg failed")
    probe = run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-of", "json", base + ".mp4"])
    info = json.loads(probe.stdout or "{}").get("format", {})
    print(f"video: {base}.mp4  {float(info.get('duration', 0)) / 60:.1f} min, "
          f"{int(info.get('size', 0)) / 1e6:.1f} MB, {len(chapters)} chapters")
    print(f"subtitles: {base}.srt")


# --------------------------------------------------------------------------- demo capture

DEFAULT_REDACT = [r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",   # JWT
                  r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}",
                  r"(?i)((?:password|passwd|secret|token|api[_-]?key)\"?\s*[:=]\s*\"?)[^\s\"',}]{6,}"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")


def redact(text, patterns):
    for p in patterns:
        rx = re.compile(p)
        text = rx.sub(lambda m: (m.group(1) if rx.groups else "") + "[redacted]", text)
    return text


def capture(args):
    if not args.demo_cmd:
        die("give the command after --")
    ddir = os.path.join(os.path.abspath(args.workdir), "demo")
    os.makedirs(ddir, exist_ok=True)
    cmd = args.demo_cmd
    shell_line = cmd[0] if len(cmd) == 1 else None
    t0 = time.monotonic()
    try:
        p = subprocess.run(["bash", "-c", shell_line] if shell_line else cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace", timeout=args.timeout, cwd=args.cwd)
        output, code = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        output, code = (e.stdout or "") + f"\n[timed out after {args.timeout}s]", 124
    patterns = DEFAULT_REDACT + (args.redact or [])
    output = redact(ANSI.sub("", output), patterns)
    real = shell_line or " ".join(cmd)
    rec = {"command": redact(args.display or real, patterns), "real_command": redact(real, patterns),
           "output": output, "exit_code": code, "seconds": round(time.monotonic() - t0, 2),
           "captured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    path = os.path.join(ddir, args.name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    print(output, end="" if output.endswith("\n") else "\n")
    print(f"[exit {code}, {len(output.splitlines())} lines, {rec['seconds']}s] -> demo/{args.name}.json")


# --------------------------------------------------------------------------- demo browser (CDP)

class CDP:
    """Just enough WebSocket + DevTools protocol to drive one tab."""

    def __init__(self, ws_url):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", ws_url)
        self.sock = socket.create_connection((m.group(1), int(m.group(2))), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {m.group(3)} HTTP/1.1\r\nHost: {m.group(1)}:{m.group(2)}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                die("demo browser closed the DevTools connection")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            die(f"DevTools websocket handshake failed: {buf[:200]!r}")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0
        self.events = []

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                die("DevTools connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, payload, opcode=1):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv(self):
        message = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            op = b0 & 0x0F
            if op == 9:
                self._send(data, opcode=10)
                continue
            if op == 8:
                die("DevTools closed the websocket")
            message += data
            if b0 & 0x80:
                return json.loads(message)

    def call(self, method, **params):
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": method, "params": params}).encode())
        while True:
            msg = self._recv()
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    die(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self.events.append(msg)

    def wait_event(self, name, timeout):
        self.sock.settimeout(timeout)
        try:
            while not any(e.get("method") == name for e in self.events):
                self.events.append(self._recv())
        except (socket.timeout, TimeoutError):
            print(f"  (no {name} within {timeout}s, continuing)")
        finally:
            self.sock.settimeout(60)
            self.events = []

    def goto(self, url, timeout=30):
        self.events = []
        self.call("Page.navigate", url=url)
        self.wait_event("Page.loadEventFired", timeout)

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))

    def evaluate(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            die(f"--js threw: {r['exceptionDetails'].get('exception', {}).get('description') or r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")


def cdp_targets(port=None):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port or CDP_PORT}/json/list", timeout=3) as r:
            return [t for t in json.load(r) if t.get("type") == "page"]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def browser_start(args):
    if cdp_targets() is not None:
        print(f"demo browser already running on DevTools port {CDP_PORT}")
        return
    profile = os.path.join(os.path.abspath(args.workdir), ".demo-browser")
    os.makedirs(profile, exist_ok=True)
    cmd = [find_chrome(), f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile}", "--no-first-run",
           "--no-default-browser-check", f"--window-size={SHOT_W},{SHOT_H + 90}"]
    if args.headless:
        cmd.append("--headless=new")
    if args.insecure:
        cmd.append("--ignore-certificate-errors")   # dev environments with a private CA, this profile only
    cmd.append(args.url or "about:blank")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        if cdp_targets():
            print(f"demo browser started (DevTools port {CDP_PORT}, profile {profile})")
            return
        time.sleep(0.5)
    die("demo browser did not come up")


def shot(args):
    targets = cdp_targets()
    if not targets:
        die("demo browser is not running; start it with `prvideo.py browser-start WORKDIR`")
    c = CDP(targets[0]["webSocketDebuggerUrl"])
    c.call("Page.enable")
    c.call("Emulation.setDeviceMetricsOverride", width=args.width, height=args.height,
           deviceScaleFactor=args.scale, mobile=False)
    if args.url:
        c.goto(args.url)
    time.sleep(args.wait)
    if args.js:
        print(f"js -> {json.dumps(c.evaluate(args.js))}")
        time.sleep(args.wait)
    out = os.path.join(os.path.abspath(args.workdir), args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    c.screenshot(out)
    print(f"url:   {c.evaluate('location.href')}")
    print(f"title: {c.evaluate('document.title')}")
    text = c.evaluate("document.body ? document.body.innerText : ''") or ""
    print("text:  " + re.sub(r"\s*\n\s*", " | ", text.strip())[:args.text_chars])
    print(f"saved: {out}")


def signin(args):
    """Fill and submit a username/password login form in the demo browser.

    For unattended runs only: the job's runner calls this before the agent starts, with a
    credentials file the agent did not write and does not read. Nothing from the file is printed,
    no screenshot is taken, and the file is never part of a command line."""
    with open(args.credentials_file, encoding="utf-8") as f:
        creds = json.load(f)
    username, password = creds.get("username"), creds.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        die("credentials file needs non-empty string fields: username, password")
    targets = cdp_targets()
    if not targets:
        die("demo browser is not running; start it with `prvideo.py browser-start WORKDIR`")
    c = CDP(targets[0]["webSocketDebuggerUrl"])
    c.call("Page.enable")
    c.goto(args.url)
    fill = """(() => {
      const u = document.querySelector(%s), p = document.querySelector(%s);
      if (!u || !p || !p.form) return false;
      const set = (el, v) => { el.focus(); el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); };
      set(u, %s); set(p, %s);
      p.form.requestSubmit ? p.form.requestSubmit() : p.form.submit();
      return true;
    })()""" % (json.dumps(args.username_selector), json.dumps(args.password_selector),
               json.dumps(username), json.dumps(password))
    deadline = time.time() + args.timeout
    submitted = False
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            if not submitted:
                submitted = bool(c.evaluate(fill))   # the form may appear only after a redirect or two
                continue
            href = c.evaluate("location.href") or ""
            still_form = c.evaluate("!!document.querySelector(%s)" % json.dumps(args.password_selector))
        except SystemExit:
            raise
        except Exception:
            continue                                 # the page is navigating
        if href.startswith(args.expect_url_prefix or args.url) and not still_form:
            print(f"signed in: {href.split('?')[0]}")
            print(f"title: {c.evaluate('document.title')}")
            return
    die("sign-in did not complete: " + ("the login form rejected the credentials or did not redirect back"
                                        if submitted else "no login form appeared"))


# --------------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "slides", "tts", "video", "all"):
        p = sub.add_parser(name)
        p.add_argument("storyboard")
        if name in ("tts", "all"):
            p.add_argument("--engine", choices=("kokoro", "say"), default="kokoro")
    p = sub.add_parser("capture")
    p.add_argument("workdir")
    p.add_argument("--name", required=True, help="file name under demo/, e.g. 02-create-gateway")
    p.add_argument("--display", help="command text to show on the slide instead of the real one")
    p.add_argument("--redact", action="append", help="extra regex to replace with [redacted]")
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--cwd")
    p = sub.add_parser("browser-start")
    p.add_argument("workdir")
    p.add_argument("--url")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--insecure", action="store_true", help="ignore TLS errors (private dev CA)")
    p = sub.add_parser("signin")
    p.add_argument("workdir")
    p.add_argument("--url", required=True, help="page that leads to the login form, e.g. the console home")
    p.add_argument("--credentials-file", required=True, help='JSON {"username": ..., "password": ...}')
    p.add_argument("--expect-url-prefix", help="signed in once the browser is back under this prefix (default: --url)")
    p.add_argument("--username-selector", default="#username")
    p.add_argument("--password-selector", default="#password")
    p.add_argument("--timeout", type=float, default=90)
    p = sub.add_parser("shot")
    p.add_argument("workdir")
    p.add_argument("--out", required=True, help="path relative to WORKDIR, e.g. shots/03-gateway-detail.png")
    p.add_argument("--url")
    p.add_argument("--js", help="expression evaluated after load, e.g. to click: document.querySelector('...').click()")
    p.add_argument("--wait", type=float, default=1.5)
    p.add_argument("--width", type=int, default=SHOT_W, help="viewport width in CSS px (default 1280)")
    p.add_argument("--height", type=int, default=SHOT_H, help="viewport height in CSS px (default 720)")
    p.add_argument("--scale", type=float, default=SHOT_SCALE, help="device scale factor of the PNG (default 1.5)")
    p.add_argument("--text-chars", type=int, default=600)
    argv, demo_cmd = sys.argv[1:], []
    if "--" in argv:                      # everything after -- is the demo command, untouched
        cut = argv.index("--")
        argv, demo_cmd = argv[:cut], argv[cut + 1:]
    a = ap.parse_args(argv)

    if a.cmd == "capture":
        a.demo_cmd = demo_cmd
        return capture(a)
    if a.cmd == "browser-start":
        return browser_start(a)
    if a.cmd == "shot":
        return shot(a)
    if a.cmd == "signin":
        return signin(a)
    board = Board(a.storyboard)
    if a.cmd in ("check", "all"):
        check(board)
    if a.cmd in ("slides", "all"):
        slides(board)
    if a.cmd in ("tts", "all"):
        tts(board, a.engine)
    if a.cmd in ("video", "all"):
        video(board)


if __name__ == "__main__":
    main()
