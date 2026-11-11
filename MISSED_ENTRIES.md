# Missed entries: technical notes

How **⇪ Missed entries** builds `missed_entries.json`, what is in it, and how
a submission gets merged into [`mvdb/`](mvdb/). For the database format itself
see [MVDB.md](MVDB.md).

## Overview

```
 sources of local videos            filter                    output
 ------------------------           ------                    ------
 mvdb_local.json  (link edits) --+
 contributions.json (renders)  --+--> not in the bundled  --> missed_entries.json
 song.ini of song folders      --+    database (mvdb/)        (+ submitted_by)
```

Code: `gui.App._export_missed()` and `gui.App._local_videos()` collect the
items, `mvdb.missed_entries()` filters them and `mvdb.write_missed()` writes
the file. `mvdb.merge_submission()` / `python mvdb.py merge` takes it back in.

## Local files

All in the data folder, `%LOCALAPPDATA%\BackstageHero\` (`updater.data_dir()`):

| file                 | written by                         | holds                                                    |
|----------------------|------------------------------------|----------------------------------------------------------|
| `mvdb_local.json`    | @ window, Video type menu, library scan | your edits, applied on top of the bundled database   |
| `contributions.json` | `conpack.record_contribution()` after every CON render | one item per rendered CON song      |
| `settings.json`      | first export                       | `contributor_name`, used as `submitted_by`               |
| `youtube_titles.json`| Open in browser column             | YouTube titles cache (not part of submissions)           |

### `mvdb_local.json`

A list of edits, replayed in order by `mvdb.load_all()`:

```json
[
 {"kind": "link", "source": "rb2",
  "song": {"con_id": "myownworstenemy", "artist": "Lit", "title": "My Own Worst Enemy"},
  "type": "official", "youtube": "sc5iTNVEOAg", "old": null,
  "time": "2026-11-10T23:40:12+00:00"},
 {"kind": "con_id", "source": "rb2",
  "song": {"con_id": "myownworstenemy", "artist": "Lit", "title": "My Own Worst Enemy"}},
 {"kind": "choice", "source": "customs",
  "song": {"artist": "Kansas", "title": "Carry on Wayward Son"}, "type": "meme"}
]
```

| kind     | made by                                   | submitted? |
|----------|-------------------------------------------|------------|
| `link`   | **Set link** / **Edit** in the @ window   | yes        |
| `con_id` | a CON song matched to an entry by name    | no (see limitations) |
| `choice` | picking a type in the Video type menu     | no, it's a personal preference |

Only one `link` per song + type is kept (a new one replaces the previous), and
one `choice` per song.

### `contributions.json`

One item per rendered CON song (re-rendering replaces it):

```json
[
 {"source": "rb2",
  "song": {"con_id": "epro", "artist": "Beck", "title": "E-Pro"},
  "video": {"type": "official", "youtube": "<YouTube ID>", "title": "<video title>",
            "offset_ms": 0, "autoedit": {"segments": [...], "log": [...]},
            "pack": "RB2-Rock-Band-2-Export"}}
]
```

`source` is the song's `game_origin` (Nautilus with `game_origin` in its
list output), else `customs`. `offset_ms` is the render offset, or `0` with
`autoedit` when the video was rebuilt.

## Building the submission

`_local_videos()` collects, in this order:

1. **Link edits**: `mvdb.link_edits()`, every `link` edit with a YouTube ID,
   as a video of that type. No offset.
2. **Renders**: everything in `contributions.json`.
3. **Song folders** (Clone Hero, not CON) with a video: `backstagehero_source`
   from `song.ini` as the YouTube ID, `video_start_time` as `offset_ms`,
   `artist` / `name` / `charter` from `song.ini`, type `official`, source
   `customs`. Folders without `backstagehero_source` (videos added by hand)
   are skipped.

`mvdb.missed_entries(db, items, name)` then drops:

- items without a YouTube ID
- items whose YouTube ID the **bundled** database (`mvdb.load()`, without your
  local edits) already has for that song. The song is found by `con_id`, else
  by artist + title, in any source.

and sets `video.submitted_by` to your name on the rest. Nothing is sent
anywhere: the file is saved where you choose.

## File format

```json
{
 "format": "backstagehero-mvdb-submission",
 "version": 1,
 "submitted_by": "AARP41298",
 "created": "2026-11-10T23:58:03+00:00",
 "entries": [
  {
   "source": "rb2",
   "song": {"con_id": "myownworstenemy", "artist": "Lit", "title": "My Own Worst Enemy"},
   "video": {"type": "official", "youtube": "sc5iTNVEOAg", "submitted_by": "AARP41298"}
  }
 ]
}
```

| field                  | meaning                                                         |
|------------------------|-----------------------------------------------------------------|
| `format`, `version`    | identify the file; bump `version` if the layout changes          |
| `submitted_by`         | the contributor's name (also copied into every video)            |
| `created`              | UTC time of the export                                           |
| `entries[].source`     | mvdb source key (file name in `mvdb/`)                           |
| `entries[].song`       | `con_id`, `artist`, `title` (and `charter` for song folders)     |
| `entries[].video`      | a video record as in MVDB.md: `type`, `youtube`, and when known `title`, `offset_ms`, `autoedit`, `pack` |

Before submitting, check by hand:

- `type`: renders and song folders always start as `official`
- `source`: song folders always start as `customs`

## Merging

```
python mvdb.py merge missed_entries.json [more.json ...]
```

For each entry, `mvdb.add()`:

1. maps `source` with `mvdb.source_key()` (unknown `game_origin` values get
   their own file)
2. finds the song in that source by `con_id`, else artist + title; creates it
   if missing (keeping `con_id`, `artist`, `title`, `charter`)
3. links the `con_id` if the existing song had none
4. skips the video if the song already has one with the same `youtube` **and**
   `offset_ms`; otherwise appends it
5. fills `submitted_by` from the file header if the video lacks it

Then every touched source file is saved (sorted by artist, title). Review the
diff before committing: merging trusts the file.

## Limitations

- **A fixed link is added, not replaced.** **Edit** in the @ window records the
  old ID locally (`old`), but the submission doesn't carry it, so merging adds
  the new video next to the wrong one. Remove the wrong one by hand when
  merging.
- **The same video can appear twice** in one submission (a link edit without
  an offset and a render of it with one). Both pass the dedupe at merge, since
  their `offset_ms` differ.
- **CON IDs matched by name aren't submitted** (`con_id` edits). They're
  re-linked on every machine at scan time, so nothing is lost, but the
  database files don't learn them unless someone runs from source (which
  writes them into `mvdb/` directly).
- **Link edits carry no offset.** The offset comes later, from a render of
  that video.
- **No validation** of the YouTube ID beyond its shape: a private or deleted
  video is accepted.
