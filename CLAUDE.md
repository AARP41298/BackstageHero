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
  ⬇ Download always downloads the chosen type's link. Links are added by
  hand in the @ window: the 🔍 Search again action and the ▦/👁 frame
  collage were removed (2026-11-26); 👁 is now the Preview button. Old
  `bad` ids in con_videos.json are still skipped by the search.
  Replacing a finished video not made by BackstageHero asks twice.
- No Entry songs use the automatic search and never touch the database.
- Exports keep the original download in the cache (export MP4 to check, then
  queue WebM); 🗑 clears one song, the footer button clears all.
  The ▾ next to Start (`_DL_MODES`, saved as `download_mode`): Search &
  Review, or Download → WebM / MP4 which renders each synced CON download
  right away (unsynced ones stay for review). Select with links = chosen
  type has a database link.
  Preview (2026-11-24, before Queue as MP4): `conpack.render_preview`, the
  same cut at 480p x264 ultrafast, no watermark, then `listen_mix` (CON
  audio left, video audio right) into the work dir's preview.mkv, played in
  the 🎧 `ListenWindow` (`mixed=`: no second mix, file kept on close; a new
  Preview closes it first, mpv locks the file). Renders right away on its own thread, not through the
  queue (~10 s for a 5 min song); its ffmpeg runs are `export=False`, so
  the queue's cancel doesn't stop them. Not an export: nothing next to the
  CON, no `rendered`, no contribution.
  Export queue (2026-11-17): Queue as MP4 / Queue as WebM put a song in
  the queue (`Song.queued`). Start export (`_exporting`) renders them one
  at a time (`rendering` = queued / running) and songs queued later go
  straight to the renders; Pause export lets the running one finish and
  puts the waiting ones back in the queue. Queuing a song that has the
  other format next to the CON asks replace / keep both (`keep_other`;
  Download → WebM / MP4 always replaces). Each export logs its size,
  MB/min, render time and the other format's size (`_log_render_size`). ☰ Queue (`QueueWindow`) lists them,
  click shows the song (`_reveal`), a click on a not-started row's format
  switches WebM <-> MP4 (`_switch_format`, same replace / keep both
  questions; a waiting one keeps its place: the render thread reads
  `render_fmt` when its turn comes), ✕ takes one out (on the running one it
  asks, then `conpack.cancel_render` stops ffmpeg; the download stays). Group rows say
  "exporting N · N in queue" in words (bold Segoe UI has no ⟳ / ⏸) and
  turn blue. Closing while rendering (`CloseWhileRendering`): quit now
  (`conpack.kill_renders`), after the current file, after the queue.
  "Shut down when done" (off on every start) shuts Windows down after the
  export batch, with a 60 s countdown that can be cancelled.
- Search ignores accents and case (`_fold`). 🎧 shows on every CON song:
  without a video it plays just the CON audio.
- WebM export settings (VP8 CRF 30, 8M max, 2-pass, keyframe 128,
  `-deadline good -cpu-used 1`, Opus 128k) are the YARG wiki's recommended
  ones: don't change them. Parallel WebM exports were benchmarked
  (2026-11-12, i7-12700, 90 s 1080p clips): 3 jobs only ~1.4x faster than one
  at a time, 2 jobs ~1.3x, since one libvpx export already uses a good part
  of the CPU. Not built; a 90 s clip takes ~3 min, a full song ~8-9 min.
- Renders crop black bars before the watermark. The crop ignores the last
  10 s (max 10% of the video): end screens, credits, closing curtains.
