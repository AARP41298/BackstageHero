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


def find_con_files(root):
    """CON/LIVE files under root. They have no extension, so only extension-less
    files are opened to check the magic; song audio/charts are never touched."""
    found = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1]:
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


# Song lists are cached by file path + size + mtime, so rescanning a library of
# multi-GB packs doesn't re-run Nautilus for CONs that haven't changed.
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
    """{con_path: [{'id','name','artist'}...]} for each CON, plus {con: error} for
    the ones that couldn't be read. progress(done, total) is called as it goes."""
    cache = _load_cache()
    result, errors = {}, {}
    dirty = False
    work = tempfile.mkdtemp(prefix='bh_list_')
    try:
        for i, con in enumerate(cons):
            if progress and i % 10 == 0:
                progress(i, len(cons))
            try:
                st = os.stat(con)
            except OSError as e:
                errors[con] = str(e)
                continue
            key = os.path.abspath(con).lower()
            stamp = [st.st_size, int(st.st_mtime)]
            hit = cache.get(key)
            if hit and hit.get('stamp') == stamp:
                result[con] = hit['songs']
                continue
            out = os.path.join(work, f'{i}.json')
            try:
                _run_nautilus(['list', con, out], out)
                with open(out, encoding='utf-8') as f:
                    songs = [{'id': s['id'], 'name': s.get('name') or s['id'],
                              'artist': s.get('artist') or ''}
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


def mix_reference(ogg, channels, out_wav):
    """All tracks at -5 dB summed to mono, as float WAV so the sum can't clip.
    This is what the fingerprinter compares the YouTube audio against."""
    channels = channels or 2
    terms = '+'.join(f'{TRACK_GAIN:.4f}*c{i}' for i in range(channels))
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', ogg,
         '-af', f'pan=mono|c0={terms}', '-c:a', 'pcm_f32le', out_wav],
        check=True, creationflags=NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare_song_audio(con, song_id, folder):
    """Decrypt the song's mogg via Nautilus and leave song.wav in folder."""
    mogg = os.path.join(folder, 'song.mogg')
    _run_nautilus(['extract', con, song_id, mogg], mogg)
    ogg = os.path.join(folder, 'song.ogg.tmp')
    try:
        channels = mogg_to_ogg(mogg, ogg)
        mix_reference(ogg, channels, os.path.join(folder, 'song.wav'))
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


def _run_ffmpeg(cmd, total_seconds, on_progress):
    """Run ffmpeg, calling on_progress(0..1) from its -progress output."""
    proc = subprocess.Popen(cmd[:1] + ['-progress', 'pipe:1', '-nostats'] + cmd[1:],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors='replace', creationflags=NO_WINDOW)
    for line in proc.stdout:
        if on_progress and total_seconds and line.startswith('out_time_us='):
            try:
                on_progress(int(line.split('=', 1)[1]) / 1e6 / total_seconds)
            except ValueError:
                pass   # 'N/A' before the first frame
    err = proc.stderr.read()
    if proc.wait() != 0:
        msg = err.strip().splitlines()
        raise Exception('WebM encode failed: ' + (msg[-1] if msg else f'exit code {proc.returncode}'))


def encode_synced_webm(src, offset_ms, dest, work, tag='', progress=None):
    """Cut the video so it starts with the song, as a YARG-style WebM.

    offset_ms uses the video_start_time convention: -3000 means the song starts
    3 s into the video, so 3 s are cut off the front. A positive value means the
    video starts after the song, so that much black (and silence) is added in front.
    2-pass VP8 at constant quality, keyframe every 128 frames, Opus 128 kb/s.
    `tag` goes into the BACKSTAGEHERO metadata tag."""
    pre, filters = _cut_filters(offset_ms)
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

def save_review(con, song_id, video_id, offset_ms, matched, title):
    with open(os.path.join(work_dir(con, song_id), 'state.json'), 'w', encoding='utf-8') as f:
        json.dump({'video_id': video_id, 'offset': offset_ms, 'matched': matched,
                   'title': title}, f)


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


# ---- rendering -----------------------------------------------------------------

def existing_video(con, song_id):
    """The song's current background video next to the CON (.webm or .mp4), or None."""
    for ext in ('webm', 'mp4'):
        p = video_path(con, song_id, ext)
        if os.path.exists(p):
            return p
    return None


def render(con, song_id, fmt, version, progress=None):
    """Cut + encode the reviewed download next to the CON as <song id>.<fmt>
    ('webm' or 'mp4'), mark its YouTube ID good and drop the raw download.
    A previous video of the other format is removed so YARG has just one."""
    state = load_review(con, song_id)
    if not state:
        raise Exception('no downloaded video to render')
    dest = video_path(con, song_id, fmt)
    tag = (f'v{version} source={state["video_id"]} offset={state["offset"]} '
           f'synced={"yes" if state.get("matched") else "no"}')
    work = work_dir(con, song_id)
    if fmt == 'mp4':
        encode_synced_mp4(state['raw'], state['offset'], dest, tag, progress)
    else:
        encode_synced_webm(state['raw'], state['offset'], dest, work, tag, progress)
    other = video_path(con, song_id, 'mp4' if fmt == 'webm' else 'webm')
    if os.path.exists(other):
        os.remove(other)
    mark_good(con, song_id, state['video_id'])
    discard_work(con, song_id)
    return dest


def _cut_filters(offset_ms):
    """(input args, ffmpeg filter args) for cutting/padding plus the watermark."""
    pre, vf, af = [], [], []
    if offset_ms < 0:
        pre = ['-ss', f'{-offset_ms / 1000:.3f}']
    elif offset_ms > 0:
        vf.append(f'tpad=start_duration={offset_ms / 1000:.3f}:color=black')
        af = ['-af', f'adelay={offset_ms}:all=1']
    vf.append(_watermark_filter())
    return pre, ['-vf', ','.join(vf)] + af


def encode_synced_mp4(src, offset_ms, dest, tag='', progress=None):
    """Same cut as the WebM, as H.264/AAC MP4: one pass, much faster."""
    pre, filters = _cut_filters(offset_ms)
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
