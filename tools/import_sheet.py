"""
Import the "Clone Hero Background Video (for Music Videos) Time Codes" sheet
(by moocartoons and contributors) into mvdb/.

  1. Open the sheet, File > Download > Web page (.html, zipped)
     (CSV works too, but CSV drops the links: no YouTube IDs then)
  2. python tools/import_sheet.py "Time Codes_.html"

Columns: Artist, Song, Charter, Source, Offset, Video Source, Sync Credit, Notes.
Artist/Song are blank on rows that continue the one above. Video Source
links to the video; Source links to where the chart can be downloaded.

Importing again replaces what an earlier import added (videos marked
from_sheet); con_ids already linked to songs and everything else is kept.
"""
import csv
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mvdb  # noqa: E402

SHEET = 'https://docs.google.com/spreadsheets/d/1QJ7wotWFoNNSwzIIIAGRcf4WAaYz1pXjhljYSk5h9DI'


class _Table(HTMLParser):
    """Rows of (text, href) cells from a Google Sheets HTML export."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell, self._href = [], None, None, ''

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag == 'td' and self._row is not None:
            self._cell, self._href = [], ''
        elif tag == 'a' and self._cell is not None:
            self._href = self._href or dict(attrs).get('href', '')
        elif tag == 'br' and self._cell is not None:
            self._cell.append(' ')

    def handle_endtag(self, tag):
        if tag == 'td' and self._cell is not None:
            self._row.append((''.join(self._cell).strip(), self._href))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _cells(path):
    """Every row as a list of (text, href); the first cell is the blank column A."""
    if path.lower().endswith(('.html', '.htm')):
        p = _Table()
        with open(path, encoding='utf-8') as f:
            p.feed(f.read())
        return p.rows
    with open(path, encoding='utf-8-sig', newline='') as f:
        return [[(c, '') for c in row] for row in csv.reader(f)]


def rows(path):
    r = _cells(path)
    start = next(i for i, row in enumerate(r) if len(row) > 2 and row[1][0] == 'Artist')
    artist = song = charter = source = chart_url = ''
    for row in r[start + 2:]:
        row = (row + [('', '')] * 10)[:10]
        (a, _), (s, _), (ch, _), (src, src_url), (off, _), (label, vurl), (credit, _), \
            (notes, _) = row[1:9]
        if not credit and re.match(r'^(@|r/)|#\d{4}$', label):
            # a row missing its Video Source cell: the credit slid left
            credit, label, vurl = label, '', ''
        if not any((a, s, off, label)):
            continue
        if a:
            artist, song, charter, source, chart_url = a, '', '', '', ''
        if s:
            song, charter, source, chart_url = s, '', '', ''
        if ch:
            charter = ch
        if src:
            source, chart_url = src, src_url
        yield artist, song, charter, source, chart_url, off, label, vurl, credit, notes


def _drop_previous_import(db):
    for g in db.values():
        for s in g['songs']:
            s['videos'] = [v for v in s.get('videos', []) if not v.get('from_sheet')]
        g['songs'] = [s for s in g['songs'] if s['videos'] or s.get('con_id')]


def main(path):
    db = mvdb.load()
    _drop_previous_import(db)
    added = linked = 0
    for artist, song, charter, source, chart_url, off, label, vurl, credit, notes in rows(path):
        m = re.fullmatch(r'-?\d+', off)
        offset = int(off) if m else None
        if offset is None and off:
            notes = (off + '. ' + notes).strip(' .') if notes else off
        vid = mvdb.youtube_id(vurl)
        extra = {}
        if vurl and not vid:
            extra['url'] = vurl          # Vimeo, Instagram, a Drive file...
        if chart_url:
            extra['chart_url'] = chart_url
        if source:
            # several charts of the same song can have their own offsets
            extra['chart_source'] = source
        video = mvdb.make_video(
            vid, mvdb.video_type(label), title=label, offset_ms=offset, notes=notes,
            sync_credit=credit, from_sheet=SHEET, submitted_by='moocartoons sheet', **extra)
        entry = {'artist': artist, 'title': song}
        if charter:
            entry['charter'] = charter
        if mvdb.add(db, mvdb.source_key(source), entry, video):
            added += 1
            linked += bool(vid)
    db = {k: g for k, g in db.items() if g['songs']}
    mvdb.save(db)
    print(f'imported {added} video(s), {linked} with a YouTube link')
    for k, g in sorted(db.items()):
        print(f"  {k:12s} {len(g['songs']):4d} songs")


if __name__ == '__main__':
    main(sys.argv[1])
