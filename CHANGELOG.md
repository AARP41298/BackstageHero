# Changelog

## 2.4.1 (2026-11-26)

- Music video database: 153 more songs, 171 more video links. Most are
  customs (UGC+, +108 songs); also RB1 / RB2 / RB3, their DLC (new: RB2 DLC
  and RB3 DLC), RB4 DLC and The Beatles: Rock Band.

## 2.4.0 (2026-11-26)

- **🔍 Search again** and the **frame collage** are gone: every video link
  is set by hand in the @ window now. Videos marked wrong before are still
  skipped by the search.
- **👁** is now the Preview button. It's hidden once the song is exported
  with that edit.
- 🎧 is red while a song has no exported video and green once it has one.
- The actions are in a new order: @, ⬇, ▶, 👁, Editor, Queue as MP4,
  Queue as WebM, 🎧, 🗑.
- **Auto-mix priority...** has a checkbox per video to leave it out of the
  mix (at least two stay on).
- When an auto-mix's videos changed since the download (one added, removed,
  turned off or moved), 👁 Preview offers to make the mix again and previews
  it when it's ready. Making a mix again (also with ⬇) only downloads the
  videos it doesn't have yet.
- The original Rock Band songs' **crowd** (the audience cheering and singing
  along) is left out of the CON audio for the sync, the auto-edit, 🎧,
  Preview and Editor: it isn't in any video and threw the matching off.
  Kept downloads are re-checked with the new audio when exported or with
  ↺ Auto-sync.
- **Editor** next to Preview: for the songs the auto-edit can't get right,
  asks the editor (Premiere Pro, DaVinci Resolve, Final Cut Pro, Shotcut or
  auto-editor, the formats auto-editor's `--export` has) and a folder, then
  makes a folder with the video (every video for an auto-mix), the CON audio
  and the edit as a timeline, plus a HOW TO.txt. The videos' Opus audio is
  converted to AAC so Premiere can import them, and the CON audio is a
  normal stereo WAV laid out the way Premiere expects. Timelines of 29.97 fps
  videos no longer drift (the rate was rounded to 30).
- **Auto-mix priority...** is per song now: it lists only the song's linked
  videos, each meme link on its own row with its YouTube title, and marks
  the ones the downloaded mix couldn't use (not downloadable, or no part
  fits). The order applies to the next ⬇ Download.
- **☰ Queue**: click a song's format (WEBM / MP4) to switch it, with the
  same replace / keep both questions. A song waiting for its turn keeps its
  place; the one exporting can't be switched.
- **Preview** next to Queue as MP4 / WebM: a quick 480p render of the edit
  (about 10-20 s instead of minutes), played in the 🎧 window with the CON
  audio on the left and the video's audio on the right. It's only for
  checking the sync: nothing is exported next to the CON.
- **Duplicates** filter: shows the songs that are in more than one place (a
  CON and a song folder, two packs, a backup folder), and lists every copy
  in the log.
- Auto-edit fixes: when the video starts after the song (the chart's
  count-in), the edit now starts later instead of slowing the whole video
  down (Rap God was 3 s early at the start; ~40 cached songs had this). The
  count-in's stick hits no longer count as music, so the count-in stays
  black instead of reusing footage (The Real Slim Shady). The speed of each
  piece ignores the count-in and the outro, so a video at the song's own
  tempo isn't slowed a little (Quevedo, Ahora te Puedes Marchar).
- **↺ Auto-sync** on CON songs with a kept download re-checks their
  auto-edit (one at a time, the button stops it). Songs whose exported video
  has an older edit go into the export queue as the same format: select all,
  ↺ Auto-sync, then Start export. Exporting a kept download also re-runs an
  outdated auto-edit first.
- Export queue: **Queue as MP4** / **Queue as WebM** (MP4 no longer exports
  right away). **Start export** renders the queue one at a time and keeps
  going with songs queued later, **Pause export** stops after the current
  file. **☰ Queue** lists what's queued and exporting. Folder / pack rows say what's exporting
  or queued inside them, even when closed.
- Queuing a song that already has a video of the other format (MP4 / WebM)
  asks whether to replace it or keep both. The log shows each export's size,
  MB per minute and render time, next to the other format's size.
- Closing the app while a video exports asks: quit now, after the current
  file, or after the queue.
- **Expand all** / **Collapse all** buttons for the folder and pack groups.
- 🎧 on every CON song: without a video it plays the CON audio.
- Search ignores accents ("zoe" finds "Zoé").
- A song can have several meme links: the @ window lists each one (✎ Edit,
  🗑, ☆) and **+ Add meme link** adds another. The ☆ picks the meme used for
  the meme type; auto-mix uses them all.
