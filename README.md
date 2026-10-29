# pr-explain-video

A Claude Code skill that explains a pull request as a narrated video: slides
rendered with headless Chrome, speech from a Kokoro TTS server, assembled with
ffmpeg. When a live environment is given, it records a tutorial-style demo with
real captured output.

The skill itself is [SKILL.md](SKILL.md). Install it by linking this directory
into `~/.claude/skills/pr-explain-video`.

Requirements: Python 3 (standard library only), Google Chrome or Chromium,
ffmpeg/ffprobe, `gh`, and a Kokoro-FastAPI server (`KOKORO_URL`, default
`http://localhost:8880`). See the header of `scripts/prvideo.py` for the
environment variables.

An unattended job that runs this skill on a cluster lives in the
`hypershell-gitops` repository (`20-pr-explain` under the cluster's bootstrap
apps); it bakes this directory into its sandbox image.
