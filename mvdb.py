"""
Music video database: which video goes with which chart, and how to sync it.

One JSON file per song source in mvdb/, keyed like Rock Band 3 Deluxe's
dx_locale_sources.dta (rb3dlc, ugc2, c3customs, gh3, ...), so a song's
game_origin picks its file. Big sources (SPLIT_SOURCES, the thousands of
custom songs) are a folder instead, one file per artist initial:
mvdb/customs/g.json has every Gorillaz song. See MVDB.md for the format.

Video types:
  official   the artist's official music video (or official lyric video)
  alternate  not the music video, but a version that matches the album audio:
             live performances, visualizers, live visuals
  fan        fan-made video or a fan edit synced to the album audio
  meme       a TV/movie/game clip or meme set to the song (e.g. a South Park
             scene edited to Carry on Wayward Son)

"directors_choice": true on a video makes it the one used by default; without
it the order is fan > official > alternate > meme. Picking a type in the app
overrides both.

Contributions: the app writes missed_entries.json with every video you synced
that the database doesn't have yet; submit it and it gets merged in.
"""

import datetime
import json
import os
import re
import sys
import unicodedata

TYPES = ('official', 'alternate', 'fan', 'meme')
# which type is used when you haven't picked one: a video marked
# "directors_choice": true first, then this order
PRIORITY = ('fan', 'official', 'alternate', 'meme')

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
    'rb4_rivals': 'Rock Band Rivals', 'rb4_dlc': 'Rock Band 4 DLC', 'rbjapan': 'Rock Band Japan', 'lost_dlc': 'Lost Rock Band DLC',
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
    'c3customs': 'C3 Customs', 'customs': 'Custom Songs', 'rb1_dlc': 'Rock Band 1/2 DLC',
    'ugc': 'Custom Songs', 'ugc_plus': 'Custom Songs', 'ugc_c3': 'C3 Customs',
    'ugc_lost': 'Lost Rock Band Network', 'ugc_rv': 'RhythmVerse Customs',
    'c3legacy': 'Legacy C3 Customs', 'ch': 'Clone Hero',
}
DEFAULT_SOURCE = 'customs'

# sources stored as mvdb/<key>/<initial>.json instead of mvdb/<key>.json.
# Any folder in mvdb/ is read this way; these are also written this way.
SPLIT_SOURCES = {'customs'}

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
    """Map a source name, a songs.dta game_origin, or a key to a source key."""
    n = (name or '').strip().lower()
    if n in SOURCES:
        return n
    # an unknown game_origin (a custom pack's own value) keeps its own file
    if re.fullmatch(r'[a-z0-9]+_[a-z0-9_]+', n):
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


# words in a title's brackets (or after " - ") that make it another recording:
# "19-2000 (Soulchild Remix)" isn't "19-2000", "Run to the Hills (Original
# Version)" isn't the Rock Band 1 cover. "(feat. ...)", "(Part 1)" don't count.
_VERSION_WORDS = re.compile(
    r'\b(remix|mix|live|acoustic|demo|unplugged|instrumental|version|edit|extended|'
    r'cover|re-?record(?:ed|ing)?|reprise|orchestral|piano)\b')


def version_tag(text):
    """The version words of a title, e.g. {'remix'} for "19-2000 (Soulchild
    Remix)"; empty for the plain song. Titles only match with the same tag."""
    t = (text or '').lower()
    extra = ' '.join(re.findall(r'[\(\[](.*?)[\)\]]', t))
    m = re.search(r'\s-\s(.*)$', t)
    if m:
        extra += ' ' + m.group(1)
    return frozenset(w.replace('-', '').replace('recorded', 'record').replace('recording', 'record')
                     for w in _VERSION_WORDS.findall(extra))


def same_title(a, b):
    """Loose title match (norm) that still tells versions apart (version_tag)."""
    return norm(a) == norm(b) and version_tag(a) == version_tag(b)


# ---- storage --------------------------------------------------------------------