- **auto-mix** in the Video type menu, for CON songs with 2+ video links:
  the official video is the base and the clips that use the song (a movie
  scene, a game intro) are put in wherever their audio fits the song, each
  part once. **Auto-mix priority...** sets the order (meme > fan > alternate
  > official by default, the top one wins). Clips of another size are scaled
  and padded into the base's frame. `autoedit.py --mix CLIP` does the same
  from the command line (also for Clone Hero song folders).
- Database: Shrek (All Star), Scooby-Doo (Wanted Dead or Alive), Hot Wheels
  Turbo Racing (Fuel), White Chicks (A Thousand Miles) and The Longest Yard
  (Thunderstruck), with the official videos they mix with.

## 2.3.0 (2026-11-15)

### Rock Band CON packs (YARG)

- Packed Rock Band CON/LIVE files are supported. Videos go next to the CON,
  named after the song ID, the way YARG reads them. The CON itself is never
  touched. Songs and audio are read with
  [Nautilus](https://github.com/NemosNautilus/Nautilus) (a build with the
  `-backstagehero` command line).
- CON songs are reviewed before they are rendered: ▶ play, ▦ frame collage,
  🔍 search again, ⬇ download the chosen video type, 🎧 hear the video against
  the CON audio (level meters, yellow frame), MP4 / WebM export, 🗑 clear
  cache and @ video links.
- WebM export uses YARG's recommended settings (VP8 2-pass, keyframe every
  128 frames, Opus 128k). **Add WebM to queue** and **Export N WebMs** render
  them in a batch; **Shut down when done** turns the PC off after the batch,
  with a 60 s countdown you can cancel.
- The original download is kept in a cache, so you can export an MP4 to
  check, then the WebM, without downloading again.
- The **▾** next to Start picks the mode: Search & Review, Download → WebM or
  Download → MP4 (each synced download is exported right away).
- CON packs are re-listed when the Nautilus build changes; CON files with
  dots in their names are found.

### Auto-edit

- When an offset isn't enough (a longer intro, a skit, a missing chorus, a
  slightly different tempo), the video is mapped onto the song audio and
  rebuilt: extra parts cut, missing parts filled with matching footage,
  speed adjusted. Every edit is logged in plain words.
- Intro and outro rules: the video's own pre-roll and ending come first; a
  silent count-in the video can't cover stays black; otherwise the first or
  last frame is held (up to 3 s) or footage is reused.
- From the command line it works on any video/audio pair and exports a
  Premiere/Resolve XML or an auto-editor timeline. See
  [AUTOEDIT.md](AUTOEDIT.md).

### Rendering

- Black bars (letterbox or pillarbox) are cropped and snapped to a standard
  ratio. The last 10 s of the video aren't measured, so end screens and
  credits don't change the crop.
- Rendered videos keep their audio track, get a small watermark for the first
  3 seconds and are tagged as made by BackstageHero.
- Videos BackstageHero didn't make show ⚠ in the Status column, and replacing
  one asks twice.

### Music video database

- [mvdb/](mvdb/) lists which video goes with which chart, with four types:
  official, alternate, fan and meme. It starts from the community Clone Hero
  Background Video Time Codes sheet, with ~570 syncs, plus 101 new videos and
  more than 200 entries linked to their CON song IDs.
- A song with a database link downloads that video without searching
  YouTube. The default type is the director's choice, else fan > official >
  alternate > meme; the Video type column picks another.
- The @ window lists a song's links: edit or set a link, ☆ set your own
  director's choice, 🗑 remove a bad link, search on YouTube. Only one @ window
  opens at a time.
- **⇪ Missed entries** exports the links you set and the videos you rendered
  to `missed_entries.json`, to send back to the database
  ([MVDB.md](MVDB.md), [MISSED_ENTRIES.md](MISSED_ENTRIES.md)).
- Vimeo links are supported (with `vimeo_cookies.txt` in the data folder).
- Name matching tells versions of a song apart; The Beatles customs default
  to The Beatles as the artist.

### App

- The song table groups songs by folder and CON pack, collapsed by default;
  a search opens every group. Only the checkbox selects a row.
- Folders with a `.yargignore` file are skipped, like YARG does.
- A loading window shows while the library is scanned.
- **Select with links** selects the songs whose video type has a database
  link.
- Open folder button next to Change folder; Show log button.
- Link dialogs have a right-click menu and a Paste button.
- Bigger fonts; eye icon for the collage action.
- App updates are checked on the AARP41298 fork.

### Fixes

- Sync: only peaks beyond the drift window count as rivals.
- WebM: 2-pass options apply to the video stream only.
- yt-dlp updates that need a newer Python than the exe are skipped.
- The Listen window opens in front; no Tcl error when a scan ends fast.
- The build stops when a requirement is missing for the Python running it.

## 2.2.0

Smarter sync: rejects different cuts and looped-song false locks, reuses
extractions, verifies updates.

## 2.1.1

Search prefers the requested resolution.

## 2.1.0

Embedded preview in the sync editor.

## 2.0.0

GUI, audio fingerprinting, community offsets, auto-updater.
