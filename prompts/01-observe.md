# Stage 1 — Observe

Read `<OUT_DIR>/` and write `<OUT_DIR>/observation.md`.

Inputs:

- `frames/index.tsv` — every frame with its approximate timestamp and segment
- `frames/*.jpg` — read them **in timestamp order**, not filename order
- `video.txt` — the transcript
- `video.info.json` — caption, author, duration, view/like counts

## The one rule

**Record what is visible and audible. Nothing else.**

No "conveys authority." No "creates intrigue." No guessing at intent, mood, or
strategy. If you cannot point at the pixel or quote the line, it does not go in
this file. Stage 2 is where interpretation happens, and it is only as good as
this file is clean.

Two tests before any sentence stays:

- Would a second person looking at the same frame write something contradicting it?
  If yes, it is interpretation. Cut it.
- Does it use a word about effect rather than fact — *compelling, striking,
  engaging, powerful, effective*? Cut it.

Write "a hand enters frame holding a blue package," not "the product reveal
lands." Write "cut to a new angle at 2.5s," not "a snappy edit keeps energy up."

## What to record

**Per frame** — but only what *changed* from the previous frame. Do not
re-describe a static background ten times.

- Subject and position — who or what, where in frame, facing where
- Motion — what moved, which direction
- On-screen text — quote it **exactly**, note position and whether it is new
- Framing — shot size, angle, and any change from the previous frame
- Cuts — mark the timestamp whenever the frame is discontinuous from the one before

**Across the video**

- A cut list: every timestamp where the shot changes
- On-screen text timeline: each text element, when it appears, when it goes
- Transcript alignment: which spoken line is running at each frame's timestamp
- Anything the transcript says that no frame shows, and vice versa

## Gaps are findings

Frames are samples, not the video. Between two body frames 2 seconds apart,
anything could have happened. Where a change is too large to be explained by
the sampling gap, say so explicitly rather than inventing the bridge.
"body_003 and body_004 show different locations; the transition between them
was not sampled" is a good line. Making up a transition is not.

Mark anything you genuinely cannot resolve — blurred text, an obscured face,
an ambiguous object — as `[unclear: ...]`. Do not smooth over it.

## Output

```markdown
# Observation — <video id>

## Source
Duration, caption, author, counts. Straight from video.info.json.

## Timeline
| t (s) | frame | what changed | spoken line at this moment |

## Cut list
Timestamps of every shot change.

## On-screen text
| t in | t out | exact text | position |

## Coverage gaps
Where sampling leaves genuine uncertainty. What is [unclear].
```
