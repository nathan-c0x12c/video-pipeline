# video-pipeline

Pull down a short-form video and turn it into things you can actually read: a
transcript and a set of frames. Frames are sampled unevenly on purpose — dense
over the hook, sparse over the body — because the first few seconds are what
decide whether anyone watches the rest.

Then Claude reads the result and tells you why the video works.

Works on anything `yt-dlp` handles: TikTok, Instagram Reels, YouTube Shorts.
Everything runs locally — no VM, no API key, no GPU.

## Install

```bash
brew install yt-dlp ffmpeg      # ffmpeg brings ffprobe with it
pip install sherpa-onnx
```

On Debian/Ubuntu: `sudo apt install ffmpeg && pip install yt-dlp sherpa-onnx`.
On Windows: `winget install yt-dlp.yt-dlp Gyan.FFmpeg && pip install sherpa-onnx`.

`sherpa-onnx` runs Whisper through onnxruntime instead of torch — about 30MB
installed rather than 2.5GB, and the weights come from GitHub releases. Models
download on first use into `~/.cache/vpipe-models`.

The original `openai-whisper` CLI still works if you prefer it: install it and
pass `--asr whisper`. With `--asr auto` (the default) vpipe uses sherpa-onnx when
it's importable and falls back to the whisper CLI when it isn't.

Then put `bin/` on your PATH, or call the script directly:

```bash
./bin/vpipe "https://www.tiktok.com/@user/video/123"
```

## Usage

```bash
vpipe <url> [options]
```

| Option | Default | What it does |
| --- | --- | --- |
| `-o, --out DIR` | `out/<video-id>` | Where everything lands |
| `-m, --model NAME` | `small` | Whisper model: `tiny`…`medium`, or `.en` variants like `small.en` |
| `--asr BACKEND` | `auto` | `onnx`, `whisper`, or `auto` |
| `--hook SECONDS` | `5` | Length of the hook window |
| `--hook-fps N` | `2` | Frames per second over the hook |
| `--body-fps N` | `0.5` | Frames per second after it |
| `--skip-download` | | Reuse a `video.mp4` that's already there |
| `--skip-transcript` | | Don't run whisper |
| `--skip-frames` | | Don't extract frames |
| `--sheet` | | Also compose `frames/` into one labelled `sheet.jpg` |
| `-n, --dry-run` | | Print the commands instead of running them |

### Output

```
out/<video-id>/
├── video.mp4
├── video.info.json     # caption, author, view/like counts, duration
├── video.txt           # transcript, one timestamped line per speech segment
├── sheet.jpg           # only with --sheet: every frame tiled into one image
└── frames/
    ├── index.tsv       # frame → approximate timestamp, for aligning to the transcript
    ├── hook_001.jpg    # first 5s @ 2fps  → 10 frames
    ├── ...
    └── body_001.jpg    # the rest @ 0.5fps → 1 frame / 2s
```

### Contact sheets

`bin/vsheet OUT_DIR` tiles a video's frames into one labelled image — each
tile gets its timestamp burned into the corner (`H 0:02`, `B 0:15`). It's what
`vpipe --sheet` calls internally, but it also runs standalone against any
output directory that already has `frames/index.tsv`:

```bash
vsheet out/123                       # → out/123/sheet.jpg, 3 columns
vsheet out/123 --cols 4 --no-labels  # wider grid, no timestamps
```

One sheet is far cheaper to hand a model than a dozen separate frames, and
still legible enough for hook text and on-screen UI. Pull an individual
full-resolution frame from `frames/` instead when a detail needs a closer
look than the sheet gives it.

### Examples

