---
name: pr-explain-video
description: Explain a pull request as a narrated video - slides plus text-to-speech, assembled with ffmpeg. Covers what the PR does functionally, which components it touches, architecture changes with before/after diagrams, the key changes in detail, and (when a live environment is given) a tutorial-style demo of the new behaviour with real captured output. Use when asked to "explain this PR in a video", "make a video walkthrough of PR N", "narrated explainer for this pull request", or to re-render one after the PR changed.
argument-hint: "<PR url> [live environment URLs / how to reach it]"
---

# PR explainer video

Turn a pull request into a short narrated video that a teammate can watch instead of reading the diff. The
viewer should finish it knowing **what changed for users, where in the system it changed, why it is built
that way, and how to see it working** - and nothing else. The video is the meat of the PR, not a tour of it.

You produce one file, `storyboard.json` (slides + narration), and `scripts/prvideo.py` turns it into frames
(headless Chrome), speech (Kokoro TTS) and an MP4 with subtitles and chapters (ffmpeg). All the judgement is
in the analysis and the storyboard; the build is mechanical and takes about a minute, so iterate freely.

`SK` below is this skill's directory. Work in `./videos/<repo>-<number>/` under the current directory
(call it `WD`). If the current directory is the repository under review, put `WD` outside the tree or in an
ignored path so the video never ends up in a commit.

## 1. Gather

```bash
python3 SK/scripts/pr_overview.py <PR url> --save WD/pr
```

This prints the description, commits, churn per area, and the files that carry the idea with generated
code, lock files, tests and docs separated out (on big PRs those are most of the lines and none of the story).
It saves `pr.json`, `files.json` and `pr.diff` (generated files omitted) for reading.

Then get the system context, because a diff does not tell you what the components *are*:

- Prefer a local checkout (look for one before cloning; `gh repo clone` into the scratchpad otherwise, and
  `gh pr checkout` there, never in the user's working copy). Read the top-level README, `CLAUDE.md`/`AGENTS.md`,
  architecture docs or specs, and the deploy manifests at the base ref.
- Read the PR's linked issues and any PR it says it supersedes; the *why* is often there.
- Read the signal files properly. For a file that is mostly deleted, read what it used to do; for a new
  file, read all of it.

## 2. Analyse - write `WD/analysis.md`

Write the analysis down before the storyboard; it is what you narrate from, and it is what the user will
correct. Work through these in order:

1. **Functional summary.** What is different for a user, operator or API client after this merges? Lead with
   behaviour and capability, not code: "every gateway database now lives on one external PostgreSQL server",
   not "removes cnpg_db.go". Include anything that breaks or needs action on upgrade. Two to five points.
2. **Component map.** First list the components that make up the *solution* (services, CLIs, UIs, operators,
   data stores, external systems) with a one-line role each - from the docs and manifests, not from the diff.
   Then mark each as new / changed / removed / unchanged, with one line on what changed. Unchanged components
   stay on the map: they are how the viewer orients.
3. **Architecture-relevant changes.** A change is architectural if it alters the boxes or the arrows: a
   component added or removed, a new or removed dependency between components, data or responsibility
   moving from one owner to another, an API contract or resource type appearing or disappearing, a change in
   deployment topology, trust boundary, or credential flow. If there are none, say so and skip the section
   in the video - do not inflate a refactor into architecture.
4. **Before / after.** If a component or a relation is new or gone, sketch the before and after graphs
   (nodes, edges, which are new/changed/removed) and note *where it applies*: which flow, which environments,
   which deployments are affected and which are not.
5. **Key changes, tied to the goal.** Three to six changes that implement the summary. For each: what it
   does, why it is done this way (the design decision, the trade-off, the failure mode it handles), and which
   goal from point 1 it serves. If you cannot tie a change to a goal, it is either noise or a second goal you
   missed.
6. **Left out.** One list of what you are deliberately not covering. The test: *would a teammate who skipped
   this PR be surprised by it later, or need it to understand the system?* If not, it is out: generated code,
   renames, formatting, dependency bumps, test plumbing, doc rewording, and the mechanical fan-out of one
   decision (a field removed from the API, both SDKs, the CLI and the console is **one** point, said once).
   At most one sentence of the video goes to "also in this PR".

## 3. Live demo (only if the user gave an environment)

The demo answers "how do I see the new behaviour myself?" as a short tutorial: a handful of steps, each with
one action and one observation. Design it from the functional summary - each step should make one of those
points visible. Showing that something is *gone* (a 404 on a removed endpoint, a missing menu entry, a CRD
that no longer exists) is as valid as showing something new.

1. **Probe first, read-only.** Confirm the environment is reachable and actually runs this PR's code (version
   endpoint, image tag, a behaviour only the PR has). If it does not, stop and tell the user; a demo of the
   wrong build is worse than none.
2. **Draft the script** in `WD/demo.md`: numbered steps, the command or click, what the viewer should notice,
   and which point it proves. Three to seven steps.
3. **Run every step for real and capture it.** Output on a slide must come from a capture file, never from
   memory or expectation:
   ```bash
   python3 SK/scripts/prvideo.py capture WD --name 02-create-gateway \
       --display 'hypershell create gateway demo-pr300' -- '<the real command line>'
   ```
   `--display` is the clean command shown on the slide when the real one carries tokens or plumbing.
   Tokens, JWTs and password-like values are redacted automatically; add `--redact REGEX` for anything else.
   Look at what was captured before using it.