def db_dir():
    """mvdb/ next to the app (inside the exe bundle when frozen)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'mvdb')


def _empty(source):
    return {'source': source, 'name': SOURCES.get(source, source), 'songs': []}


def initial(artist):
    """File name (without .json) of an artist in a split source: the first
    letter, ignoring accents and a leading "The" ("The Beatles" -> b);
    "0" for digits, "other" for anything else."""
    a = unicodedata.normalize('NFKD', artist or '')
    a = ''.join(c for c in a if not unicodedata.combining(c))
    n = norm(a)
    if not n:
        return 'other'
    return n[0] if n[0].isalpha() else '0'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load(path=None):
    """{source_key: {'source','name','songs':[...]}} for every file in mvdb/
    (a folder is one source split in several files)."""
    path = path or db_dir()
    out = {}
    if not os.path.isdir(path):
        return out
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isdir(full):
            group = out.setdefault(name, _empty(name))
            for part in sorted(os.listdir(full)):
                if not part.endswith('.json'):
                    continue
                try:
                    data = _read(os.path.join(full, part))
                    group['songs'].extend(data.get('songs', []))
                    if data.get('name'):
                        group['name'] = data['name']
                except Exception as e:
                    print(f'mvdb: skipping {name}/{part}: {e}')
            continue
        if not name.endswith('.json'):
            continue
        try:
            data = _read(full)
            key = data.get('source') or name[:-5]
            if key in out:      # a folder and a file for the same source
                out[key]['songs'].extend(data.get('songs', []))
            else:
                out[key] = data
        except Exception as e:
            print(f'mvdb: skipping {name}: {e}')
    return out


def _write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)


def watch_url(video_id):
    return f'https://www.youtube.com/watch?v={video_id}' if video_id else ''


def _with_url(video):
    """The video with youtube_url (a clickable link, written right after
    youtube) matching its youtube ID. youtube is the one that counts:
    youtube_url is rewritten from it on every save."""
    out = {}
    for k, v in video.items():
        if k == 'youtube_url':
            continue
        out[k] = v
        if k == 'youtube' and v:
            out['youtube_url'] = watch_url(v)
    return out


def save(db, path=None):
    """Write the given sources (only those: pass a subset to touch fewer files)."""
    path = path or db_dir()
    os.makedirs(path, exist_ok=True)
    for key, data in db.items():
        data['songs'].sort(key=lambda s: (norm(s.get('artist')), norm(s.get('title'))))
        for s in data['songs']:
            for v in s.get('videos', []):
                fixed = _with_url(v)     # in place: callers may hold these dicts
                v.clear()
                v.update(fixed)
        folder = os.path.join(path, key)
        single = os.path.join(path, key + '.json')
        if key not in SPLIT_SOURCES and not os.path.isdir(folder):
            _write(single, data)
            continue
        parts = {}
        for s in data['songs']:
            parts.setdefault(initial(s.get('artist')), []).append(s)
        os.makedirs(folder, exist_ok=True)
        for letter, songs in parts.items():
            _write(os.path.join(folder, letter + '.json'),
                   {'source': key, 'name': data.get('name') or SOURCES.get(key, key),
                    'songs': songs})
        # letters left without songs, and the old single file
        for part in os.listdir(folder):
            if part.endswith('.json') and part[:-5] not in parts:
                os.remove(os.path.join(folder, part))
        if os.path.exists(single):
            os.remove(single)


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
            if norm(s.get('title')) == t and same_title(s.get('title'), title) \
                    and (not a or norm(s.get('artist')) == a):
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


# ---- your edits (local layer) ------------------------------------------------
# The bundled database is read-only inside the exe, so what you change in the
# app (links you set or fix, con_ids matched by name) goes to a local file in
# the data dir and is applied on top of it at load time. Those edits are what
# "Missed entries" submits. Running from source, con_id matches are also
# written straight into mvdb/ (that's the maintainer's checkout).

def _data_dir():
    try:
        import updater
        return updater.data_dir()
    except Exception:
        return os.path.join(os.path.expanduser('~'), '.backstagehero')


def local_path():
    return os.path.join(_data_dir(), 'mvdb_local.json')


def load_local():
    try:
        with open(local_path(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save_local(edits):
    path = local_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(edits, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _apply(db, e):
    source = source_key(e.get('source'))
    song = e['song']
    if e['kind'] == 'choice':
        s = (find_song(db, song.get('con_id', ''), song.get('artist', ''), song.get('title', ''),
                       source if source in db else None)
             or find_song(db, song.get('con_id', ''), song.get('artist', ''), song.get('title', '')))
        if s is not None:
            s['chosen'] = e['type']
        return
    if e['kind'] == 'con_id':
        s = find_song(db, '', song.get('artist', ''), song.get('title', ''),
                      source if source in db else None)
        if s is not None and not s.get('con_id'):
            s['con_id'] = song['con_id']
        return
    # kind == 'link': set the YouTube video of one type
    group = db.setdefault(source, _empty(source))
    s = (find_song(db, song.get('con_id', ''), song.get('artist', ''),
                   song.get('title', ''), source if source in db else None)
         or find_song(db, song.get('con_id', ''), song.get('artist', ''), song.get('title', '')))
    if s is None:
        s = {k: song[k] for k in ('con_id', 'artist', 'title') if song.get(k)}
        s['videos'] = []
        group['songs'].append(s)
    vids = [v for v in s['videos'] if v.get('type', 'official') == e['type']]
    target = next((v for v in vids if v.get('youtube') == e.get('old')), None) \
        or next((v for v in vids if not v.get('youtube')), None) \
        or (vids[0] if vids and e.get('old') is not None else None)
    if target is None:
        target = {'type': e['type']}
        s['videos'].append(target)
    if target.get('youtube') and target['youtube'] != e['youtube']:
        # a different video: the old sync data doesn't apply to it
        for k in ('offset_ms', 'autoedit', 'crop', 'title'):
            target.pop(k, None)
    target['youtube'] = e['youtube']
    target['edited'] = True


def load_all():
    """The database with your local edits applied (what the app uses)."""
    db = load()
    for e in load_local():
        try:
            _apply(db, e)
        except Exception as ex:
            print(f'mvdb: skipping local edit: {ex}')
    return db


def song_ref(con_id='', artist='', title=''):
    return {k: v for k, v in (('con_id', con_id), ('artist', artist), ('title', title)) if v}


def resolve(db, con_id='', artist='', title='', source=None, pending=None):
    """The song entry for a chart: by con_id, else by artist + title (the
    chart's source first). A name match gets the con_id linked, saved in the
    local layer (and in mvdb/ when running from source). None = No Entry.
    pending: a list to collect those links in instead of saving each one;
    save them all at once with remember_con_ids()."""
    src = source_key(source) if source else None
    if con_id:
        s = find_song(db, con_id, '', '', None)
        if s is not None:
            return s
    s = None
    if src in db:
        s = find_song(db, '', artist, title, src)
    if s is None:
        s = find_song(db, '', artist, title)
    if s is not None and con_id and not s.get('con_id'):
        s['con_id'] = con_id
        if pending is not None:
            pending.append((s, con_id, src))
        else:
            remember_con_ids(db, [(s, con_id, src)])
    return s


def _source_of(db, song):
    for k, g in db.items():
        if any(x is song for x in g['songs']):
            return k
    return None


def remember_con_ids(db, links):
    """Save con_ids matched by name: [(song entry, con_id, source)]."""
    if not links:
        return
    edits = load_local()
    have = {e['song'].get('con_id') for e in edits if e['kind'] == 'con_id'}
    rows = []
    for song, con_id, src in links:
        key = _source_of(db, song) or src or DEFAULT_SOURCE
        rows.append((key, song, con_id))
        if con_id not in have:
            have.add(con_id)
            edits.append({'kind': 'con_id', 'source': key,
                          'song': song_ref(con_id, song.get('artist', ''), song.get('title', ''))})
    try:
        _save_local(edits)
    except Exception as ex:
        print(f'mvdb: could not save con_ids: {ex}')
    if getattr(sys, 'frozen', False):
        return
    # from source: write them into the repo's files too
    try:
        base = load()
        touched = set()
        for key, song, con_id in rows:
            if key not in base:
                continue
            s = find_song({key: base[key]}, '', song.get('artist', ''), song.get('title', ''), key)
            if s is not None and not s.get('con_id'):
                s['con_id'] = con_id
                touched.add(key)
        if touched:
            save({k: base[k] for k in touched})
    except Exception as ex:
        print(f'mvdb: could not write con_ids to mvdb/: {ex}')


def set_link(db, source, song, vtype, video_id, old=None):
    """Set (or fix) the YouTube video of one type for a song: saved in the
    local layer, applied to `db`, and offered by Missed entries."""
    e = {'kind': 'link', 'source': source_key(source) if source else DEFAULT_SOURCE,
         'song': song_ref(song.get('con_id', ''), song.get('artist', ''), song.get('title', '')),
         'type': vtype, 'youtube': video_id, 'old': old,
         'time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    edits = [x for x in load_local()
             if not (x['kind'] == 'link' and x['type'] == vtype and x['song'] == e['song'])]
    edits.append(e)
    _save_local(edits)
    _apply(db, e)


def link_edits():
    """Your link edits as submission items ({'source','song','video'})."""
    out = []
    for e in load_local():
        if e['kind'] == 'link' and e.get('youtube'):
            out.append({'source': e['source'], 'song': e['song'],
                        'video': make_video(e['youtube'], e['type'])})
    return out


def set_choice(db, source, song, vtype):
    """Which type to use for a song (default: official). Saved locally only:
    it's your preference, not a database change, so it isn't submitted."""
    e = {'kind': 'choice', 'source': source_key(source) if source else DEFAULT_SOURCE,
         'song': song_ref(song.get('con_id', ''), song.get('artist', ''), song.get('title', '')),
         'type': vtype}
    edits = [x for x in load_local()
             if not (x['kind'] == 'choice' and x['song'] == e['song'])]
    edits.append(e)
    _save_local(edits)
    _apply(db, e)


def _best(vids):
    """Of a type's videos: the director's choice, then a linked one, then any."""
    return sorted(vids, key=lambda v: (not v.get('directors_choice'),
                                       not v.get('youtube')))[0] if vids else None


def types_of(song):
    """[(type, video or None)] for every type, in TYPES order."""
    out = []
    for t in TYPES:
        vids = [v for v in (song or {}).get('videos', []) if v.get('type', 'official') == t]
        out.append((t, _best(vids)))
    return out


def default_type(song):
    """The type used when you haven't picked one: the director's choice,
    else the first of PRIORITY (fan > official > alternate > meme) it has."""
    have = available_types(song)
    if not have:
        return None
    dc = [v for v in (song or {}).get('videos', []) if v.get('directors_choice')]
    if dc:
        dc.sort(key=lambda v: not v.get('youtube'))
        return dc[0].get('type', 'official')
    return next(t for t in PRIORITY if t in have)


def directors_choice(song):
    """The type marked as director's choice, or None."""
    for v in (song or {}).get('videos', []):
        if v.get('directors_choice'):
            return v.get('type', 'official')
    return None


def available_types(song):
    """Types the entry has a video record for (linked or not)."""
    return [t for t, v in types_of(song) if v is not None]


def chosen_type(song):
    """The type used for a song: your choice, else the default (director's
    choice > fan > official > alternate > meme). None for No Entry."""
    have = available_types(song)
    if not have:
        return None
    pick = (song or {}).get('chosen')
    if pick in have:
        return pick
    return default_type(song)


def chosen_video(song):
    """The video record of the type in use (may have no 'youtube'), or None."""
    pick = chosen_type(song)
    return dict(types_of(song))[pick] if pick else None


def ordered_types(song):
    """types_of() in download order: the chosen type, then PRIORITY order."""
    pick = chosen_type(song)
    items = types_of(song)
    return sorted(items, key=lambda tv: (tv[0] != pick, PRIORITY.index(tv[0])))


def type_summary(song):
    """Video Type column text: the type that will be used, '(no link)' when
    the database has no video for it, and '*' when other types are available."""
    pick = chosen_type(song)
    if pick is None:
        return 'No Entry'
    v = dict(types_of(song))[pick]
    text = pick + (' ★' if v.get('directors_choice') else '')
    text += '' if v.get('youtube') else ' (no link)'
    return text + ('*' if len(available_types(song)) > 1 else '')


def sort_key(song):
    """Sorts entries by the type in use: official, alternate, fan, meme, then
    the ones without a link, No Entry last."""
    pick = chosen_type(song)
    if pick is None:
        return (2, 0)
    v = dict(types_of(song))[pick]
    return (0 if v.get('youtube') else 1, PRIORITY.index(pick))


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


# fields a submitted video can fill in on a video the database already has
_FILL = ('title', 'offset_ms', 'autoedit', 'crop', 'reference_audio', 'pack', 'chart_source')


def merge_entry(db, source, song, video):
    """Merge one submitted video. Returns (status, source key, song entry):
    'new'       added (to a new or an existing song)
    'filled'    the database had this video; missing fields were filled in
    'same'      the database already had it with the same data
    'conflict'  the database has it with a different type/offset: kept as is
    'invalid'   no YouTube ID, or no title / con_id"""
    vid = youtube_id(video.get('youtube'))
    if not vid or not (song.get('title') or song.get('con_id')):
        return 'invalid', None, None
    key = source_key(source)
    # a con_id is the same song whatever source the entry says (a link edit
    # and a render of one song can disagree), then the name in that source
    s = find_song(db, song['con_id'], '', '', None) if song.get('con_id') else None
    if s is not None:
        key = _source_of(db, s) or key
    elif key in db:
        s = find_song(db, '', song.get('artist', ''), song.get('title', ''), key)
    if s is None and key == DEFAULT_SOURCE:
        # song folders always say "customs": the song may live in another source
        s = find_song(db, song.get('con_id', ''), song.get('artist', ''), song.get('title', ''))
        if s is not None:
            key = _source_of(db, s) or key
    if s is None:
        group = db.setdefault(key, _empty(key))
        s = {k: song[k] for k in ('con_id', 'artist', 'title', 'charter') if song.get(k)}
        s['videos'] = []
        group['songs'].append(s)
    elif song.get('con_id') and not s.get('con_id'):
        s['con_id'] = song['con_id']
    video = dict(video, youtube=vid)
    video['type'] = video.get('type') if video.get('type') in TYPES else 'official'
    old = next((v for v in s['videos'] if v.get('youtube') == vid), None)
    if old is None:
        s['videos'].append(video)
        return 'new', key, s
    # renders (they carry the CON 'pack') are always recorded as official:
    # the type of a video the database already has wins over that
    if old.get('type', 'official') != video['type'] and not video.get('pack'):
        return 'conflict', key, s
    if video.get('autoedit') and not old.get('autoedit'):
        # an auto-edited render replaces a plain offset
        old['autoedit'] = video['autoedit']
        old['offset_ms'] = video.get('offset_ms', 0)
        filled = True
    elif ('offset_ms' in old and 'offset_ms' in video and not old.get('autoedit')
          and old['offset_ms'] != video['offset_ms']):
        return 'conflict', key, s
    else:
        filled = False
    for k in _FILL:
        if k in video and k not in old:
            old[k] = video[k]
            filled = True
    return ('filled' if filled else 'same'), key, s


def merge_submission(db, path, report=None):
    """Merge a missed_entries.json into the database. Returns how many videos
    were new. report: a list to collect (status, source key, entry) in."""
    data = _read(path)
    if data.get('format') != 'backstagehero-mvdb-submission':
        raise ValueError(f'{path}: not a missed entries file')
    n = 0
    for e in data.get('entries', []):
        video = dict(e.get('video') or {})
        if not video.get('submitted_by') and data.get('submitted_by'):
            video['submitted_by'] = data['submitted_by']
        status, key, _ = merge_entry(db, e.get('source'), e.get('song') or {}, video)
        n += status == 'new'
        if report is not None:
            report.append((status, key or e.get('source'), e))
    return n


if __name__ == '__main__':
    # python mvdb.py merge missed_entries.json [more.json ...]
    #   (tools/merge_missed.py does the same with a report and --dry-run)
    if len(sys.argv) >= 3 and sys.argv[1] == 'merge':
        db = load()
        total = sum(merge_submission(db, p) for p in sys.argv[2:])
        save(db)
        print(f'merged {total} new video(s)')
    elif sys.argv[1:] == ['split']:
        # rewrite every source in SPLIT_SOURCES as a folder
        db = load()
        save({k: db[k] for k in SPLIT_SOURCES if k in db})
        print('split: ' + ', '.join(sorted(SPLIT_SOURCES)))
    else:
        db = load()
        for k, g in db.items():
            print(f"{k:12s} {len(g['songs']):5d} songs  {g['name']}")
