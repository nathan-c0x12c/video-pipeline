# analyses

Committed output of the two analysis prompts, one directory per video.

`out/` is gitignored — it holds the media, the frames and the raw pipeline
output, and stays local. What lands here is the readable part: the observation
and teardown, plus the transcript, frame index and metadata they cite, so a
finding can be checked without re-downloading the video.

```
analyses/<video-id>/
├── observation.md      # prompts/01-observe.md output
├── teardown.md         # prompts/02-teardown.md output
├── transcript.txt      # copy of out/<id>/video.txt
├── frames-index.tsv    # copy of out/<id>/frames/index.tsv
└── metadata.json       # copy of out/<id>/video.info.json
```

`metadata.json` is renamed on the way in — `*.info.json` is gitignored.

Frames are not committed. To reproduce them for a given analysis, re-run the
pipeline with the same sampling as the frame index:

```bash
./bin/vpipe "<url>"
```
