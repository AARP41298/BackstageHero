# Auto-edit

Official music videos often aren't the album cut. A single offset
(`video_start_time`) fixes a long intro, but not:

- a **different tempo** (old songs especially)
- an **interlude** or skit in the middle
- **one chorus or verse fewer** (or more) than the album
- an **extended intro/outro** on the album that the video doesn't have

`autoedit.py` maps every moment of the album (or chart) audio to a moment of
the video and rebuilds the video from those pieces, so it plays in step with
the audio from start to end. `aspectcrop.py` crops letterbox bars to a
standard aspect ratio.

Both need Python 3.11+, numpy and ffmpeg/ffprobe on the PATH.

## How it works

1. Both tracks are turned into chroma + band-energy features, ~10 per second.
2. A Viterbi path through the cost matrix lets each album frame advance the
   video by 0, 1 or 2 frames (tempo) or jump anywhere for a fixed penalty
   (edits). Intros and outros are free.
3. The path is split at jumps and fitted with straight lines: a short list of
   segments `album [ref_start, ref_end] <- video [vid_start, vid_end] x speed`.
4. ffmpeg trims, re-times and joins the pieces.

What each difference turns into:

| Video vs album                 | Edit                                                       |
|--------------------------------|------------------------------------------------------------|
| long intro / interlude / skit  | **cut**: that part of the video is skipped                 |
| different tempo                | **speed**: the piece plays a little faster/slower          |
| missing chorus / longer album  | **insert**: an earlier part of the video plays again       |

The match quality is checked against a random pairing of the two tracks;
a wrong video sits near 1.0 and is refused (`match too weak`).

## Commands

```
python autoedit.py ALBUM VIDEO [OUTPUT] [options]
```

| Option              | What it does                                                         |
|---------------------|----------------------------------------------------------------------|
| `--plan`            | only print the segments and the edit log, don't render              |
| `--audio`           | put the album audio in the output (to check the sync by ear)        |
| `--keep-audio`      | keep the video's own audio, cut and re-timed the same way           |
| `--crop`            | crop black bars to a standard aspect ratio (see below)              |
| `--coverage 0.9`    | with `--crop`: share of frames that must stay whole (default 1.0)   |
| `--json FILE`       | save the plan + edit log as JSON (what the database stores)         |
| `--xml FILE`        | Final Cut Pro 7 XML timeline (Premiere, Resolve, Kdenlive)          |
| `--v3 FILE`         | auto-editor v3 timeline                                              |
| `--video-audio FILE`| use this file as the video's audio (when the video file has none)   |

Examples:

```
# see what it would do
python autoedit.py "Silent Running.mp3" "Silent Running (Official Video).webm" --plan

# render, with the album audio, cropped
python autoedit.py album.mp3 video.webm out.mp4 --audio --crop

# hand the edit to a video editor to fine tune
python autoedit.py album.mp3 video.webm --xml timeline.xml --v3 timeline.v3
```

### Edit log

Every plan comes with a log of what was done, in source (video) times:

```
segment 0:00.000-1:37.476 change speed by -0.48%
segment 1:37.476-1:45.747 was cut
segment 2:22.312-2:49.791 was cut
at 0:18.608, the fragment 0:02.900-0:18.619 was inserted
segment 3:51.905-4:01.561 was cut (outro)
```

- **cut**: that source range never reaches the output (auto-editor's `--cut`)
- **change speed by N%**: that source range plays at `1 + N/100` speed
  (auto-editor's `--set-speed SPEED,START,STOP`)
- **inserted**: at that output time, the source fragment plays again.
  auto-editor has no equivalent (it never repeats footage).

The cuts and speed changes are also printed as auto-editor options.

### Fine tuning in an editor

`--xml` writes the same Final Cut Pro 7 XML that auto-editor's
`--export premiere` makes: one clip per segment on V1, speed changes as
Time Remap filters, and the album audio on A1 so you can see and hear where
it drifts. Import it in Premiere Pro (File > Import), DaVinci Resolve
(File > Import > Timeline) or Kdenlive.

`--v3` writes an [auto-editor](https://github.com/WyattBlue/auto-editor)
v3 timeline, which auto-editor can render or convert to other editors:

```
auto-editor timeline.v3 -o out.mp4
auto-editor timeline.v3 --export resolve -o timeline.fcpxml
```

## Aspect crop

```
python aspectcrop.py VIDEO [--coverage 0.9] [--fps 2]
```

Scans the whole video (2 frames a second) and measures the picture in every
frame, then picks the tightest standard ratio that still contains it: 16:9,
1.85 flat, 2.00 Univisium, 2.20 70mm, 2.35 / 2.39 Scope, 2.55 CinemaScope,
2.76 Ultra Panavision, plus 4:3 and narrower for pillarboxed videos. It snaps
outwards, so a thin black line can stay but no picture is lost. It prints
how many frames fit in each ratio.

Videos that change ratio along the way (Blinding Lights) keep every frame
whole by default. `--coverage 0.9` picks the crop that fits 90% of frames,
cutting the edges of the widest 10%.

Cropping saves pixels (smaller output, faster decoding), but black bars are
already nearly free to encode, so don't expect a much smaller file at the same
quality.

## In BackstageHero

For songs in Rock Band CON packs, the auto-edit runs after every download,
using the song audio Nautilus decrypts. If the video needs more than a single
offset, the song shows **Review download (auto-edit, N edits)**, and the MP4 /
WebM renders rebuild the video from the plan (same codecs, watermark and tag
as the normal renders, with the video's own audio cut the same way). If it
only needs an offset that the first sync missed, that offset is used.

The plan and its edit log go into the music video database when you share
the video. See [MVDB.md](MVDB.md).

Clone Hero song folders still use `video_start_time` only; use the command
line to auto-edit those for now.
