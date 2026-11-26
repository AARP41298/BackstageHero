"""
Background videos for songs inside packed Rock Band CON/LIVE files (for YARG).

A CON has to stay intact, so there's no song.ini to put video_start_time in.
Instead the video itself is cut so it starts exactly where the song does, and
saved next to the CON as <song id>.webm, which is the name YARG looks for:

    C:\\...\\RB1\\RB1-Rock-Band-1-Export
    C:\\...\\RB1\\blitzkriegbop.webm
    C:\\...\\RB1\\creep.webm

Reading the CON and decrypting its moggs is done by Nautilus
(`Nautilus.exe -backstagehero ...`), which has to be installed; its path is a
setting. Everything else (search, fingerprint sync, download) is the same code
the Clone Hero flow uses, run against a scratch folder holding the song audio.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

import updater

# Lead-in mixing: every mogg track at -5 dB, summed to mono. Same as the manual
# Audacity flow, and it keeps a 12+ track multitrack from clipping.
TRACK_GAIN = 10 ** (-5 / 20)

# YARG-style WebM: VP8, constant quality, 2-pass, keyframe every 128 frames.
VP8_CRF = '30'
VP8_MAX_BITRATE = '8M'
KEYFRAME_DISTANCE = '128'

CON_MAGICS = (b'CON ', b'LIVE', b'PIRS')
NAUTILUS_TIMEOUT = 300

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

_nautilus_exe = None


def set_nautilus_path(path):
    global _nautilus_exe
    _nautilus_exe = path or None


def nautilus_path():
    return _nautilus_exe


def nautilus_ready():
    return bool(_nautilus_exe) and os.path.isfile(_nautilus_exe)


def is_con_file(path):
    """True for an Xbox 360 STFS package (CON/LIVE). Only the 4-byte magic is read."""
    try:
        if os.path.getsize(path) < 0x1000:
            return False
        with open(path, 'rb') as f:
            return f.read(4) in CON_MAGICS
    except OSError:
        return False


# YARG (AARP41298's YARG.Core, "feat: .YARGignore file to skip folder"): a
# folder with this file (any case) has its files skipped by the song scan,
# its sub-folders are still scanned. BackstageHero lists songs the same way.
YARG_IGNORE = '.yargignore'


def yarg_ignored(files):
    """True if a folder's file names include .yargignore (any case)."""
    return any(f.lower() == YARG_IGNORE for f in files)


def walk_songs(root, hidden=True, on_folder=None):
    """os.walk(root) without the files of folders YARG ignores (.yargignore):
    yields (dirpath, files). hidden=False also skips folders starting with
    '.', like glob's '**'. on_folder(dirpath) is called for every folder
    visited (progress; it may raise to stop the walk)."""
    for dirpath, dirs, files in os.walk(root):
        if on_folder:
            on_folder(dirpath)
        if not hidden:
            dirs[:] = [d for d in dirs if not d.startswith('.')]
        if yarg_ignored(files):
            continue
        yield dirpath, files


def find_song_inis(root, on_folder=None):
    """Every song.ini under root (Clone Hero / YARG song folders), except in
    folders with a .yargignore."""
    for dirpath, files in walk_songs(root, hidden=False, on_folder=on_folder):
        ini = next((f for f in files if f.lower() == 'song.ini'), None)
        if ini:
            yield os.path.join(dirpath, ini)


# file types in a song library that are never CONs, so they're not opened.
# CONs have no extension, but their names often have dots ("Mr. Nice Guy",
# "(ft. Someone"), which splitext reads as one: anything else is checked.
_NOT_CON_EXTS = frozenset((
    '.ini', '.chart', '.mid', '.midi', '.sng', '.ogg', '.opus', '.mp3', '.wav',
    '.flac', '.mogg', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp',
    '.mp4', '.webm', '.mkv', '.avi', '.mov', '.m4a', '.txt', '.json', '.xml',
    '.dta', '.milo_xbox', '.yargsong', '.yarg', '.db', '.bin', '.log',
    '.zip', '.rar', '.7z', '.exe', '.dll', '.pdf', '.lnk', '.url', '.ico',
    '.nfo', '.md', '.html', '.part', '.tmp', '.bak',
))


def find_con_files(root, on_folder=None):
    """CON/LIVE files under root. Files of known types (_NOT_CON_EXTS: song
    audio, charts, videos...) are never opened; the rest get the 4-byte magic
    check. Folders with a .yargignore are skipped (not their sub-folders),
    like YARG."""
    found = []
    for dirpath, files in walk_songs(root, on_folder=on_folder):
        for name in files:
            if name.startswith('.') or \
                    os.path.splitext(name)[1].lower() in _NOT_CON_EXTS:
                continue
            path = os.path.join(dirpath, name)
            if is_con_file(path):
                found.append(path)
    return found


def video_path(con, song_id, ext='webm'):
    """Where YARG expects the background video for one song of a packed CON."""
    return os.path.join(os.path.dirname(con), f'{song_id}.{ext}')


def _run_nautilus(args, out_path):
    if not nautilus_ready():
        raise Exception('Nautilus not set up (needed to read CON files)')
    err = out_path + '.error'
    for p in (out_path, err):
        if os.path.exists(p):
            os.remove(p)
    try:
        # cwd = Nautilus folder so it finds its bin\ files
        r = subprocess.run([_nautilus_exe, '-backstagehero'] + args,
                           cwd=os.path.dirname(_nautilus_exe),
                           timeout=NAUTILUS_TIMEOUT, creationflags=NO_WINDOW)
        code = r.returncode
    except subprocess.TimeoutExpired:
        raise Exception('Nautilus timed out')
    if code != 0 or not os.path.exists(out_path):
        reason = ''
        try:
            with open(err, encoding='utf-8-sig', errors='replace') as f:
                reason = f.read().strip()
            os.remove(err)
        except OSError:
            pass
        raise Exception('Nautilus: ' + (reason or f'failed (exit code {code})'))


# Song lists are cached by file path + size + mtime (and the Nautilus build that
# read them), so rescanning a library of multi-GB packs doesn't re-run Nautilus
# for CONs that haven't changed.
_CACHE_FILE = os.path.join(updater.data_dir(), 'con_cache.json')


