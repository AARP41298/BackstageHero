# Music video database (mvdb)

Which video goes with which chart, and how to sync it. Not only the official
music videos: also the live versions, visualizers, fan edits and meme videos
that match the album audio.

## Files

One JSON file per song source in [`mvdb/`](mvdb/), named with the source
keys of Rock Band 3 Deluxe's
[`dx_locale_sources.dta`](https://github.com/hmxmilohax/rock-band-3-deluxe/blob/develop/_ark/dx/locale/dx_locale_sources.dta)
(the song's `game_origin`), plus the vanilla games that file doesn't list:

| file             | source                         |
|------------------|--------------------------------|
| `rb1.json`, `rb2.json`, `rb3.json` | Rock Band 1 / 2 / 3 on-disc |
| `dlc.json`       | Rock Band 1/2 DLC              |
| `rb3dlc.json`    | Rock Band 3 DLC                |
| `ugc2.json`      | Rock Band Network              |
| `c3customs.json` | C3 Customs                     |
| `customs.json`   | other custom songs             |
| `gh3.json`, `ghwt.json`, ... | Guitar Hero games  |

The full list is `SOURCES` in [`mvdb.py`](mvdb.py).

## Format

```json
{
 "source": "customs",
 "name": "Custom Songs",
 "songs": [
  {
   "con_id": "silentrunning",
   "artist": "Gorillaz",
   "title": "Silent Running",
   "charter": "...",
   "videos": [
    {
     "type": "official",
     "youtube": "_0Pf48RqSsg",
     "title": "Gorillaz - Silent Running ft. Adeleye Omotayo (Official Video)",
     "offset_ms": 0,
     "autoedit": {
      "segments": [{"ref_start": 0.0, "ref_end": 18.608, "vid_start": 0.0, "vid_end": 18.619, "speed": 1.0006}],
      "log": ["at 0:18.608, the fragment 0:02.900-0:18.619 was inserted"]
     },
     "crop": "crop=1920:816:0:132",
     "reference_audio": "GSLdiC48Q7Q",
     "submitted_by": "AARP41298",
     "notes": "..."
    }
   ]
  }
 ]
}
```

Song fields:

| field     | meaning                                                          |
|-----------|------------------------------------------------------------------|
| `con_id`  | the song's ID (shortname) in Rock Band CON packs, e.g. `creep`   |
| `artist`, `title`, `charter` | as in the chart                               |
| `videos`  | every known video, best first                                    |

Video fields:

| field          | meaning                                                     |
|----------------|-------------------------------------------------------------|
| `type`         | `official`, `alternate`, `fan` or `meme` (below)            |
| `youtube`      | YouTube video ID (empty when only the description is known) |
| `title`        | video title or description                                  |
| `offset_ms`    | `video_start_time`; 0 when `autoedit` is set (the edited video starts with the song) |
| `autoedit`     | segments + edit log from [auto-edit](AUTOEDIT.md)           |
| `crop`         | ffmpeg crop filter from `aspectcrop.py`                     |
| `reference_audio` | the audio the offset/auto-edit was measured against (YouTube ID), when it isn't the chart's |
| `chart_source` | which chart the offset is for, when one song has several    |
| `submitted_by` | who contributed it                                          |
| `sync_credit`  | who originally synced it (imported entries)                 |
| `notes`        | anything else                                               |

### Types

- **official**: the artist's official music video (or official lyric video)
- **alternate**: not the music video, but a version that matches the album
  audio: live performances, official visualizers, live visuals
- **fan**: a fan-made video or fan edit synced to the album audio
- **meme**: a TV, movie or game clip, or a meme, set to the song. For
  example the official video of Kansas' *Carry on Wayward Son* is
  [P5ZJui3aPoQ](https://www.youtube.com/watch?v=P5ZJui3aPoQ), and there's a
  South Park scene edited to it:
  [cSLQDiXtorY](https://www.youtube.com/watch?v=cSLQDiXtorY)

## How BackstageHero uses it

When searching for a song's video, the videos the database lists for it
(by CON ID, then artist + title) are tried before the YouTube search results,
official first.

## Contributing

Nobody is going to download every music video, so the database grows with
what each person syncs for the songs they like.

1. Sync your videos in BackstageHero as usual.
2. Click **⇪ Missed entries** in the bottom bar. The first time it asks for
   the name to credit you with.
3. It writes `missed_entries.json` with every video in your library that the
   database doesn't have yet: rendered CON songs (with their auto-edit) and
   song folders that have a `backstagehero_source` in `song.ini`.
4. Check the `type` (everything starts as `official`) and the `source`
   (everything starts as `customs`), then attach the file to an issue or
   pull request.

To merge submissions:

```
python mvdb.py merge missed_entries.json [more.json ...]
python mvdb.py            # counts per source
```

## Importing the community sheet

The [Clone Hero Background Video Time Codes](https://docs.google.com/spreadsheets/d/1QJ7wotWFoNNSwzIIIAGRcf4WAaYz1pXjhljYSk5h9DI/htmlview)
sheet (moocartoons and contributors) has offsets for ~550 charts, but no
YouTube links. To import it, open it, File > Download > CSV, then:

```
python tools/import_sheet.py sheet.csv
```

Imported videos keep the sheet's description and sync credit with an empty
`youtube`. Fill the ID in once someone finds the exact video.
