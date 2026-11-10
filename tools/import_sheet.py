"""
Import the "Clone Hero Background Video (for Music Videos) Time Codes" sheet
(by moocartoons and contributors) into mvdb/.

  1. Open the sheet, File > Download > Comma-separated values (.csv)
  2. python tools/import_sheet.py sheet.csv

Columns: Artist, Song, Charter, Source, Offset, Video Source, Sync Credit, Notes.
Artist/Song are blank on rows that continue the one above. The sheet has no
YouTube links, so imported videos have an empty "youtube" and keep the
sheet's label; fill in the ID once someone finds the exact video.
"""
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mvdb  # noqa: E402

SHEET = 'https://docs.google.com/spreadsheets/d/1QJ7wotWFoNNSwzIIIAGRcf4WAaYz1pXjhljYSk5h9DI'


def rows(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        r = list(csv.reader(f))
    # header row is the one whose second cell is "Artist"
    start = next(i for i, row in enumerate(r) if len(row) > 2 and row[1].strip() == 'Artist')
    artist = song = charter = source = ''
    for row in r[start + 2:]:
        row = (row + [''] * 10)[:10]
        _, a, s, ch, src, off, label, credit, notes = [c.strip() for c in row[:9]]
        if not any((a, s, off, label)):
            continue
        if a:
            artist, song, charter, source = a, '', '', ''
        if s:
            song, charter, source = s, '', ''
        charter = ch or charter
        source = src or source
        yield artist, song, charter, source, off, label, credit, notes


def main(path):
    db = mvdb.load()
    added = 0
    for artist, song, charter, source, off, label, credit, notes in rows(path):
        m = re.fullmatch(r'-?\d+', off)
        offset = int(off) if m else None
        if offset is None and off:
            notes = (off + '. ' + notes).strip(' .') if notes else off
        video = mvdb.make_video(
            '', mvdb.video_type(label), title=label, offset_ms=offset, notes=notes,
            sync_credit=credit, from_sheet=SHEET,
            submitted_by='moocartoons sheet')
        entry = {'artist': artist, 'title': song}
        if charter:
            entry['charter'] = charter
        # several charts of the same song can have their own offsets: keep the
        # sheet's source text so the right one can be told apart
        if source:
            video['chart_source'] = source
        added += mvdb.add(db, mvdb.source_key(source), entry, video)
    mvdb.save(db)
    print(f'imported {added} video(s)')
    for k, g in sorted(db.items()):
        print(f"  {k:12s} {len(g['songs']):4d} songs")


if __name__ == '__main__':
    main(sys.argv[1])