def _load_cache():
    try:
        with open(_CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        tmp = _CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def list_songs(cons, progress=None):
    """{con_path: [{'id','name','artist','game_origin'}...]} for each CON, plus {con: error} for
    the ones that couldn't be read. progress(done, total) is called as it goes."""
    cache = _load_cache()
    result, errors = {}, {}
    dirty = False
    # a different Nautilus build may read packs differently, so it re-lists them
    try:
        nst = os.stat(_nautilus_exe)
        tool = f'{nst.st_size}:{int(nst.st_mtime)}'
    except (OSError, TypeError):
        tool = ''
    work = tempfile.mkdtemp(prefix='bh_list_')
    try:
        for i, con in enumerate(cons):
            if progress:
                progress(i, len(cons))
            try:
                st = os.stat(con)
            except OSError as e:
                errors[con] = str(e)
                continue
            key = os.path.abspath(con).lower()
            stamp = [st.st_size, int(st.st_mtime), tool]
            hit = cache.get(key)
            if hit and hit.get('stamp') == stamp:
                result[con] = hit['songs']
                continue
            out = os.path.join(work, f'{i}.json')
            try:
                _run_nautilus(['list', con, out], out)
                # game_origin needs a Nautilus build from after it was added;
                # older builds leave it '' and songs fall back to 'customs'
                with open(out, encoding='utf-8') as f:
                    songs = [{'id': s['id'], 'name': s.get('name') or s['id'],
                              'artist': s.get('artist') or '',
                              'game_origin': s.get('game_origin') or ''}
                             for s in json.load(f) if s.get('id')]
            except Exception as e:
                if 'no songs.dta' not in str(e):
                    errors[con] = str(e)
                    continue
                songs = []   # a CON without songs (title update, DLC unlock...), cache it as empty
            result[con] = songs
            cache[key] = {'stamp': stamp, 'songs': songs}
            dirty = True
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # also when progress() raised to cancel: keep what was listed so far
        if dirty:
            _save_cache(cache)
    return result, errors


def _ogg_channels(data, start):
    """Channel count from the Vorbis identification header of the first Ogg page."""
    nseg = data[start + 26]
    packet = start + 27 + nseg
    if data[packet:packet + 7] != b'\x01vorbis':
        return None
    return data[packet + 11]


def mogg_to_ogg(mogg, ogg):
    """Strip the mogg header from a decrypted mogg. Returns the channel count."""
    with open(mogg, 'rb') as f:
        data = f.read()
    start = int.from_bytes(data[4:8], 'little') if len(data) > 8 else 0
    if data[start:start + 4] != b'OggS':
        start = data.find(b'OggS')
        if start < 0:
            raise Exception('mogg has no Ogg audio (still encrypted?)')
    with open(ogg, 'wb') as f:
        f.write(data[start:])
    return _ogg_channels(data, start)


def _dta_tree(text):
    """songs.dta parsed into nested lists ('quoted' and "string" atoms keep
    their text, numbers stay strings); ';' comments dropped."""
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|;[^\n]*|[()\[\]{}]|[^\s()\[\]{};"]+', text)
    root, stack = [], []
    cur = root
    for t in tokens:
        if t.startswith(';'):
            continue
        if t in '([{':
            stack.append(cur)
            cur.append([])
            cur = cur[-1]
        elif t in ')]}':
            if stack:
                cur = stack.pop()
        else:
            cur.append(t.strip('\'"') if t[:1] in '\'"' else t)
    return root


def _find_key(node, key):
    """The first (key ...) list anywhere under node, or None."""
    for item in node:
        if isinstance(item, list):
            if item and item[0] == key:
                return item
            hit = _find_key(item, key)
            if hit is not None:
                return hit
    return None


def _ints(node):
    out = []
    for x in node:
        if isinstance(x, list):
            out += _ints(x)
        elif re.fullmatch(r'\d+', x):
            out.append(int(x))
    return out


def crowd_channels_in(dta_text, song_id):
    """The song's crowd_channels in a songs.dta (mogg channel numbers), [] if none."""
    for entry in _dta_tree(dta_text):
        if isinstance(entry, list) and entry and isinstance(entry[0], str) \
                and entry[0].lower() == song_id.lower():
            hit = _find_key(entry, 'crowd_channels')
            return _ints(hit[1:]) if hit else []
    return []


_crowd_cache = {}


def crowd_channels(con, song_id):
    """The song's crowd channels (the audience cheering and singing along in
    the original Rock Band songs), read from the CON's songs.dta through
    Nautilus. They aren't in any video, so the sync and the edits leave them
    out. [] when the song has none or the DTA can't be read."""
    try:
        st = os.stat(con)
        key = (os.path.abspath(con).lower(), st.st_size, int(st.st_mtime))
    except OSError:
        return []
    text = _crowd_cache.get(key)
    if text is None:
        tmp = tempfile.mkdtemp(prefix='bh_dta_')
        try:
            out = os.path.join(tmp, 'songs.dta')
            _run_nautilus(['dta', con, out], out)
            with open(out, 'rb') as f:
                raw = f.read()
            text = raw.decode('utf-8', errors='replace') if b'utf8' in raw[:4096].lower() \
                or b'utf-8' in raw[:4096].lower() else raw.decode('latin-1')
        except Exception as e:
            print(f'  Crowd channels: could not read songs.dta ({e}), using every channel')
            text = ''
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _crowd_cache[key] = text
    try:
        return crowd_channels_in(text, song_id)
    except Exception:
        return []


def mix_reference(ogg, channels, out_wav, skip=()):
    """All tracks at -5 dB summed to mono, as float WAV so the sum can't clip.
    This is what the fingerprinter compares the YouTube audio against.
    skip: channels left out (the crowd)."""
    channels = channels or 2
    keep = [i for i in range(channels) if i not in set(skip)] or list(range(channels))
    terms = '+'.join(f'{TRACK_GAIN:.4f}*c{i}' for i in keep)
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', ogg,
         '-af', f'pan=mono|c0={terms}', '-c:a', 'pcm_f32le', out_wav],
        check=True, creationflags=NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare_song_audio(con, song_id, folder):
    """Decrypt the song's mogg via Nautilus and leave song.wav in folder,
    without the crowd channels (crowd_channels)."""
    mogg = os.path.join(folder, 'song.mogg')
    _run_nautilus(['extract', con, song_id, mogg], mogg)
    ogg = os.path.join(folder, 'song.ogg.tmp')
    try:
        channels = mogg_to_ogg(mogg, ogg)
        crowd = [c for c in crowd_channels(con, song_id) if c < (channels or 2)]
        if crowd:
            print(f'  Crowd channels {", ".join(map(str, crowd))} left out of the song audio')
        mix_reference(ogg, channels, os.path.join(folder, 'song.wav'), skip=crowd)
    finally:
        for p in (mogg, ogg):
            if os.path.exists(p):
                os.remove(p)


def video_format(max_height):
    """yt-dlp format for CON videos: video plus audio (kept in the WebM so the
    video can still be lined up by hand in an editor)."""
    return (f'bestvideo[height<={max_height}][vcodec^=avc]+bestaudio/'
            f'bestvideo[height<={max_height}]+bestaudio/'
            f'best[height<={max_height}]/best')


# Matroska tag written into every WebM this app makes. Re-exporting from an
# editor drops it, which is the point: a hand-fixed video no longer counts as ours.
TAG_KEY = 'BACKSTAGEHERO'


def read_tag(video):
    """The BackstageHero tag of a video ('' if it has none, None if unreadable)."""
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', video],
                           capture_output=True, text=True, timeout=15,
                           encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    except Exception:
        return None
    m = re.search(r'^\s*' + TAG_KEY + r'\s*:\s*(.+)$', r.stderr, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else ''


def is_ours(video):
    return bool(read_tag(video))


def probe_video(video):
    """(height like '720p' or '?', BackstageHero tag or '') of a video, with
    one ffmpeg run: the library scan reads both for every CON video.
    (None, None) if it couldn't be read."""
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', video],
                           capture_output=True, text=True, timeout=15,
                           encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    except Exception:
        return None, None
    # the Video: stream line, so embedded cover art doesn't count
    line = next((l for l in r.stderr.splitlines() if 'Video:' in l), '')
    m = re.search(r'(\d{3,4})x(\d{3,4})', line or r.stderr)
    res = f'{int(m.group(2))}p' if m else '?'
    t = re.search(r'^\s*' + TAG_KEY + r'\s*:\s*(.+)$', r.stderr, re.IGNORECASE | re.MULTILINE)
    return res, (t.group(1).strip() if t else '')


def video_info(video):
    """{'w', 'h', 'audio', 'duration'} of a video from one ffmpeg run:
    picture size (0 if unknown), whether it has an audio stream, length in s."""
    info = {'w': 0, 'h': 0, 'audio': False, 'duration': None}
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', video],
                           capture_output=True, text=True, timeout=15,
                           encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    except Exception:
        return info
    line = next((l for l in r.stderr.splitlines() if 'Video:' in l), '')
    m = re.search(r'(\d{2,5})x(\d{2,5})', line)
    if m:
        info['w'], info['h'] = int(m.group(1)), int(m.group(2))
        # SAR other than 1:1 (anamorphic): the displayed width differs
        d = re.search(r'DAR (\d+):(\d+)', line)
        if d and int(d.group(2)):
            info['w'] = round(info['h'] * int(d.group(1)) / int(d.group(2)))
    info['audio'] = any('Audio:' in l for l in r.stderr.splitlines())
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', r.stderr)
    if m:
        info['duration'] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return info


def listen_mix(con, song_id, video, out):
    """The 🎧 check: the exported video with the CON's song audio on the left
    channel and the video's own audio on the right, both normalized, so you
    hear whether they line up. The video stream is copied, so this only takes
    as long as decrypting the mogg and mixing the audio. Written to out (.mkv).
    Returns video_info(video).
    video=None (the song has no video yet): just the CON audio, in both ears."""
    info = video_info(video) if video else {'w': 0, 'h': 0, 'audio': False,
                                             'duration': None}
    tmp = tempfile.mkdtemp(prefix='bh_listen_')
    try:
        prepare_song_audio(con, song_id, tmp)
        ref = os.path.join(tmp, 'song.wav')
        mono = 'aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono,dynaudnorm'
        if not video:
            r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                '-i', ref, '-filter_complex',
                                f'[0:a]{mono},pan=stereo|FL=c0|FR=c0[a]',
                                '-map', '[a]', '-c:a', 'pcm_s16le', out],
                               capture_output=True, text=True, errors='replace',
                               creationflags=NO_WINDOW)
            if r.returncode != 0 or not os.path.exists(out):
                msg = r.stderr.strip().splitlines()
                raise Exception('Could not read the audio: ' + (msg[-1] if msg else
                                                               f'exit code {r.returncode}'))
            info['duration'] = _duration(ref)
            return info
        if info['audio']:
            graph = (f'[1:a]{mono},apad[l];[0:a]{mono},apad[r];'
                     '[l][r]amerge=inputs=2,aformat=channel_layouts=stereo[a]')
        else:      # no audio in the video: CON audio on the left, silence right
            graph = f'[1:a]{mono},pan=stereo|FL=c0[a]'
        length = max(info['duration'] or 0, _duration(ref) or 0) or None
        cmd = (['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-i', video, '-i', ref, '-filter_complex', graph,
                '-map', '0:v:0', '-map', '[a]', '-c:v', 'copy', '-c:a', 'pcm_s16le']
               + (['-t', f'{length:.3f}'] if length else []) + [out])
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                           creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(out):
            msg = r.stderr.strip().splitlines()
            raise Exception('Could not mix the audio: ' + (msg[-1] if msg else
                                                          f'exit code {r.returncode}'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info


LEVEL_HOP = 0.025      # seconds per level reading


def listen_levels(path, hop=LEVEL_HOP, rate=16000):
    """Level meter data for a listen_mix file: an (n, 2) float array, one row
    per `hop` seconds, RMS in dBFS of the left (CON) and right (video)
    channels, floored at -90. None if it can't be read."""
    import numpy as np
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', path,
                            '-map', '0:a:0', '-ac', '2', '-ar', str(rate),
                            '-f', 's16le', '-'],
                           capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        return None
    pcm = np.frombuffer(r.stdout, dtype='<i2')
    if r.returncode != 0 or pcm.size < 4:
        return None
    step = max(1, int(rate * hop))
    frames = pcm[:pcm.size - pcm.size % 2].reshape(-1, 2).astype(np.float32) / 32768.0
    n = frames.shape[0] // step
    if not n:
        return None
    blocks = frames[:n * step].reshape(n, step, 2)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    return np.maximum(20 * np.log10(np.maximum(rms, 1e-9)), -90.0)


WATERMARK_TEXT = 'BackstageHero'
WATERMARK_SECONDS = 3


def _watermark_filter():
    """'BackstageHero' in the bottom-right corner for the first 3 seconds,
    semi-transparent, scaled to the video height."""
    font = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'arialbd.ttf')
    if not os.path.exists(font):
        font = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'arial.ttf')
    font_opt = ''
    if os.path.exists(font):
        # ffmpeg filter syntax: forward slashes, escape the drive colon
        font_opt = "fontfile='" + font.replace('\\', '/').replace(':', '\\:') + "':"
    return (f"drawtext={font_opt}text='{WATERMARK_TEXT}':"
            f"fontsize=h/22:fontcolor=white@0.7:borderw=2:bordercolor=black@0.5:"
            f"x=w-tw-h/30:y=h-th-h/30:enable='lt(t,{WATERMARK_SECONDS})'")


