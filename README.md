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
| `-n, --dry-run` | | Print the commands instead of running them |

### Output

```
out/<video-id>/
├── video.mp4
├── video.info.json     # caption, author, view/like counts, duration
├── video.txt           # transcript, one timestamped line per speech segment
└── frames/
    ├── index.tsv       # frame → approximate timestamp, for aligning to the transcript
    ├── hook_001.jpg    # first 5s @ 2fps  → 10 frames
    ├── ...
    └── body_001.jpg    # the rest @ 0.5fps → 1 frame / 2s
```

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