```bash
# Defaults: 5s hook at 2fps, body at 1 frame every 2s
vpipe "https://www.tiktok.com/@user/video/123"

# Tighter hook, sampled harder — for a video that opens fast
vpipe "https://youtube.com/shorts/abc" --hook 3 --hook-fps 4

# Better transcript, no frames
vpipe "https://instagram.com/reel/xyz" --model medium --skip-frames

# Re-cut frames from a video you already downloaded
vpipe --skip-download --skip-transcript -o out/123 --hook-fps 6

# See what it would run
vpipe "https://www.tiktok.com/@user/video/123" --dry-run
```

## Analysis

The pipeline produces the inputs. Claude is the brain — no vision API to
configure, nothing to self-host.

```bash
./bin/vpipe "https://www.tiktok.com/@user/video/123"
```

Then, in Claude Code from the repo root:

```
Follow prompts/01-observe.md for out/123
Follow prompts/02-teardown.md for out/123
```

Two stages, deliberately:

| Stage | Produces | Job |
| --- | --- | --- |
| `01-observe.md` | `observation.md` | What is on screen, frame by frame. **No interpretation.** |
| `02-teardown.md` | `teardown.md` | Why it works — every claim citing a timestamp. |

Splitting them is the whole point. Ask for a verdict directly and you get
confident narration of things that were never on screen. Stage 1 pins down what
is actually there, and stage 2 has to cite it. Run stage 1 alone when you only
want a record of what happens.

`frames/index.tsv` maps each frame to its timestamp, which is what lets Claude
line frames up against the transcript. Without it, frame filenames carry no
timing.

### Local app

If you'd rather work from a Claude / ChatGPT / Gemini subscription than
Claude Code, `bin/vpipe-app` runs the same pipeline behind a local page:

```bash
./bin/vpipe-app
```

Opens `http://127.0.0.1:8765`. Drop a video (or paste a URL — TikTok isn't
blocked on a normal home network the way it can be in a sandboxed session),
and it downloads, transcribes, extracts frames, and builds a `sheet.jpg`
automatically. The page shows the transcript and frames, lets you drop in
comment screenshots and fill in a link/stats, then assembles the filled-in
`prompts/03-ad-teardown.md` prompt and copies it to your clipboard — paste it
into whichever chat you're logged into. Paste the model's report back into
the page and it saves as `teardown.md` next to the video.

No API keys, nothing billed per video — it stops at "prompt ready to paste"
and leans on the subscription you're already paying for. Stdlib Python only
(`app/server.py`), so nothing new to install beyond what `vpipe` already
needs. Binds to `127.0.0.1` only.

### Where the prompts came from

The observation/synthesis split, the continuity discipline, and "avoid
interpretation — stick to what you can see" are adapted from
[byjlw/video-analyzer](https://github.com/byjlw/video-analyzer).

Its *architecture* is not reused, on purpose. That tool describes one frame at a
time and then synthesises a summary from **frame 1 plus text notes** of the
rest — a necessary workaround for local vision models that take a single image
with tight context. Claude reads every frame together, so passing frames
directly is strictly more information than relaying them through text.

## Notes

- The first run downloads the model (`small` is ~640MB), so that one is slow.
  Later runs reuse the cached weights in `~/.cache/vpipe-models`.
- `small` is a good default. Drop to `base` if you're batching a lot, go to
  `medium` for accents or noisy audio. The `.en` variants are a little sharper on
  English and a little smaller.
- Transcript lines carry `[start -> end]` timestamps, which is what lets the
  analysis line spoken words up against frames. `bin/vtranscribe --plain` drops
  them if you just want the text.
- Speech is cut on silence before transcription, because Whisper's encoder only
  takes 30 seconds at a time. On a video with no detectable speech — music only,
  say — it falls back to fixed 25s windows rather than returning nothing.
- Videos shorter than the hook window skip body frames rather than failing.
- `out/` is gitignored — the downloaded media stays local.
- Frame timestamps in `index.tsv` are computed from the sample rate, not read
  back from the file. They're accurate to within a frame interval.
- Frames are samples. A 2-second gap between body frames can hide an entire
  shot — the prompts treat that as something to report, not paper over.