def _duration(path):
    """Media duration in seconds from ffmpeg's banner, or None."""
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', path], capture_output=True,
                           text=True, timeout=15, errors='replace', creationflags=NO_WINDOW)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', r.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


_running_procs = set()      # ffmpeg renders in progress, for kill_renders()
_preview_procs = set()      # Preview renders: not stopped by cancel_render()
_cancel = threading.Event()  # cancel_render(): the running render stops


class RenderCancelled(Exception):
    """The running render was cancelled from the export queue."""


def cancel_render():
    """Stop the running render (✕ in the export queue). It raises
    RenderCancelled, its .part file is removed and no later ffmpeg pass of
    it starts (WebM pass 2). The render thread calls clear_cancel() before
    the next one."""
    _cancel.set()
    for proc in list(_running_procs):
        try:
            proc.kill()
        except Exception:
            pass


def clear_cancel():
    _cancel.clear()


def kill_renders():
    """Stop every running render (closing the app mid-export). The render
    raises, and its .part file is removed."""
    for proc in list(_running_procs | _preview_procs):
        try:
            proc.kill()
        except Exception:
            pass


def _run_ffmpeg(cmd, total_seconds, on_progress, export=True):
    """Run ffmpeg, calling on_progress(0..1) from its -progress output.
    export=False (Preview): the export queue's cancel doesn't touch it."""
    procs = _running_procs if export else _preview_procs
    if export and _cancel.is_set():
        raise RenderCancelled()
    proc = subprocess.Popen(cmd[:1] + ['-progress', 'pipe:1', '-nostats'] + cmd[1:],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors='replace', creationflags=NO_WINDOW)
    procs.add(proc)
    try:
        for line in proc.stdout:
            if on_progress and total_seconds and line.startswith('out_time_us='):
                try:
                    on_progress(int(line.split('=', 1)[1]) / 1e6 / total_seconds)
                except ValueError:
                    pass   # 'N/A' before the first frame
        err = proc.stderr.read()
        proc.wait()
    finally:
        procs.discard(proc)
    if export and _cancel.is_set():
        raise RenderCancelled()
    if proc.returncode != 0:
        msg = err.strip().splitlines()
        raise Exception('Encode failed: ' + (msg[-1] if msg else f'exit code {proc.returncode}'))


