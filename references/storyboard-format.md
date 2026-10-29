# storyboard.json

One JSON file describes the whole video. Paths are relative to the storyboard's directory.

```json
{
  "title": "Gateway databases from one external PostgreSQL",
  "output": "hypershell-300-explainer",
  "pr": {"repo": "owner/repo", "number": 300, "url": "https://github.com/owner/repo/pull/300"},
  "voice": "am_michael",
  "speed": 1.0,
  "pronounce": {"CNPG": "C N P G", "kubectl": "cube control"},
  "slides": [ ... ]
}
```

- `output`: base name of the `.mp4` and `.srt`, written next to the storyboard.
- `voice`: any Kokoro voice. Prefix = language and gender: `af_`/`am_` US English, `bf_`/`bm_` British,
  `ef_`/`em_` Spanish, `ff_` French, `if_`/`im_` Italian, `pf_`/`pm_` Portuguese. Good defaults: `am_michael`,
  `af_heart`, `bm_george`. Blend with `voiceA+voiceB`. Write the narration in the language of the voice.
- `speed`: 1.0 is natural; 1.05-1.1 is fine for dense technical content.
- `pronounce`: whole-word replacements applied to narration before synthesis only (subtitles keep the original).

## Fields every slide accepts

| Field | Meaning |
|---|---|
| `type` | one of the types below (required) |
| `id` | short slug; names the frame and audio files (`03-components.png`) |
| `section` | small label above the title. A change of section starts a new video chapter |
| `title` | the slide's claim |
| `narration` | what the voice says. Without it the slide is silent for `duration` seconds (default 3) |
| `note` | one line pinned to the bottom, for the takeaway ("**Why it matters:** ...") |
| `step` | badge before the title, for demo steps ("Step 2") |
| `pause` | seconds of silence after the narration (default 0.8) |
| `voice` | per-slide voice override |

Inline formatting in on-screen text: `` `code` `` and `**bold**`. Nothing else is interpreted.

## Slide types

### `title`
```json
{"type": "title", "title": "...", "subtitle": "the PR in one sentence",
 "meta": ["owner/repo #300", "by author", "+3179", "−20550", "311 files"], "narration": "..."}
```
`meta` entries that look like `+123` / `−123` are coloured.

### `section`
A divider, used before the demo. `{"type": "section", "number": "Demo", "title": "...", "subtitle": "..."}`

### `bullets`
```json
{"type": "bullets", "title": "...", "bullets": ["plain", {"text": "with detail", "sub": ["smaller line"]}]}
```
Six bullets at most; three or four read better. Keep each to one line where you can.

### `components`
```json
{"type": "components", "title": "...", "columns": 3, "components": [
  {"name": "Control plane", "role": "Reconciles gateways into the cluster", "status": "changed",
   "change": "Provisions role + database per gateway on the external server"},
  {"name": "CLI", "role": "hypershell command", "status": "unchanged"}
]}
```
`status`: `new` | `changed` | `removed` | `unchanged` (dimmed). `columns` is optional. List every component
of the solution, not only the touched ones. Up to about nine cards; group small ones ("SDKs") if there are more.

### `diagram`
Before/after, laid out automatically: you give layers (rows, top to bottom), the script places the boxes and
draws the arrows.
```json
{"type": "diagram", "title": "...", "caption": "where this applies, in one line",
 "before_label": "Before", "after_label": "After this PR",
 "before": { ...graph... }, "after": { ...graph... }}
```
A graph:
```json
{"layers": [["cli", "console"], ["api"], ["cp"], ["pg"]],
 "nodes": {"cp": {"label": "Control plane", "sub": "reads admin Secret", "status": "changed"},
           "pg": {"label": "External PostgreSQL", "shape": "db", "status": "new"}},
 "edges": [{"from": "api", "to": "cp", "label": "gRPC"},
           {"from": "cp", "to": "pg", "label": "CREATE ROLE", "status": "new"}],
 "groups": [{"label": "cluster", "nodes": ["api", "cp"]}]}
```
- Every node id appears in exactly one layer. `nodes` is optional detail: `label` (default: the id), `sub`,
  `status` (`new` | `changed` | `removed`), `shape` (`box` | `db` | `actor`).
- Edge fields: `from`, `to`, optional `label` (keep under ~18 characters), `status`, `both` (arrow on both ends).
- To show that something disappeared, keep it in the **after** graph with `"status": "removed"`; it is drawn
  ghosted with a GONE tag, which reads much better than a box that silently vanishes.
- Keep the same layers and node order on both sides so the eye sees only the difference.
- A `db` cylinder has less room for text than a box: with three or more nodes in its layer, give it a short
  label and no `sub` (say the detail in the caption or the narration instead).
- Limits for legibility: four layers, four nodes per layer in a before/after pair. Prefer edges between
  adjacent layers; an edge that skips a layer is drawn straight and may cross a box. Groups should be contiguous.
- Use `"graph": {...}` instead of before/after for a single full-width diagram (up to six nodes per layer),
  or `"svg": "<svg ...>"` to supply your own drawing (1680x590).

### `code`
```json
{"type": "code", "title": "...", "file": "internal/gateway/database.go", "language": "go",
 "code": "func ...", "highlight": [3, 4, 5], "note": "**Why it matters:** ..."}
```
At most ~20 lines and ~100 columns; cut to the lines that carry the decision and mark elisions with a
`// ...` line. `highlight` takes line numbers or regexes. `"language": "diff"` colours `+`/`-`/`@@` lines.

### `terminal`
```json
{"type": "terminal", "step": "Step 2", "title": "Create a gateway",
 "capture": "demo/02-create-gateway.json", "lines": [1, 18], "highlight": ["sslmode"], "narration": "..."}
```
- `capture`: a file written by `prvideo.py capture`; supplies the command and the real output.
- `lines: [from, to]` or `max_lines` trims long output (a `...` marker is added). `highlight`: regexes or line numbers.
- Several commands on one slide: `"entries": [{"capture": ...}, {"capture": ..., "highlight": [...]}]`.
- A non-zero exit is drawn in red and `check` warns, unless the slide sets `"expect_failure": true`
  (for steps whose point is the error, such as a 404 on a removed endpoint).
- Inline `command` + `output` is accepted for illustrative, clearly non-live content only; `check` warns.

### `screenshot`
```json
{"type": "screenshot", "step": "Step 4", "title": "The database field is gone from the gateway page",
 "image": "shots/04-gateway-detail.png", "url": "https://console.example/gateways/demo", "note": "..."}
```

### `html`
Escape hatch: `{"type": "html", "title": "...", "html": "<div>...</div>"}` renders your markup inside the
themed frame. Sizes inside are relative to a 40px base font; see `assets/theme.css`.

## Output layout

```
WD/
  storyboard.json   analysis.md   demo.md
  pr/               pr.json files.json pr.diff        (from pr_overview.py)
  demo/             *.json captures                    (inputs - keep)
  shots/            *.png browser screenshots          (inputs - keep)
  build/            slides/ frames/ audio/ ...         (disposable)
  <output>.mp4      <output>.srt
```
