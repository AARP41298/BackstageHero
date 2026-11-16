# BackstageHero: notes for Claude Code

Downloads background videos for Clone Hero song folders and Rock Band CON
packs (YARG), syncs them to the chart audio, and keeps a music video database.
Windows desktop app (customtkinter). Read README.md first; AUTOEDIT.md,
MVDB.md and MISSED_ENTRIES.md document the newer parts.

## Running

```
py -3.12 -m pip install -r requirements.txt
py .\gui.py           # the app (from source)
py .\build.py         # dist\BackstageHero.exe (stops if a requirement is missing)
```

CON support needs the Nautilus fork with the `-backstagehero` CLI:
`C:\Todo\Repos\Nautilus` (branch `master-backstage`, build in Visual Studio,
exe in `Nautilus\bin\Debug`). Its `list` output includes `game_origin`.

## Repo conventions

- Branch: `main-con-suppot` (fork AARP41298/BackstageHero). Commit, don't push
  unless asked.
- The Windows checkout uses CRLF; the index is LF (`core.autocrlf`). Keep
  files CRLF on disk.
- `test/` (sample videos, the sheet export) and `.idea/` are not committed.
- Commit messages: a short summary line, then what changed and why.

## Map of the code

| file | what |
|------|------|
| `gui.py` | the app: song table, actions, @ video links window, footer buttons |
| `VideoDownload.py` | search / select / download pipeline (`process_download` for song folders, `process_con_download` for CON songs), `mode` = auto / db / search |
| `conpack.py` | CON packs via Nautilus: song list cache, review state, render (MP4 / WebM 2-pass), black-bar crop, cache of kept downloads, contributions |
| `autoedit.py` | auto-edit: aligns video to chart audio (chroma + Viterbi), segments, intro/outro rules (`_fix_edges`), edit log, ffmpeg render, FCP7 XML / auto-editor v3 export |
| `aspectcrop.py` | finds letterbox/pillarbox bars over the whole video, snaps to a standard ratio |
| `audiosync.py` | the older single-offset fingerprint sync |
| `mvdb.py` + `mvdb/*.json` | music video database, one JSON per source (RB3DX `game_origin` keys; big ones like `customs/` split per artist initial, `g.json` = Gorillaz); local edit layer `mvdb_local.json`; missed entries export / merge |
| `tools/merge_missed.py` | merges users' `missed_entries.json` into `mvdb/` (dry run by default, `--write` to merge) |
| `tools/import_sheet.py` | imports the community Time Codes sheet (HTML export keeps the links) |

Data folder: `%LOCALAPPDATA%\BackstageHero\` (settings, `con_work\` cache of
downloads, `mvdb_local.json`, `contributions.json`, `youtube_titles.json`).

## Behaviour decided with the user

- Video type default: `directors_choice` video, else fan > official >
  alternate > meme. The user's pick in the Video type menu overrides it.
  The ☆/★ in the @ window sets the user's own `directors_choice` (a local
  `favorite` edit, replaces the database's, clears the type pick); it is
  never exported in Missed entries. 🗑 in the @ window removes a bad link
  (local `clear` edit, not exported either). One @ window at a time.
- A song with a database link downloads that video, no YouTube search.
  🔍 Search again searches YouTube, skips the database links and never marks
  them as wrong. ⬇ Download always downloads the chosen type's link.
  Replacing a finished video not made by BackstageHero asks twice.
- No Entry songs use the automatic search and never touch the database.
- Exports keep the original download in the cache (export MP4 to check, then
  queue WebM); 🗑 clears one song, the footer button clears all.
  The ▾ next to Start (`_DL_MODES`, saved as `download_mode`): Search &
  Review, or Download → WebM / MP4 which renders each synced CON download
  right away (unsynced ones stay for review). Select with links = chosen
  type has a database link.
  WebM is queued (Add WebM to queue) and rendered with "Export N WebMs".
  "Shut down when done" (off on every start) shuts Windows down after the
  WebM batch, with a 60 s countdown that can be cancelled.
- WebM export settings (VP8 CRF 30, 8M max, 2-pass, keyframe 128,
  `-deadline good -cpu-used 1`, Opus 128k) are the YARG wiki's recommended
  ones: don't change them. Parallel WebM exports were benchmarked
  (2026-11-12, i7-12700, 90 s 1080p clips): 3 jobs only ~1.4x faster than one
  at a time, 2 jobs ~1.3x, since one libvpx export already uses a good part
  of the CPU. Not built; a 90 s clip takes ~3 min, a full song ~8-9 min.
- Renders crop black bars before the watermark. The crop ignores the last
  10 s (max 10% of the video): end screens, credits, closing curtains.
- Auto-edit intro: the video's own pre-roll first; the silent count-in the
  video can't cover stays black; where the chart audio has music, up to 3 s
  hold the first frame, longer reuses footage from the video's first half.
  Outro: the video's own ending first, then hold (<= 3 s) or reuse from the
  second half. Mid-song inserts are fine (Silent Running).
- auto-mix (`mvdb.AUTO_MIX`, 2026-11-16): a Video type pick, not a video
  type, shown for songs with 2+ linked videos (CON only; song folders show it
  disabled). Downloads every link; base = lowest in `mvdb.MIX_PRIORITY`
  that plans ok (default meme > fan > alternate > official, saved as
  `mix_priority`, changed in Auto-mix priority...), higher ones put in where
  they fit (`autoedit.overlay_pieces` / `mix`). Work dir keeps video.mp4
  (base) + mix_N.mp4; state has `mix` and `base_plan` (what contributions
  share). Thresholds were only checked on synthetic clips (Silent Running
  excerpts under another song), not on the real Shrek / Hot Wheels clips.
- Several links per type (2026-11-16): `link` edits are per link (`old` =
  the link replaced, null = add), `clear` drops only that link's edit,
  `favorite` can carry `youtube` to star one of several. The @ window shows
  them all; + Add is offered for `_MULTI` = ('meme',) only.
- Only the checkbox selects a row.
- The song table groups by folder (relative to the Songs folder) and CON pack
  (a CON with 2+ songs), collapsed by default; single-song CONs and song
  folders are plain rows in their folder. A folder with just one pack or one
  sub-folder merges into one row. A search opens every group.
- `.yargignore` (any case, YARG.Core commit 639d812e on `master-personal`):
  the folder's files (song.ini, CONs) are skipped, sub-folders still scanned.
  All library scans go through `conpack.find_song_inis` / `find_con_files`.

## State at handoff (2026-11-11)

Everything below is committed, but was only tested with py_compile and
headless unit checks by Claude (the user ran the GUI and confirmed the icons,
the database links and E-Pro's auto-edit): WebM queue, Export as MP4 / Add
WebM to queue labels, 🔍 / ⬇ actions, database links used without searching,
kept-download cache + 🗑, black count-in and intro/outro rules,
`directors_choice` + priority, @ icon, shut down when done.

Open ideas, not done:
- Missed entries: carry the replaced link (`replaces`) so merging swaps it,
  and drop the duplicate when a link edit and a render are the same video.
- Clone Hero song folders only get an offset in the app; auto-edit is CLI-only for them.