def encode_synced_webm(src, offset_ms, dest, work, tag='', progress=None, crop=''):
    """Cut the video so it starts with the song, as a YARG-style WebM.

    offset_ms uses the video_start_time convention: -3000 means the song starts
    3 s into the video, so 3 s are cut off the front. A positive value means the
    video starts after the song, so that much black (and silence) is added in front.
    2-pass VP8 at constant quality, keyframe every 128 frames, Opus 128 kb/s.
    `tag` goes into the BACKSTAGEHERO metadata tag."""
    pre, filters = _cut_filters(offset_ms, crop)
    common =(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + pre
              + ['-i', src] + filters
              + ['-c:v', 'libvpx', '-crf', VP8_CRF, '-b:v', VP8_MAX_BITRATE,
                 '-g', KEYFRAME_DISTANCE, '-auto-alt-ref', '1', '-lag-in-frames', '16',
                 '-deadline', 'good', '-cpu-used', '1',
                 '-passlogfile:v', os.path.join(work, 'vp8pass')])
    tmp = dest + '.part.webm'
    # output length, for the progress bar: source minus the cut, plus padding
    total = _duration(src)
    if total:
        total = max(1.0, total + offset_ms / 1000)
    p1 = (lambda f: progress(0.4 * f)) if progress else None
    p2 = (lambda f: progress(0.4 + 0.6 * f)) if progress else None
    try:
        _run_ffmpeg(common + ['-an', '-pass:v', '1', '-f', 'webm', os.devnull], total, p1)
        _run_ffmpeg(common + ['-c:a', 'libopus', '-b:a', '128k',
                              '-metadata', f'{TAG_KEY}={tag or "1"}',
                              '-pass:v', '2', '-f', 'webm', tmp], total, p2)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _song_key(con, song_id):
    return f'{os.path.abspath(con).lower()}|{song_id}'


def work_dir(con, song_id):
    """Per-song folder in the app's data dir (never inside the user's library).
    Holds the raw download and its state.json until the video is rendered."""
    tag = hashlib.sha1(_song_key(con, song_id).encode()).hexdigest()[:12]
    return os.path.join(updater.data_dir(), 'con_work',
                        f'{re.sub(r"[^A-Za-z0-9_-]", "_", song_id)}_{tag}')


def work_folder(con, song_id):
    """Fresh, empty work_dir for a new download."""
    path = work_dir(con, song_id)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def discard_work(con, song_id):
    shutil.rmtree(work_dir(con, song_id), ignore_errors=True)


# ---- downloads waiting for review ------------------------------------------
# After downloading, a CON song waits for the user: play it, look at the
# collage, search again, or render. state.json in its work_dir remembers which
# video it is and the measured offset, so this survives a restart.

def save_review(con, song_id, video_id, offset_ms, matched, title, autoedit=None,
                mix=None, base_plan=None):
    """autoedit: an autoedit.plan() result (with 'log') when the video needs
    cuts/inserts/speed changes; render() then rebuilds it instead of offsetting.
    Auto-mix: mix lists the videos the plan's segments use ('src' indexes it):
    [{'video_id', 'file' (in the work dir), 'type', 'title'}], the first is
    video.mp4, the base. base_plan is the base video's own plan (None when an
    offset is enough), which is what gets shared with the database."""
    state = {'video_id': video_id, 'offset': offset_ms, 'matched': matched, 'title': title}
    if autoedit:
        import autoedit as ae
        state['autoedit'] = autoedit
        state['plan_v'] = ae.PLAN_VERSION
    if mix:
        state['mix'] = mix
        state['base_plan'] = base_plan
    with open(os.path.join(work_dir(con, song_id), 'state.json'), 'w', encoding='utf-8') as f:
        json.dump(state, f)


def load_review(con, song_id):
    """The pending review state (dict with 'raw' = downloaded video path), or None."""
    d = work_dir(con, song_id)
    raw = os.path.join(d, 'video.mp4')
    try:
        with open(os.path.join(d, 'state.json'), encoding='utf-8') as f:
            state = json.load(f)
    except Exception:
        return None
    if not os.path.exists(raw):
        return None
    state['raw'] = raw
    return state


# ---- good / bad YouTube IDs per song -----------------------------------------
# Rendering marks the video as good; "search again" marks it bad so the next
# search skips it. Kept in the app's data dir, keyed by CON path + song id.

_IDS_FILE = os.path.join(updater.data_dir(), 'con_videos.json')


def _load_ids():
    try:
        with open(_IDS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ids(data):
    tmp = _IDS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, _IDS_FILE)


def video_ids(con, song_id):
    """{'good': id or None, 'bad': [ids]} for one song."""
    e = _load_ids().get(_song_key(con, song_id)) or {}
    return {'good': e.get('good'), 'bad': list(e.get('bad') or [])}


