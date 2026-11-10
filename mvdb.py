"""
Music video database: which video goes with which chart, and how to sync it.

One JSON file per song source in mvdb/, keyed like Rock Band 3 Deluxe's
dx_locale_sources.dta (rb3dlc, ugc2, c3customs, gh3, ...), so a song's
game_origin picks its file. See MVDB.md for the format.

Video types:
  official   the artist's official music video (or official lyric video)
  alternate  not the music video, but a version that matches the album audio:
             live performances, visualizers, live visuals
  fan        fan-made video or a fan edit synced to the album audio
  meme       a TV/movie/game clip or meme set to the song (e.g. a South Park
             scene edited to Carry on Wayward Son)

Contributions: the app writes missed_entries.json with every video you synced
that the database doesn't have yet; submit it and it gets merged in.
"""

import datetime
import json
import os
import re
import sys

TYPES = ('official', 'alternate', 'fan', 'meme')

# RB3DX source keys (dx_locale_sources.dta) plus the vanilla game_origin
# values that file doesn't list.
SOURCES = {
    'rb1': 'Rock Band', 'rb2': 'Rock Band 2', 'rb3': 'Rock Band 3',
    'lego': 'LEGO Rock Band', 'greenday': 'Green Day: Rock Band',
    'beatles': 'The Beatles: Rock Band', 'beatles_dlc': 'The Beatles: Rock Band DLC',
    'dlc': 'Rock Band 1/2 DLC', 'rb3dlc': 'Rock Band 3 DLC',
    'ugc1': 'Rock Band Network 1.0', 'ugc2': 'Rock Band Network 2.0',
    'blitz': 'Rock Band Blitz', 'pearljam': 'Pearl Jam: Rock Band',
    'rb4': 'Rock Band 4', 'rb4dlc': 'Rock Band 4 DLC', 'rbvr': 'Rock Band VR',
    'rb4_rivals': 'Rock Band Rivals', 'lost_dlc': 'Lost Rock Band DLC',
    'rbtp_vol_1': 'Track Pack Vol. 1', 'rbtp_vol_2': 'Track Pack Vol. 2',
    'rbtp_acdc': 'AC/DC Live Track Pack', 'rbtp_classic_rock': 'Classic Rock Track Pack',
    'rbtp_country_1': 'Country Track Pack', 'rbtp_country_2': 'Country Track Pack 2',
    'rbtp_metal': 'Metal Track Pack',
    'gh1': 'Guitar Hero', 'gh2': 'Guitar Hero II', 'gh2dlc': 'Guitar Hero II DLC',
    'gh80s': 'Guitar Hero Encore: Rocks the 80s', 'gh3': 'Guitar Hero III: Legends of Rock',
    'gh3dlc': 'Guitar Hero III DLC', 'gha': 'Guitar Hero: Aerosmith',
    'ghwt': 'Guitar Hero: World Tour', 'ghwtdlc': 'Guitar Hero: World Tour DLC',
    'ghm': 'Guitar Hero: Metallica', 'ghsh': 'Guitar Hero: Smash Hits',
    'gh5': 'Guitar Hero 5', 'gh5dlc': 'Guitar Hero 5 DLC', 'bh': 'Band Hero',
    'ghvh': 'Guitar Hero: Van Halen', 'ghwor': 'Guitar Hero: Warriors of Rock',
    'ghwordlc': 'Guitar Hero: Warriors of Rock DLC', 'ghl': 'Guitar Hero Live',
    'c3customs': 'C3 Customs', 'customs': 'Custom Songs',
    'ugc_rv': 'RhythmVerse Customs', 'ch': 'Clone Hero',
}
DEFAULT_SOURCE = 'customs'