4. **Web UI steps** use a dedicated demo browser. `shot` lays the page out in a 1280x720 viewport and
   captures it at 1.5x, so the PNG is 1920x1080 but the page text stays readable inside a slide. Keep that
   default; for a page that only works wide, pass `--width`/`--height` and check the frame is still legible:
   ```bash
   python3 SK/scripts/prvideo.py browser-start WD --url https://console.example [--insecure]
   python3 SK/scripts/prvideo.py shot WD --out shots/03-gateway-detail.png --url https://console.example/gateways/x
   python3 SK/scripts/prvideo.py shot WD --out shots/04-after-click.png --js "document.querySelector('...').click()"
   ```
   `shot` prints the page URL, title and visible text - read them to confirm you captured the page you meant
   (a login redirect is the usual surprise). If the console needs a login, ask the user to sign in once in
   the demo browser window; never type credentials yourself. `--insecure` is for dev environments with a
   private CA and affects only the demo profile.
   **Unattended jobs** (a runner script started you and says so): the runner has already signed in for you.
   It ran `prvideo.py signin` on the demo browser and gave the CLI a session, from credentials you never
   see. Do not start another browser profile, do not log out, and do not look for the credentials. If a page
   or the CLI reports that the session is gone, that step failed: report it, do not try to log in.
5. **Be a good guest.** Create resources with a recognisable name (`demo-pr<number>-...`), clean up what you
   created, and ask before anything destructive or anything that touches resources you did not create.
6. **A step that fails is a finding.** Report it to the user with the captured output. Do not route around
   it, fake it, or quietly drop the step - the PR may be broken, or the environment may be stale.

## 4. Storyboard - write `WD/storyboard.json`

Format and every slide type: [references/storyboard-format.md](references/storyboard-format.md). A complete
small example: [assets/example-storyboard.json](assets/example-storyboard.json).

The order is fixed; the slide counts are a budget, not a quota:

| # | Section | Slides | Slide types |
|---|---------|--------|-------------|
| 1 | Title - the PR in one sentence | 1 | `title` |
| 2 | What changes (functional) | 1-3 | `bullets` |
| 3 | Components - the map, then what is touched | 1 | `components` |
| 4 | Architecture - only if section 2.3 found any | 0-2 | `diagram` (before/after), `bullets` for "where it applies" |
| 5 | Key changes, each tied to the goal | 2-6 | `bullets`, `code`, `diagram` |
| 6 | Live demo - only with an environment | 1 + steps | `section`, then `terminal` / `screenshot` with `step` |
| 7 | Recap - what to remember, what needs care on upgrade | 1 | `bullets` |

Aim for 3-6 minutes; a very large PR may take 8. If `check` estimates more, cut key changes before anything else.

**Slides** carry the exact words: identifiers, numbers, file paths, the one code fragment that matters. A
slide title is a claim ("The controller re-reads credentials on every operation"), not a topic
("Credentials"). Use a `code` slide only when the code is the clearest way to show the decision, and show
only those lines.

**Narration** carries the reasoning, and it is written for the ear - this matters more than anything else
for how the video feels:

- Do not read the slide. The slide says *what*; the voice says *why* and *so what*.
- Short sentences, one idea each, in a conversational first person plural. 40-80 words per slide; over
  ~95 words on one still frame drags, so split the slide.
- Say names the way a person would: "the gateway database admin secret", not the hyphenated resource name
  that is on screen. No symbols, paths, URLs, underscores or backticks - `check` warns about these.
- Put acronyms and product names the voice might mangle in the storyboard's `pronounce` map
  (`"CNPG": "C N P G"`, `"kubectl": "cube control"`, `"PostgreSQL": "postgres"`). Subtitles keep the
  original spelling.
- Link each key change back to the goal out loud ("this is what makes password rotation safe").
- Demo narration is a live session in the first person: what I am doing, what to look at, what it proves.
  "First, I create a gateway. Watch the last line: the secret now carries the certificate authority."

## 5. Build

```bash
python3 SK/scripts/prvideo.py all WD/storyboard.json
```

`all` runs `check` (structure, narration length, unspeakable text, estimated duration), `slides`, `tts` and
`video`; each is also a subcommand. Kokoro is started from the TTS experiment (`PRVIDEO_TTS_DIR`, default
`/Users/amarin/work/experiments/tts`, `make up`) if it is not answering. Speech is cached per slide, so after
editing one narration only that clip is re-synthesised. `--engine say` is a macOS fallback if Kokoro cannot run.

Then verify before delivering:

- Fix every `WARN` from `slides`: content that was shrunk or overflows is unreadable in a video. Trim, do not squeeze.
- Look at the frames that can go wrong - `Read` the PNGs in `WD/build/frames/` for each diagram, the densest
  code or terminal slide, and each screenshot. Check arrows land where you meant and nothing important is cut.
- Confirm the duration is in budget.

## 6. Deliver

Send the MP4 (`SendUserFile` if available) and tell the user, briefly: the duration, the outline (section by
section, one line each), what you deliberately left out, any demo step that failed or surprised you, and
which words you put in `pronounce` - you cannot hear the result, so the user is the one who will catch a
mispronunciation. Do not stop for approval of the outline before rendering; a render is cheaper than a
round trip, and feedback on a video is better than feedback on a plan.

To revise: edit `storyboard.json` (or re-capture a demo step) and run `all` again. If the PR itself changed,
re-run `pr_overview.py`, update `analysis.md` first, then the storyboard.