def mark_good(con, song_id, video_id):
    data = _load_ids()
    e = data.setdefault(_song_key(con, song_id), {})
    e['good'] = video_id
    e['bad'] = [b for b in e.get('bad', []) if b != video_id]
    _save_ids(data)


def mark_bad(con, song_id, video_id):
    if not video_id:
        return
    data = _load_ids()
    e = data.setdefault(_song_key(con, song_id), {})
    if e.get('good') == video_id:
        e['good'] = None
    if video_id not in e.setdefault('bad', []):
        e['bad'].append(video_id)
    _save_ids(data)


def unmark_bad(con, song_id, video_id):
    """Take a video off the song's 'wrong video' list (chosen again on purpose)."""
    data = _load_ids()
    e = data.get(_song_key(con, song_id))
    if e and video_id in e.get('bad', []):
        e['bad'] = [b for b in e['bad'] if b != video_id]
        _save_ids(data)


# ---- rendering -----------------------------------------------------------------

def existing_video(con, song_id):
    """The song's current background video next to the CON (.webm or .mp4), or None."""
    for ext in ('webm', 'mp4'):
        p = video_path(con, song_id, ext)
        if os.path.exists(p):
            return p
    return None


def render(con, song_id, fmt, version, progress=None, keep_other=False):
    """Cut + encode the reviewed download next to the CON as <song id>.<fmt>
    ('webm' or 'mp4') and mark its YouTube ID good. The raw download stays in
    the cache (work_dir) so it can be exported again, e.g. a quick MP4 to check
    the auto-edit, then the WebM; clear_cache() removes it.
    A previous video of the other format is removed so YARG has just one,
    unless keep_other (the user chose to keep both)."""
    state = load_review(con, song_id)
    if not state:
        raise Exception('no downloaded video to render')
    dest = video_path(con, song_id, fmt)
    work = work_dir(con, song_id)
    crop = bars_crop(state, work)
    mix = state.get('mix')
    if state.get('autoedit') and not mix:
        _replan_if_old(con, song_id, state, work)
    plan = state.get('autoedit')
    if plan and mix:
        files = [os.path.join(work, m['file']) for m in mix]
        crops = [crop] + [mix_crop(state, work, m['file']) for m in mix[1:]]
        tag = (f'v{version} source={state["video_id"]} automix='
               f'{"+".join(m["video_id"] for m in mix[1:])} synced=yes')
        encode_autoedit(files, plan, dest, fmt, work, tag, progress, crops)
    elif plan:
        tag = (f'v{version} source={state["video_id"]} autoedit='
               f'{len(plan.get("log") or [])}edits synced=yes')
        encode_autoedit(state['raw'], plan, dest, fmt, work, tag, progress, crop)
    else:
        tag = (f'v{version} source={state["video_id"]} offset={state["offset"]} '
               f'synced={"yes" if state.get("matched") else "no"}')
        if fmt == 'mp4':
            encode_synced_mp4(state['raw'], state['offset'], dest, tag, progress, crop)
        else:
            encode_synced_webm(state['raw'], state['offset'], dest, work, tag, progress, crop)
    record_contribution(con, song_id, state)
    other = video_path(con, song_id, 'mp4' if fmt == 'webm' else 'webm')
    if os.path.exists(other) and not keep_other:
        os.remove(other)
    mark_good(con, song_id, state['video_id'])
    # the edit this export was made with (None: a plain offset), so a re-check
    # knows whether the video next to the CON is outdated (export_outdated)
    _update_state(work, rendered=fmt,
                  rendered_segments=plan['segments'] if plan else None)
    return dest


PREVIEW_HEIGHT = 480


def preview_path(con, song_id):
    return os.path.join(work_dir(con, song_id), 'preview.mkv')


def render_preview(con, song_id, progress=None):
    """A quick look at the edit before exporting: the same cut as render(),
    at 480p with x264 ultrafast and no watermark, then the CON audio on the
    left ear and the video's own audio on the right (listen_mix), so the
    sync can be judged by eye and ear. Written to the work dir (preview.mkv),
    never next to the CON: it isn't an export. Returns its path."""
    import autoedit
    state = load_review(con, song_id)
    if not state:
        raise Exception('no downloaded video to preview')
    work = work_dir(con, song_id)
    mix = state.get('mix')
    if state.get('autoedit') and not mix:
        _replan_if_old(con, song_id, state, work)
    crop = bars_crop(state, work)
    plan = state.get('autoedit')
    scale = f'scale=-2:{PREVIEW_HEIGHT}'
    fast = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k']
    cut = os.path.join(work, 'preview_cut.mp4')
    if plan:
        src, size = state['raw'], None
        if mix:
            src = [os.path.join(work, m['file']) for m in mix]
            crop = [crop] + [mix_crop(state, work, m['file']) for m in mix[1:]]
            w, h = autoedit.frame_size(src[0], crop[0])
            size = (max(2, round(w * PREVIEW_HEIGHT / h / 2) * 2), PREVIEW_HEIGHT)
        cmd = autoedit.build_cmd(src, plan['segments'], keep_audio=True, crop=crop,
                                 post='' if size else scale, size=size)
        total = max(1.0, plan['segments'][-1]['ref_end'])
    else:
        offset = state.get('offset') or 0
        pre, vf, af = [], [crop] if crop else [], []
        if offset < 0:
            pre = ['-ss', f'{-offset / 1000:.3f}']
        elif offset > 0:
            vf.append(f'tpad=start_duration={offset / 1000:.3f}:color=black')
            af = ['-af', f'adelay={offset}:all=1']
        vf.append(scale)
        cmd = (['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + pre
               + ['-i', state['raw'], '-vf', ','.join(vf)] + af)
        total = max(1.0, (_duration(state['raw']) or 1.0) + offset / 1000)
    out = preview_path(con, song_id)
    try:
        _run_ffmpeg(cmd + fast + [cut], total,
                    (lambda f: progress(0.9 * f)) if progress else None, export=False)
        listen_mix(con, song_id, cut, out)
    finally:
        if os.path.exists(cut):
            os.remove(cut)
    if progress:
        progress(1.0)
    return out


def editor_segments(state, song_len):
    """The edit an export would make, as autoedit segments: the auto-edit
    plan, or for a plain offset one piece (black first when the video starts
    later, the video's start cut when it starts earlier)."""
    plan = state.get('autoedit')
    if plan and plan.get('segments'):
        return plan['segments']
    off = (state.get('offset') or 0) / 1000
    ref0, vid0 = max(0.0, off), max(0.0, -off)
    vlen = _duration(state['raw']) or song_len or 1.0
    end = ref0 + max(0.1, vlen - vid0)
    if song_len:
        end = min(end, song_len)
    end = max(end, ref0 + 0.1)
    return [{'ref_start': round(ref0, 3), 'ref_end': round(end, 3), 'vid_start': round(vid0, 3),
             'vid_end': round(vid0 + end - ref0, 3), 'speed': 1.0}]


_EDITOR_VCODECS = ('h264', 'hevc')      # what every editor reads from an MP4
_EDITOR_ACODECS = ('aac',)              # Premiere can't read Opus / Vorbis


def _codecs(path):
    """{'video': codec, 'audio': codec} of a file's first streams ('' if none)."""
    out = {'video': '', 'audio': ''}
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'stream=codec_type,codec_name', '-of', 'csv=p=0', path],
                           capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        for line in r.stdout.splitlines():
            name, _, kind = line.strip().partition(',')
            if kind in out and not out[kind]:
                out[kind] = name
    except Exception:
        pass
    return out