- Auto-edit plans are saved at download with `plan_v`; an export re-plans a
  kept download whose plan is older than `autoedit.PLAN_VERSION` (bump it
  when the rules change; auto-mixes aren't re-planned). Music start
  (`_music`, 2026-11-24): the quietest fifth of the next second above 0.8 %
  of the median energy, so count-in stick hits stay black. v3: a first line
  that starts before the video starts later in the song at its own speed
  (it used to be clamped to 0:00 and slowed: Rap God, 24K Magic, ~40 cached
  songs had a weak first segment `match` > 0.1 from this). v4
  (`_robust_line`): each piece's line is refit on the frames within 0.35 s
  of it, so the count-in hold and an outro past the video don't tilt the
  speed (Quevedo BZRP 52: constant -4.00 s fitted as 0.9976, felt offset at
  the start and again from the middle; now 1.000). v5 (2026-11-25): CON
  audio without the crowd channels.
- Crowd channels (2026-11-25): the original Rock Band songs have a crowd
  (audience cheering / singing along) in the mogg; it isn't in any video,
  so every use of the CON audio leaves it out: `prepare_song_audio` skips
  `crowd_channels(con, song_id)` (read from songs.dta via Nautilus `dta`,
  parsed in Python by `_dta_tree`, so Nautilus didn't need a rebuild;
  cached per CON in memory). Covers download sync, auto-edit, re-plan, 🎧,
  Preview and Editor (its song.wav is always rebuilt). Song folders already
  skipped `crowd.ogg` (`audiosync.EXCLUDE_STEMS`). RB1 Wanted Dead or Alive
  re-planned without the crowd: worst piece match 0.195 -> 0.097.
  ↺ Auto-sync on CON songs with a kept download (`_start_replan`, one at a
  time, the button turns into ■ Stop) runs `conpack.replan`; renders save
  `rendered_segments` (the edit the export used) and `export_outdated` puts
  songs whose export has an older edit into the export queue as the same
  format (only if that video is ours; keeps the other format if it exists).
  Rap God / 24K Magic / Slim Shady got `rendered_segments` by hand on
  2026-11-24 (their old plans had been overwritten by testing).
- Auto-edit intro: the video's own pre-roll first; the silent count-in the
  video can't cover stays black; where the chart audio has music, up to 3 s
  hold the first frame, longer reuses footage from the video's first half.
  Outro: the video's own ending first, then hold (<= 3 s) or reuse from the
  second half. Mid-song inserts are fine (Silent Running).
- auto-mix (`mvdb.AUTO_MIX`, 2026-11-16): a Video type pick, not a video
  type, shown for songs with 2+ linked videos (CON only; song folders show it
  disabled). Downloads every link; base = lowest in `mvdb.MIX_PRIORITY`
  that plans ok (default meme > fan > alternate > official; an old
  `mix_priority` setting still loads), higher ones put in where
  they fit (`autoedit.overlay_pieces` / `mix`). Auto-mix priority...
  (2026-11-25, `MixPriorityWindow`) is per song: one row per linked video
  (each meme link its own), its YouTube title (`_yt_titles`, fetched via
  `_want_title`), ▲▼ save a local `mix_order` edit (YouTube IDs, never
  exported; videos linked later go in by type, before the first lower
  type); Default order drops it. Rows the kept mix doesn't use say so.
  The checkbox (2026-11-26) turns a video off: `off` in the same edit,
  `mvdb.mix_videos` leaves it out, `mix_all` lists every one; at least two
  stay on (otherwise `can_mix` would hide the window). Default order keeps
  `off`. Takes effect on the next ⬇ Download. The download saves
  `mix_links` (the videos the mix was made from, used or not); 👁 Preview
  on an auto-mix song whose links changed (`_mix_outdated`; older states
  compare the `mix` ids) asks: make the mix again then preview
  (`_remix`, `preview_after`), preview the old one, or cancel. A re-mix
  (and ⬇ on an auto-mix song) keeps the work dir: `conpack.stash_downloads`
  moves the kept files to `<work>.reuse` and `_con_automix(reuse=)` moves
  them back instead of downloading (videos that failed or didn't fit were
  deleted, so they download again). A failed mix download is tried
  twice (YouTube gave Scooby-Doo `uen_1bL_TXc` a passing "not available";
  it downloads and 4 parts fit Wanted Dead or Alive). Work dir keeps video.mp4
  (base) + mix_N.mp4; state has `mix` and `base_plan` (what contributions
  share). Thresholds were only checked on synthetic clips (Silent Running
  excerpts under another song), not on the real Shrek / Hot Wheels clips.
- Several links per type (2026-11-16): `link` edits are per link (`old` =
  the link replaced, null = add), `clear` drops only that link's edit,
  `favorite` can carry `youtube` to star one of several. The @ window shows
  them all; + Add is offered for `_MULTI` = ('meme',) only.
- Only the checkbox selects a row.
- Editor (2026-11-25, after Preview): `EditorExportWindow` asks the
  format (`autoedit.EDITOR_FORMATS`: premiere / resolve = FCP7 XML,
  final-cut-pro = FCPXML 1.9, shotcut = MLT, v3), remembered as
  `editor_format`, then a parent folder (`editor_folder`);
  `conpack.export_to_editor` makes `<label> (edit)` with copies of the
  work dir's video(s) (`_editor_copy`: Opus -> AAC 256k, video stream
  copied unless it isn't H.264/HEVC; Premiere can't read Opus), song.wav
  (`_editor_song_audio`: its own 16-bit 48 kHz stereo, peak -1 dB; the
  fingerprinter's song.wav is mono float and Premiere didn't load it), the
  timeline (`autoedit.export_timeline`, mixes supported, NTSC rates exact)
  and HOW TO.txt. FCP7 XML audio is Premiere's layout, as auto-editor
  writes it: `explodedTracks`, sequence audio format, a stereo file = two
  exploded mono tracks; A1/A2 chart audio, A3/A4 the videos' audio linked to
  the video clips, disabled. Plain-offset songs get one piece
  (`editor_segments`). Checked by parsing the files only; the user is
  testing the Premiere import.
- Duplicates filter (2026-11-24, next to All): songs whose artist + title
  (`_dup_key`: folded, no punctuation, ft./feat./featuring the same word; a
  different guest is another song) appear in more than one place; every copy
  is listed in the log.
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
WebM to queue labels, ⬇ action, database links used without searching,
kept-download cache + 🗑, black count-in and intro/outro rules,
`directors_choice` + priority, @ icon, shut down when done.

Open ideas, not done:
- Missed entries: carry the replaced link (`replaces`) so merging swaps it,
  and drop the duplicate when a link edit and a render are the same video.
- Clone Hero song folders only get an offset in the app; auto-edit is CLI-only for them.
