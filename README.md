# video-pipeline

Pull down a short-form video and turn it into things you can actually read: a
transcript and a set of frames. Frames are sampled unevenly on purpose — dense
over the hook, sparse over the body — because the first few seconds are what
decide whether anyone watches the rest.

Works on anything `yt-dlp` handles: TikTok, Instagram Reels, YouTube Shorts.

## Install

```bash
brew install yt-dlp ffmpeg      # ffmpeg brings ffprobe with it
pip install openai-whisper
```

On Debian/Ubuntu: `sudo apt install ffmpeg && pip install yt-dlp openai-whisper`.

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
| `-m, --model NAME` | `small` | Whisper model: `tiny`…`large` |
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
├── video.txt           # transcript
└── frames/
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

## Notes

- Whisper's first run downloads the model (`small` is ~460MB), so that one is slow.
  Later runs reuse the cached weights.
- `small` is a good default for English. Drop to `base` if you're batching a lot,
  go to `medium` for accents or noisy audio.
- Videos shorter than the hook window skip body frames rather than failing.
- `out/` is gitignored — the downloaded media stays local.