def _editor_ok(c):
    return c['video'] in _EDITOR_VCODECS and (not c['audio'] or c['audio'] in _EDITOR_ACODECS)


def _editor_copy(src, dest):
    """The download for an editor: yt-dlp's Opus audio becomes AAC (Premiere
    can't import Opus) and a VP9 / AV1 video becomes H.264; streams that are
    fine are copied as they are, so timing doesn't change. A copy already
    there from an earlier export is kept when it is newer and editor-ready."""
    if (os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(src)
            and _editor_ok(_codecs(dest))):
        return
    c = _codecs(src)
    if _editor_ok(c):
        shutil.copyfile(src, dest)
        return
    vcodec = (['-c:v', 'copy'] if c['video'] in _EDITOR_VCODECS else
              ['-c:v', 'libx264', '-preset', 'fast', '-crf', '16', '-pix_fmt', 'yuv420p'])
    acodec = ['-c:a', 'aac', '-b:a', '256k'] if c['audio'] else ['-an']
    tmp = dest + '.tmp.mp4'
    print(f'Editor copy: {os.path.basename(src)} ({c["video"]}/{c["audio"] or "no audio"}) '
          f'-> {"H.264" if vcodec[1] != "copy" else c["video"]}/AAC')
    try:
        r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                            '-map', '0:v:0', '-map', '0:a:0?'] + vcodec + acodec
                           + ['-movflags', '+faststart', tmp],
                           capture_output=True, text=True, errors='replace',
                           creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise Exception('ffmpeg could not convert the video for the editor: '
                            + (r.stderr.strip().splitlines() or ['?'])[-1])
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _audio_layout(path):
    """(codec, channels, sample rate) of a file's first audio stream."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                            'stream=codec_name,channels,sample_rate', '-of', 'default=nw=1', path],
                           capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        d = dict(l.split('=', 1) for l in r.stdout.split() if '=' in l)
        return d['codec_name'], int(d['channels']), int(d['sample_rate'])
    except Exception:
        return None


def _editor_song_audio(con, song_id, dest):
    """The CON audio for an editor: 16-bit 48 kHz stereo WAV (song.wav from
    prepare_song_audio is the fingerprinter's mono float mix, which Premiere
    doesn't load as the timeline expects), peak at -1 dB so nothing clips."""
    tmp = tempfile.mkdtemp(prefix='bh_editor_')
    try:
        prepare_song_audio(con, song_id, tmp)
        src = os.path.join(tmp, 'song.wav')
        r = subprocess.run(['ffmpeg', '-hide_banner', '-i', src, '-af', 'volumedetect',
                            '-f', 'null', os.devnull], capture_output=True, text=True,
                           errors='replace', creationflags=NO_WINDOW)
        m = re.search(r'max_volume:\s*(-?[\d.]+) dB', r.stderr)
        gain = -1.0 - float(m.group(1)) if m else 0.0
        r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                            '-af', f'volume={gain:.2f}dB', '-ac', '2', '-ar', '48000',
                            '-c:a', 'pcm_s16le', dest], capture_output=True, text=True,
                           errors='replace', creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise Exception('ffmpeg could not write song.wav: '
                            + (r.stderr.strip().splitlines() or ['?'])[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def export_to_editor(con, song_id, fmt, folder, name, progress=None):
    """Hand the song to a video editor for a manual FMV: folder gets a copy of
    the kept download (and an auto-mix's other videos), the CON audio
    (song.wav, the timeline's audio track) and the edit as a timeline in fmt
    (an autoedit.EDITOR_FORMATS key), plus HOW TO.txt. The edit starts from
    what an export would render. Returns the timeline's path."""
    import autoedit
    state = load_review(con, song_id)
    if not state:
        raise Exception('no downloaded video to export')
    work = work_dir(con, song_id)
    mix = state.get('mix')
    if state.get('autoedit') and not mix:
        _replan_if_old(con, song_id, state, work)
    label, ext, how = autoedit.EDITOR_FORMATS[fmt]
    os.makedirs(folder, exist_ok=True)
    step = (lambda f: progress(f)) if progress else (lambda f: None)
    files = [m['file'] for m in mix] if mix else ['video.mp4']
    videos = []
    for k, f in enumerate(files):
        dest = os.path.join(folder, f)
        src = os.path.join(work, f)
        _editor_copy(src, dest)
        videos.append(dest)
        step(0.5 * (k + 1) / len(files))
    audio = os.path.join(folder, 'song.wav')
    _editor_song_audio(con, song_id, audio)     # always fresh (an old one may have the crowd)
    step(0.9)
    segs = editor_segments(state, _duration(audio))
    safe = re.sub(r'[<>:"/\\|?*]+', '_', name).strip(' .') or song_id
    timeline = autoedit.export_timeline(fmt, videos, segs, os.path.join(folder, safe + ext),
                                        audio=audio, name=name)
    crops = [bars_crop(state, work)]
    if mix:
        crops += [mix_crop(state, work, m['file']) for m in mix[1:]]
    lines = [name, '', f'Open in {label}: {how}', '',
             'The timeline starts from the edit BackstageHero would export:',
             '  audio track: song.wav, the CON audio (the chart timing)',
             '  video track: ' + ', '.join(files)
             + (' (their own audio on A3/A4, linked, clips disabled: enable them to'
                ' sync by ear)' if fmt in ('premiere', 'resolve') else
                ' (their own audio left off)'), '']
    plan = state.get('autoedit')
    if plan and plan.get('log'):
        lines += ['Auto-edit log:'] + ['  ' + e['text'] for e in plan['log']] + ['']
    elif not plan:
        lines += [f'One piece, offset {state.get("offset") or 0} ms.', '']
    if any(crops):
        lines += ['Black bars BackstageHero would crop (ffmpeg crop=w:h:x:y):']
        lines += [f'  {f}: {c or "none"}' for f, c in zip(files, crops)] + ['']
    lines += ['Frame holds are 1x clips from the held frame: freeze them if you like.', '',
              'When done, export the video (YARG: WebM VP8 + Opus, or MP4) next to the CON as',
              f'  {video_path(con, song_id, "webm")}',
              'A video exported from an editor counts as not made by BackstageHero',
              '(the app asks twice before replacing it).']
    with open(os.path.join(folder, 'HOW TO.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    step(1.0)
    return timeline


def _same_plan(a, b):
    """True when two plans cut the video the same way (to 0.1 s / 0.2 % speed)."""
    sa, sb = a.get('segments') or [], b.get('segments') or []
    if len(sa) != len(sb):
        return False
    for x, y in zip(sa, sb):
        if any(abs(x[k] - y[k]) > 0.1 for k in ('ref_start', 'ref_end', 'vid_start', 'vid_end')):
            return False
        if abs(x.get('speed', 1) - y.get('speed', 1)) > 0.002 or bool(x.get('hold')) != bool(y.get('hold')):
            return False
    return True


def replan(con, song_id, state=None):
    """Re-run the auto-edit of a kept download whose plan was made by an older
    version of its rules (autoedit.PLAN_VERSION): the same version gives the
    same plan. Updates state (and state.json). Returns (result, text):
      'current'  the plan is up to date      'changed'  new plan saved
      'same'     re-planned, same edit       'kept'     no confident match /
      'skip'     no auto-edit plan, or an               failed: old plan kept
                 auto-mix (not re-planned)"""
    import autoedit
    state = state if state is not None else load_review(con, song_id)
    if not state or not state.get('autoedit'):
        return 'skip', 'no auto-edit (plain offset)'
    if state.get('mix'):
        return 'skip', 'auto-mix (not re-planned)'
    if state.get('plan_v', 1) >= autoedit.PLAN_VERSION:
        return 'current', 'auto-edit up to date'
    work = work_dir(con, song_id)
    print('Auto-edit: plan made by an older version, re-planning')
    tmp = tempfile.mkdtemp(prefix='bh_replan_')
    try:
        prepare_song_audio(con, song_id, tmp)
        plan = autoedit.plan(os.path.join(tmp, 'song.wav'), state['raw'])
        if not plan['ok']:
            print('  Auto-edit: no confident match now, keeping the saved plan')
            return 'kept', 'no confident match now, old edit kept'
        plan['log'] = autoedit.edit_log(plan['segments'], _duration(state['raw']))
        print('  Auto-edit: ' + plan['info'])
        for e in plan['log']:
            print('    ' + e['text'])
        same = _same_plan(state['autoedit'], plan)
        extra = {}
        if state.get('rendered') and 'rendered_segments' not in state:
            # exported before exports recorded their edit: it used this plan
            extra['rendered_segments'] = state['rendered_segments'] = \
                state['autoedit']['segments']
        state['autoedit'], state['plan_v'] = plan, autoedit.PLAN_VERSION
        _update_state(work, autoedit=plan, plan_v=autoedit.PLAN_VERSION, **extra)
        return ('same', 'same edit') if same else ('changed', plan['info'])
    except Exception as e:
        print('  Auto-edit: re-plan failed (' + str(e) + '), keeping the saved plan')
        return 'kept', f're-plan failed ({e})'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def export_outdated(state):
    """True when the song was exported with a different edit than its saved
    plan now has (a re-check changed it). Unknown (an export older than this
    record that was never re-planned) is False."""
    if not state or not state.get('rendered') or 'rendered_segments' not in state:
        return False
    done = state.get('rendered_segments')
    plan = state.get('autoedit')
    if not done or not plan:
        return bool(done) != bool(plan)
    return not _same_plan({'segments': done}, plan)


def _replan_if_old(con, song_id, state, work):
    """Before an export: re-plan an outdated auto-edit (see replan). On any
    failure the saved plan is used."""
    try:
        replan(con, song_id, state)
    except Exception as e:
        print('  Auto-edit: re-plan failed (' + str(e) + '), keeping the saved plan')


def _update_state(work, **values):
    """Add keys to a work dir's state.json."""
    path = os.path.join(work, 'state.json')
    try:
        with open(path, encoding='utf-8') as f:
            saved = json.load(f)
        saved.update(values)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(saved, f)
    except Exception as e:
        print('Could not update ' + path + ': ' + str(e))


# ---- cache: kept original downloads ------------------------------------------

def _cache_root():
    return os.path.join(updater.data_dir(), 'con_work')


def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cache_size(con=None, song_id=None):
    """Bytes kept for one song, or for every song when con is None."""
    return _dir_size(work_dir(con, song_id) if con else _cache_root())


def clear_cache(con, song_id):
    """Delete one song's kept download (and its review, if not rendered)."""
    discard_work(con, song_id)


def clear_all_cache():
    shutil.rmtree(_cache_root(), ignore_errors=True)


def encode_autoedit(src, plan, dest, fmt, work, tag='', progress=None, crop=''):
    """Render an auto-edit plan with the same codecs, watermark and tag as the
    offset renders. The video's own audio is cut the same way and kept.
    Auto-mix: src and crop are lists (one per video, the base first); every
    video is scaled into the base's (cropped) frame."""
    import autoedit
    segs = plan['segments']
    total = max(1.0, segs[-1]['ref_end'])
    size = None
    if isinstance(src, (list, tuple)):
        size = autoedit.frame_size(src[0], crop[0] if crop else '')
    base = autoedit.build_cmd(src, segs, keep_audio=True, crop=crop,
                              post=_watermark_filter(), size=size)
    tmp = dest + ('.part.mp4' if fmt == 'mp4' else '.part.webm')
    try:
        if fmt == 'mp4':
            _run_ffmpeg(base + ['-c:v', 'libx264', '-preset', 'medium', '-crf', '20',
                                '-g', KEYFRAME_DISTANCE, '-c:a', 'aac', '-b:a', '192k',
                                '-metadata', f'{TAG_KEY}={tag or "1"}',
                                '-movflags', '+faststart+use_metadata_tags', tmp],
                        total, progress)
        else:
            vp8 = ['-c:v', 'libvpx', '-crf', VP8_CRF, '-b:v', VP8_MAX_BITRATE,
                   '-g', KEYFRAME_DISTANCE, '-auto-alt-ref', '1', '-lag-in-frames', '16',
                   '-deadline', 'good', '-cpu-used', '1',
                   '-passlogfile:v', os.path.join(work, 'vp8pass')]
            p1 = (lambda f: progress(0.4 * f)) if progress else None
            p2 = (lambda f: progress(0.4 + 0.6 * f)) if progress else None
            _run_ffmpeg(base + vp8 + ['-an', '-pass:v', '1', '-f', 'webm', os.devnull],
                        total, p1)
            _run_ffmpeg(base + vp8 + ['-c:a', 'libopus', '-b:a', '128k',
                                      '-metadata', f'{TAG_KEY}={tag or "1"}',
                                      '-pass:v', '2', '-f', 'webm', tmp], total, p2)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---- contributions for the music video database (mvdb) ----------------------
# Every render is remembered here; the GUI's "Export missed entries" compares
# them against mvdb/ and writes the ones it doesn't have to missed_entries.json.

_CONTRIB_FILE = os.path.join(updater.data_dir(), 'contributions.json')


def load_contributions():
    try:
        with open(_CONTRIB_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def record_contribution(con, song_id, state, artist='', title='', source=''):
    """Remember a rendered video so it can be submitted to the database."""
    try:
        import mvdb
        items = [i for i in load_contributions()
                 if not (i['song'].get('con_id') == song_id
                         and mvdb.vid(i['video']) == state.get('video_id'))]
        meta = _song_meta(con, song_id)
        song = {'con_id': song_id, 'artist': artist or meta.get('artist', ''),
                'title': title or meta.get('name', '')}
        # a mix shares the base video with its own plan (the clips put in
        # are database links already)
        plan = state.get('base_plan') if state.get('mix') else state.get('autoedit')
        video = mvdb.make_video(state.get('video_id', ''), 'official',
                                title=state.get('title', ''),
                                offset_ms=0 if plan else state.get('offset'),
                                autoedit=plan, pack=os.path.basename(con))
        source = source or meta.get('game_origin') or mvdb.DEFAULT_SOURCE
        items.append({'source': mvdb.source_key(source), 'song': song, 'video': video})
        tmp = _CONTRIB_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, _CONTRIB_FILE)
    except Exception as e:
        print('Could not record contribution: ' + str(e))


def _song_meta(con, song_id):
    """name/artist of one CON song from the list cache (no Nautilus run)."""
    hit = _load_cache().get(os.path.abspath(con).lower()) or {}
    for s in hit.get('songs', []):
        if s.get('id') == song_id:
            return s
    return {}


# bump when aspectcrop measures differently, so cached crops are redone
# (2: the last seconds, end screens and credits, aren't measured)
CROP_VERSION = 2


def bars_crop(state, work):
    """ffmpeg crop filter that removes the video's black bars (letterbox or
    pillarbox, see aspectcrop), measured on the raw download once and kept in
    state.json. '' when there's nothing to crop or it can't be measured.
    A crop measured by an older aspectcrop (crop_v) is measured again."""
    if 'crop' in state and state.get('crop_v') == CROP_VERSION:
        return state['crop']
    crop = ''
    try:
        import aspectcrop
        r = aspectcrop.analyze(state['raw'])
        crop = aspectcrop.crop_filter(r.get('crop'))
        print('Black bars: ' + r.get('info', ''))
    except Exception as e:
        print('Could not check for black bars: ' + str(e))
        return ''
    state['crop'], state['crop_v'] = crop, CROP_VERSION
    _update_state(work, crop=crop, crop_v=CROP_VERSION)
    return crop


def mix_crop(state, work, name):
    """bars_crop for one of an auto-mix's other videos (a file in the work
    dir), kept in state.json's mix_crops."""
    crops = state.get('mix_crops') or {}
    hit = crops.get(name)
    if hit and hit.get('v') == CROP_VERSION:
        return hit['crop']
    crop = ''
    try:
        import aspectcrop
        r = aspectcrop.analyze(os.path.join(work, name))
        crop = aspectcrop.crop_filter(r.get('crop'))
        print(f'Black bars ({name}): ' + r.get('info', ''))
    except Exception as e:
        print('Could not check for black bars: ' + str(e))
        return ''
    crops[name] = {'crop': crop, 'v': CROP_VERSION}
    state['mix_crops'] = crops
    _update_state(work, mix_crops=crops)
    return crop


def _cut_filters(offset_ms, crop=''):
    """(input args, ffmpeg filter args) for cutting/padding plus the watermark.
    crop (aspectcrop) goes first, so the watermark lands on the picture."""
    pre, vf, af = [], [crop] if crop else [], []
    if offset_ms < 0:
        pre = ['-ss', f'{-offset_ms / 1000:.3f}']
    elif offset_ms > 0:
        vf.append(f'tpad=start_duration={offset_ms / 1000:.3f}:color=black')
        af = ['-af', f'adelay={offset_ms}:all=1']
    vf.append(_watermark_filter())
    return pre, ['-vf', ','.join(vf)] + af


def encode_synced_mp4(src, offset_ms, dest, tag='', progress=None, crop=''):
    """Same cut as the WebM, as H.264/AAC MP4: one pass, much faster."""
    pre, filters = _cut_filters(offset_ms, crop)
    total = _duration(src)
    if total:
        total = max(1.0, total + offset_ms / 1000)
    tmp = dest + '.part.mp4'
    try:
        _run_ffmpeg(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + pre
                    + ['-i', src] + filters
                    + ['-c:v', 'libx264', '-preset', 'medium', '-crf', '20',
                       '-pix_fmt', 'yuv420p', '-g', KEYFRAME_DISTANCE,
                       '-c:a', 'aac', '-b:a', '192k',
                       '-metadata', f'{TAG_KEY}={tag or "1"}',
                       '-movflags', '+faststart+use_metadata_tags', tmp],
                    total, progress)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---- collage (port of video_collage.py, frames grabbed with ffmpeg) --------------

COLLAGE_COLS = 4
COLLAGE_ROWS = 6


def make_collage(video, out_dir, cols=COLLAGE_COLS, rows=COLLAGE_ROWS):
    """Grid of evenly spaced frames, skipping the very first and last (same
    spacing as video_collage.py). Returns (collage_path, [(frame_path, secs)])."""
    from PIL import Image
    dur = _duration(video)
    if not dur:
        raise Exception('could not read the video length')
    os.makedirs(out_dir, exist_ok=True)
    n = cols * rows
    times = [dur * (k + 1) / (n + 1) for k in range(n)]
    frames = []
    for k, t in enumerate(times, 1):
        path = os.path.join(out_dir, f'frame_{k:02d}.jpg')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{t:.3f}', '-i', video,
                        '-frames:v', '1', '-q:v', '3', path],
                       creationflags=NO_WINDOW, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        if os.path.exists(path):
            frames.append((path, t))
    if not frames:
        raise Exception('could not grab any frames')
    first = Image.open(frames[0][0])
    w, h = first.size
    collage = Image.new('RGB', (w * cols, h * rows))
    for i, (path, _) in enumerate(frames):
        collage.paste(Image.open(path), ((i % cols) * w, (i // cols) * h))
    out = os.path.join(out_dir, 'collage.jpg')
    collage.save(out, quality=88)
    return out, frames