# The original "Clone Hero Background Video Time Codes" sheet names sources
# freely; this maps its spellings onto the keys above.
_SOURCE_ALIASES = [
    (r'^rock band 3 dlc$', 'rb3dlc'),
    (r'^rock band (1|2)? ?dlc$|^rock band 1/2 dlc$', 'dlc'),
    (r'^rock band( 1)?$', 'rb1'), (r'^rock band 2$', 'rb2'), (r'^rock band 3$', 'rb3'),
    (r'^rock band 4 dlc$', 'rb4dlc'), (r'^rock band 4$', 'rb4'),
    (r'^rock band net', 'ugc2'), (r'^rock band blitz$', 'blitz'),
    (r'^lego rock band$', 'lego'), (r'^green day', 'greenday'),
    (r'^the beatles', 'beatles'),
    (r'world tour dlc|gh wt dlc', 'ghwtdlc'), (r'world tour', 'ghwt'),
    (r'(warriors of rock|wor)\b.*dlc', 'ghwordlc'), (r'warriors of rock|\bwor\b', 'ghwor'),
    (r'guitar hero 5 dlc', 'gh5dlc'), (r'guitar hero 5', 'gh5'),
    (r'guitar hero 3|legends of rock', 'gh3'), (r'aerosmith', 'gha'),
    (r'^band hero', 'bh'), (r'^c3 ', 'c3customs'),
]


def source_key(name):
    """Map a source name (or an existing key) to a source key."""
    n = (name or '').strip().lower()
    if n in SOURCES:
        return n
    for pat, key in _SOURCE_ALIASES:
        if re.search(pat, n):
            return key
    return DEFAULT_SOURCE


def video_type(label):
    """Guess the type from a free-text video description."""
    l = (label or '').lower()
    if re.search(r'fan|tribute|edit|recreation|compilation|unofficial', l):
        return 'fan'
    if re.search(r'live|visuali[sz]|visual|performance|session|rehearsal|lyric', l):
        return 'alternate'
    if re.search(r'tv|movie|film|clip|anime|opening|\bop\b|trailer|game|meme|credits|scene', l):
        return 'meme'
    return 'official'


def youtube_id(url):
    m = re.search(r'(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})', url or '')
    if m:
        return m.group(1)
    return url if re.fullmatch(r'[A-Za-z0-9_-]{11}', url or '') else ''


def norm(text):
    """Loose key for matching titles: lowercase, no punctuation/feat./brackets."""
    t = (text or '').lower()
    t = re.sub(r'\s*[\(\[].*?[\)\]]', '', t)
    t = re.sub(r'\b(feat|ft)\.?\b.*$', '', t)
    t = re.sub(r'^the\s+', '', t)
    return re.sub(r'[^a-z0-9]+', '', t)


# ---- storage --------------------------------------------------------------------

def db_dir():
    """mvdb/ next to the app (inside the exe bundle when frozen)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'mvdb')


def _empty(source):
    return {'source': source, 'name': SOURCES.get(source, source), 'songs': []}


def load(path=None):
    """{source_key: {'source','name','songs':[...]}} for every file in mvdb/."""
    path = path or db_dir()
    out = {}
    if not os.path.isdir(path):
        return out
    for name in sorted(os.listdir(path)):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(path, name), encoding='utf-8') as f:
                data = json.load(f)
            out[data.get('source') or name[:-5]] = data
        except Exception as e:
            print(f'mvdb: skipping {name}: {e}')
    return out


def save(db, path=None):
    path = path or db_dir()
    os.makedirs(path, exist_ok=True)
    for key, data in db.items():
        data['songs'].sort(key=lambda s: (norm(s.get('artist')), norm(s.get('title'))))
        with open(os.path.join(path, key + '.json'), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write('\n')


def find_song(db, con_id='', artist='', title='', source=None):
    """The song entry for a chart: by CON id first, then artist + title."""
    groups = [db[source]] if source in db else db.values()
    if con_id:
        for g in groups:
            for s in g['songs']:
                if s.get('con_id') == con_id:
                    return s
    a, t = norm(artist), norm(title)
    if not t:
        return None
    for g in groups:
        for s in g['songs']:
            if norm(s.get('title')) == t and (not a or norm(s.get('artist')) == a):
                return s
    return None


def videos_for(db, con_id='', artist='', title='', source=None, types=TYPES):
    """Videos known for a chart, best first: official, alternate, fan, meme."""
    s = find_song(db, con_id, artist, title, source)
    if not s:
        return []
    vids = [v for v in s.get('videos', []) if v.get('type', 'official') in types]
    return sorted(vids, key=lambda v: types.index(v.get('type', 'official')))


def has_video(db, video_id, con_id='', artist='', title='', source=None):
    s = find_song(db, con_id, artist, title, source)
    return bool(s) and any(v.get('youtube') == video_id for v in s.get('videos', []))


def make_video(video_id, vtype='official', title='', offset_ms=None, autoedit=None,
               crop=None, submitted_by='', notes='', **extra):
    """A video record. autoedit is an autoedit.plan() result with its 'log'."""
    v = {'type': vtype if vtype in TYPES else 'official', 'youtube': video_id}
    if title:
        v['title'] = title
    if offset_ms is not None:
        v['offset_ms'] = int(offset_ms)
    if autoedit:
        v['autoedit'] = {
            'segments': [{k: sg[k] for k in ('ref_start', 'ref_end', 'vid_start',
                                             'vid_end', 'speed')}
                         for sg in autoedit['segments']],
            'log': [e['text'] for e in autoedit.get('log', [])],
        }
    if crop:
        v['crop'] = crop
    if submitted_by:
        v['submitted_by'] = submitted_by
    if notes:
        v['notes'] = notes
    v.update({k: val for k, val in extra.items() if val not in (None, '')})
    return v


def add(db, source, song, video):
    """Add a video to a song (creating source/song as needed). Returns True if new."""
    source = source_key(source)
    group = db.setdefault(source, _empty(source))
    s = find_song({source: group}, song.get('con_id', ''), song.get('artist', ''),
                  song.get('title', ''), source)
    if s is None:
        s = {k: song[k] for k in ('con_id', 'artist', 'title', 'charter') if song.get(k)}
        s['videos'] = []
        group['songs'].append(s)
    elif song.get('con_id') and not s.get('con_id'):
        s['con_id'] = song['con_id']
    for old in s['videos']:
        if old.get('youtube') == video.get('youtube') and \
                old.get('offset_ms') == video.get('offset_ms'):
            return False
    s['videos'].append(video)
    return True


# ---- contributions -------------------------------------------------------------

def missed_entries(db, local, submitted_by):
    """Entries from `local` (list of {'source', 'song', 'video'}) that the
    database doesn't have, ready to write as missed_entries.json."""
    out = []
    for item in local:
        song, video = item['song'], item['video']
        vid = video.get('youtube')
        if not vid or has_video(db, vid, song.get('con_id', ''), song.get('artist', ''),
                                song.get('title', '')):
            continue
        video = dict(video, submitted_by=submitted_by)
        out.append({'source': source_key(item.get('source')), 'song': song, 'video': video})
    return out


def write_missed(path, entries, submitted_by):
    data = {'format': 'backstagehero-mvdb-submission', 'version': 1,
            'submitted_by': submitted_by,
            'created': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'entries': entries}
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write('\n')
    return path


def merge_submission(db, path):
    """Merge a missed_entries.json into the database. Returns how many were new."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    n = 0
    for e in data.get('entries', []):
        video = dict(e['video'])
        video.setdefault('submitted_by', data.get('submitted_by', ''))
        n += add(db, e.get('source'), e['song'], video)
    return n


if __name__ == '__main__':
    # python mvdb.py merge missed_entries.json [more.json ...]
    if len(sys.argv) >= 3 and sys.argv[1] == 'merge':
        db = load()
        total = sum(merge_submission(db, p) for p in sys.argv[2:])
        save(db)
        print(f'merged {total} new video(s)')
    else:
        db = load()
        for k, g in db.items():
            print(f"{k:12s} {len(g['songs']):5d} songs  {g['name']}")
