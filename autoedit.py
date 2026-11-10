"""
Auto-edit a music video so it follows the album audio.

audiosync.py finds one offset, which is enough when the video is the album cut
with a lead-in. Official videos often aren't: a long intro, a slightly different
tempo (old songs especially, transferred from tape at a different speed), an
interlude/skit in the middle, or one chorus/verse fewer than the album.

This module maps every moment of the reference (album/chart) audio to a moment
of the video, then rebuilds the video from those pieces so it plays in step with
the reference from start to end:

  long intro / interlude  -> video jumps forward over the extra part
  different tempo         -> each piece is sped up / slowed down slightly
  missing chorus/verse    -> video jumps back and reuses the closest-sounding
                             part (usually another chorus)

How: chroma + band-energy features for both tracks (~10 frames/s), a cosine
cost matrix, then a Viterbi path where each reference frame advances the video
by 0, 1 or 2 frames (tempo) or jumps anywhere for a fixed penalty (edits). The
path is split at jumps and fitted with straight lines, giving a short list of
(ref_start, ref_end, video_start, video_end) segments that ffmpeg renders.

Needs numpy and ffmpeg.

CLI: see AUTOEDIT.md, or python autoedit.py --help
"""

import json
import os
import subprocess
import sys

import numpy as np

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# Tunables

SR = 11025
N_FFT = 2048
HOP = 1103                 # ~0.1 s per frame
FPS = SR / HOP
MAX_SECONDS = 900          # never analyze more than 15 min

N_BANDS = 16               # log band energies next to the 12 chroma bins
CHROMA_WEIGHT = 1.0
BAND_WEIGHT = 0.6

STEP_PENALTY = 0.08        # cost of a hold (0) or double (2) step vs a normal one
JUMP_PENALTY = 4.0         # cost of a cut; roughly "4 s of mediocre match"

MIN_SEGMENT = 1.5          # seconds; shorter pieces are merged into neighbours
LINE_TOLERANCE = 0.35      # seconds of path deviation before a piece is split
MAX_SPEED_DEV = 0.25       # pieces faster/slower than this are treated as cuts
BAD_MATCH = 0.6            # path cost / typical cost above this -> wrong video


# Audio and features

def _decode(path, seconds=MAX_SECONDS):
    cmd = ['ffmpeg', '-v', 'quiet', '-i', path, '-vn', '-t', str(seconds),
           '-ac', '1', '-ar', str(SR), '-f', 's16le', '-']
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0 or len(proc.stdout) < N_FFT * 4:
        raise RuntimeError(f'could not decode audio from {path}')
    return np.frombuffer(proc.stdout, dtype='<i2').astype(np.float32) / 32768.0


def _features(x):
    """(frames, 12 + N_BANDS) unit-norm feature rows."""
    from numpy.lib.stride_tricks import sliding_window_view
    x = np.pad(x, (N_FFT // 2, N_FFT // 2))
    frames = sliding_window_view(x, N_FFT)[::HOP] * np.hanning(N_FFT).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)

    # chroma: fold 55 Hz..4 kHz into 12 pitch classes
    sel = (freqs >= 55) & (freqs <= 4000)
    pc = (np.round(12 * np.log2(freqs[sel] / 440.0)) % 12).astype(int)
    chroma = np.zeros((mag.shape[0], 12), np.float32)
    for k in range(12):
        chroma[:, k] = (mag[:, sel][:, pc == k] ** 2).sum(axis=1)
    chroma = np.log1p(100 * chroma / (chroma.max(axis=1, keepdims=True) + 1e-9))

    # log band energies (timbre / loudness shape), log-spaced 40 Hz..5 kHz
    edges = np.geomspace(40, 5000, N_BANDS + 1)
    bands = np.stack([mag[:, (freqs >= lo) & (freqs < hi)].sum(axis=1)
                      for lo, hi in zip(edges[:-1], edges[1:])], axis=1)
    bands = np.log1p(bands)
    bands -= bands.mean(axis=1, keepdims=True)   # loudness/master independent

    def smooth(a, n=5):   # ~0.5 s smoothing, like CENS
        k = np.ones(n, np.float32) / n
        return np.apply_along_axis(lambda c: np.convolve(c, k, 'same'), 0, a)

    chroma = smooth(chroma)
    bands = smooth(bands)
    chroma -= chroma.mean(axis=1, keepdims=True)
    feat = np.hstack([CHROMA_WEIGHT * _unit(chroma), BAND_WEIGHT * _unit(bands)])
    energy = (frames ** 2).mean(axis=1)
    return _unit(feat), energy


def _unit(a):
    return a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)


# Alignment

def _viterbi(cost):
    """Best path through cost (N ref frames x M video frames).

    Returns vid[i] for each ref frame i and a bool array marking jumps.
    """
    n, m = cost.shape
    INF = np.float32(1e9)
    back = np.zeros((n, m), np.int8)          # 0 hold, 1 step, 2 double, 3 jump
    jump_from = np.zeros(n, np.int32)
    d = cost[0].copy()                        # start anywhere: intros are free
    for i in range(1, n):
        hold = d + STEP_PENALTY
        step = np.concatenate(([INF], d[:-1]))
        dbl = np.concatenate(([INF, INF], d[:-2])) + STEP_PENALTY
        jsrc = int(d.argmin())
        jump = d[jsrc] + JUMP_PENALTY
        stack = np.stack([hold, step, dbl, np.full(m, jump, np.float32)])
        choice = stack.argmin(axis=0)
        back[i] = choice
        jump_from[i] = jsrc
        d = stack[choice, np.arange(m)] + cost[i]
    j = int(d.argmin())                       # end anywhere: outros are free
    path = np.zeros(n, np.int64)
    jumps = np.zeros(n, bool)
    for i in range(n - 1, -1, -1):
        path[i] = j
        if i == 0:
            break
        c = int(back[i, j])
        if c == 3:
            jumps[i] = True
            j = jump_from[i]
        else:
            j -= int(c)
    return path, jumps


def _fit_pieces(path, jumps):
    """Split the path at jumps and bends; fit a line to each piece."""
    runs, start = [], 0
    for i in range(1, len(path)):
        if jumps[i]:
            runs.append((start, i))
            start = i
    runs.append((start, len(path)))

    tol = LINE_TOLERANCE * FPS
    pieces = []

    def split(a, b):
        xs = np.arange(a, b)
        ys = path[a:b].astype(float)
        if b - a < 3:
            pieces.append((a, b))
            return
        slope, icpt = np.polyfit(xs, ys, 1)
        err = np.abs(ys - (slope * xs + icpt))
        k = int(err.argmax())
        if err[k] > tol and 2 < k < (b - a) - 2:
            split(a, a + k)
            split(a + k, b)
        else:
            pieces.append((a, b))

    for a, b in runs:
        split(a, b)
    return pieces


def _segments(path, pieces):
    segs = []
    for a, b in pieces:
        xs = np.arange(a, b)
        if b - a >= 3:
            slope, icpt = np.polyfit(xs, path[a:b].astype(float), 1)
        else:
            slope, icpt = 1.0, float(path[a]) - a
        if not (1 - MAX_SPEED_DEV <= slope <= 1 + MAX_SPEED_DEV):
            slope = 1.0
            icpt = float(np.median(path[a:b] - xs))
        segs.append({'ref_start': a / FPS, 'ref_end': b / FPS,
                     'vid_start': (slope * a + icpt) / FPS,
                     'vid_end': (slope * b + icpt) / FPS})

    # merge tiny pieces into the previous one (or the next, at the start)
    out = []
    for s in segs:
        if out and s['ref_end'] - s['ref_start'] < MIN_SEGMENT:
            p = out[-1]
            p['vid_end'] += s['ref_end'] - p['ref_end']
            p['ref_end'] = s['ref_end']
        else:
            out.append(dict(s))
    if len(out) > 1 and out[0]['ref_end'] - out[0]['ref_start'] < MIN_SEGMENT:
        first = out.pop(0)
        out[0]['vid_start'] -= out[0]['ref_start'] - first['ref_start']
        out[0]['ref_start'] = first['ref_start']

    # merge neighbours that are really one continuous piece
    merged = [out[0]]
    for s in out[1:]:
        p = merged[-1]
        if abs(s['vid_start'] - p['vid_end']) < 0.25:
            sp_p = (p['vid_end'] - p['vid_start']) / (p['ref_end'] - p['ref_start'])
            sp_s = (s['vid_end'] - s['vid_start']) / (s['ref_end'] - s['ref_start'])
            if abs(sp_p - sp_s) < 0.03:
                p['ref_end'], p['vid_end'] = s['ref_end'], s['vid_end']
                continue
        merged.append(s)

    for s in merged:
        s['vid_start'] = max(0.0, s['vid_start'])
        s['speed'] = round((s['vid_end'] - s['vid_start']) /
                           max(1e-6, s['ref_end'] - s['ref_start']), 4)
        for k in ('ref_start', 'ref_end', 'vid_start', 'vid_end'):
            s[k] = round(s[k], 3)
    return merged


def plan(reference_audio, video_audio):
    """Work out the edit. Returns a dict with 'segments', 'cost', 'ok', 'info'."""
    ref_f, ref_e = _features(_decode(reference_audio))
    vid_f, _ = _features(_decode(video_audio))
    cost = (1.0 - ref_f @ vid_f.T).astype(np.float32) * 0.5    # 0..1

    # silent reference frames (album intro/outro silence) match anything
    quiet = ref_e < ref_e.max() * 1e-4
    cost[quiet] = 0.2

    path, jumps = _viterbi(cost)
    segs = _segments(path, _fit_pieces(path, jumps))
    for sg in segs:
        a, b = int(sg['ref_start'] * FPS), max(int(sg['ref_start'] * FPS) + 1, int(sg['ref_end'] * FPS))
        sg['match'] = round(float(cost[np.arange(a, b), path[a:b]].mean()), 3)
    mean_cost = float(cost[np.arange(len(path)), path][~quiet].mean())
    # relative to a random pairing, so it doesn't depend on the absolute scale:
    # a real match is far below the matrix median, a wrong song sits near it
    rel = mean_cost / max(1e-6, float(np.median(cost[~quiet][:, ::7])))
    cuts = len(segs) - 1
    speeds = [s['speed'] for s in segs]
    info = (f'{len(segs)} segment(s), {cuts} cut(s), speed '
            f'{min(speeds):.3f}-{max(speeds):.3f}, match {rel:.2f} of random (lower is better)')
    return {'segments': segs, 'cost': round(mean_cost, 3),
            'ok': rel < BAD_MATCH, 'relative_cost': round(rel, 3), 'info': info,
            'duration': round(len(path) / FPS, 3)}


# Rendering

def build_cmd(video, segments, audio=None, keep_audio=False, fps=30, crop='', post=''):
    """ffmpeg command (without codec/output args) that cuts/stretches `video`
    per `segments`. Output streams: [v], plus [a] when audio or keep_audio.

    audio: a file to use as the output audio (e.g. the reference itself).
    keep_audio: cut and re-time the video's own audio the same way instead,
        handy to check the sync by ear or fix it later in an editor.
    crop: ffmpeg crop filter (see aspectcrop.crop_filter), applied per piece.
    post: extra video filters after the pieces are joined (e.g. a watermark).
    """
    parts, vlabels, alabels, inputs = [], [], [], []
    for k, s in enumerate(segments):
        out_len = s['ref_end'] - s['ref_start']
        src_len = max(1e-3, s['vid_end'] - s['vid_start'])
        factor = out_len / src_len
        # each piece gets its own seeked input: one shared input with several
        # trims makes ffmpeg buffer every frame for the later pieces (OOM)
        inputs += ['-ss', f"{s['vid_start']:.3f}", '-t', f"{src_len:.3f}", '-i', video]
        pre = crop + ',' if crop else ''
        parts.append(f"[{k}:v]{pre}setpts=(PTS-STARTPTS)*{factor:.6f},fps={fps},"
                     f"tpad=stop_mode=clone:stop_duration={out_len:.3f},"
                     f"trim=duration={out_len:.3f},setpts=PTS-STARTPTS[v{k}]")
        vlabels.append(f'[v{k}]')
        if keep_audio and not audio:
            tempo = min(2.0, max(0.5, 1.0 / factor))
            parts.append(f"[{k}:a]asetpts=PTS-STARTPTS,atempo={tempo:.6f},"
                         f"apad,atrim=duration={out_len:.3f},asetpts=PTS-STARTPTS[a{k}]")
            alabels.append(f'[a{k}]')
    # reference may start before the first matched frame (e.g. ref has silence)
    lead = segments[0]['ref_start']
    graph = ';'.join(parts) + ';' + ''.join(vlabels) + f'concat=n={len(vlabels)}:v=1:a=0'
    if lead > 0.01:
        graph += f',tpad=start_mode=clone:start_duration={lead:.3f}'
    if post:
        graph += ',' + post
    graph += ',format=yuv420p[v]'
    if alabels:
        graph += ';' + ''.join(alabels) + f'concat=n={len(alabels)}:v=0:a=1'
        if lead > 0.01:
            graph += f',adelay={int(lead * 1000)}:all=1'
        graph += '[a]'

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + inputs
    if audio:
        cmd += ['-i', audio]
    cmd += ['-filter_complex', graph, '-map', '[v]']
    if audio:
        cmd += ['-map', f'{len(segments)}:a', '-shortest']
    elif alabels:
        cmd += ['-map', '[a]']
    else:
        cmd += ['-an']
    return cmd


def render(video, segments, dest, audio=None, fps=30, extra=None, crop='',
           keep_audio=False, post=''):
    """Cut/stretch `video` per `segments` into `dest` (see build_cmd).

    extra: ffmpeg codec args, default libx264 crf 20 + AAC.
    The result already starts with the song, so its offset is 0.
    """
    cmd = build_cmd(video, segments, audio=audio, keep_audio=keep_audio, fps=fps,
                    crop=crop, post=post)
    has_audio = bool(audio) or keep_audio
    cmd += extra or (['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20']
                     + (['-c:a', 'aac', '-b:a', '192k'] if has_audio else []))
    cmd += [dest]
    subprocess.run(cmd, check=True, creationflags=_NO_WINDOW)
    return dest


def needs_edit(plan_result):
    """True when the plan is more than a single offset (cuts, inserts or a
    speed change), i.e. when a plain video_start_time offset can't sync it."""
    segs = plan_result.get('segments') or []
    return len(segs) > 1 or any(abs(s.get('speed', 1) - 1) > SPEED_EPS for s in segs)


def offset_ms(plan_result):
    """video_start_time for a single-segment plan (same sign as audiosync)."""
    s = plan_result['segments'][0]
    return -int(round((s['vid_start'] - s['ref_start']) * 1000))


def autoedit(reference_audio, video, dest, video_audio=None, mux_audio=False, **kw):
    """Plan and render in one go. video_audio defaults to the video's own track."""
    p = plan(reference_audio, video_audio or video)
    if p['ok']:
        render(video, p['segments'], dest, audio=reference_audio if mux_audio else None, **kw)
    return p


# Edit log
#
# Wording follows auto-editor: a "cut" removes a source range, "speed" changes
# a range's playback rate (--set-speed SPEED,START,STOP), and the "timeline" is
# the output. auto-editor never repeats footage, so a reused fragment is logged
# as "inserted" (an add-in of an earlier source range at a timeline position).

GAP = 0.05          # seconds; source jumps smaller than this are not edits
SPEED_EPS = 0.002   # speed changes smaller than 0.2 % are not logged


def ts(sec):
    """Seconds -> M:SS.mmm (H:MM:SS.mmm past an hour)."""
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f'{int(h)}:{int(m):02d}:{s:06.3f}' if h else f'{int(m)}:{s:06.3f}'


def edit_log(segments, video_duration=None):
    """Turn segments into a list of edit entries plus readable lines.

    Each entry: {'action': 'cut'|'insert'|'speed', ...} with times in seconds:
      cut     source [start, end] removed (never reaches the timeline)
      insert  source [start, end] played again at timeline time `at`
      speed   source [start, end] played at `speed` (1.0 = unchanged),
              `percent` is the change, e.g. -0.48
    """
    entries = []
    if not segments:
        return entries
    # inserts: a piece that starts inside source already played
    played = []                       # (start, end) source ranges shown so far
    for k, sg in enumerate(segments):
        if k and any(sg['vid_start'] < e - GAP and sg['vid_end'] > st + GAP
                     for st, e in played):
            end = min(sg['vid_end'], max(e for st, e in played if st < sg['vid_end']))
            entries.append({'action': 'insert', 'at': sg['ref_start'],
                            'start': sg['vid_start'], 'end': end})
        played.append((sg['vid_start'], sg['vid_end']))
    # cuts: source never shown at all (gaps in the union of played ranges)
    played.sort()
    cursor = 0.0
    for st, e in played:
        if st - cursor > GAP:
            entries.append({'action': 'cut', 'start': cursor, 'end': st,
                            'note': 'intro' if cursor == 0.0 else ''})
        cursor = max(cursor, e)
    if video_duration and video_duration - cursor > GAP:
        entries.append({'action': 'cut', 'start': cursor, 'end': video_duration,
                        'note': 'outro'})
    for sg in segments:
        sp = sg.get('speed', 1.0)
        if abs(sp - 1.0) > SPEED_EPS:
            entries.append({'action': 'speed', 'start': sg['vid_start'],
                            'end': sg['vid_end'], 'speed': sp,
                            'percent': round((sp - 1.0) * 100, 2)})
    order = {'cut': 0, 'insert': 1, 'speed': 2}
    entries.sort(key=lambda e: (e['start'], order[e['action']]))
    for e in entries:
        for k in ('start', 'end', 'at'):
            if k in e:
                e[k] = round(e[k], 3)
        e['text'] = describe(e)
    return entries


def describe(e):
    span = f"{ts(e['start'])}-{ts(e['end'])}"
    if e['action'] == 'cut':
        what = f" ({e['note']})" if e.get('note') else ''
        return f"segment {span} was cut{what}"
    if e['action'] == 'insert':
        return f"at {ts(e['at'])}, the fragment {span} was inserted"
    return f"segment {span} change speed by {e['percent']:+.2f}%"


def auto_editor_args(entries):
    """The cuts and speeds as auto-editor options (it has no insert/repeat)."""
    cuts = [f"{e['start']}s,{e['end']}s" for e in entries if e['action'] == 'cut']
    speeds = [f"{e['speed']},{e['start']}s,{e['end']}s" for e in entries
              if e['action'] == 'speed']
    args = []
    if cuts:
        args += ['--cut'] + cuts
    if speeds:
        args += ['--set-speed'] + speeds
    return args


# Timeline exports (to fine-tune the edit in an NLE)

def _probe_video(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                          '-show_entries', 'stream=width,height,r_frame_rate:format=duration',
                          '-of', 'default=nw=1', path], capture_output=True, text=True,
                         creationflags=_NO_WINDOW).stdout
    d = dict(l.split('=', 1) for l in out.split() if '=' in l)
    num, den = (d.get('r_frame_rate', '30/1').split('/') + ['1'])[:2]
    return {'width': int(d.get('width', 1920)), 'height': int(d.get('height', 1080)),
            'fps': round(int(num) / max(1, int(den))) or 30,
            'duration': float(d.get('duration', 0) or 0)}


def export_v3(video, segments, dest, audio=None, fps=None):
    """auto-editor v3 timeline. Render it or convert it with auto-editor:
        auto-editor timeline.v3 -o out.mp4
        auto-editor timeline.v3 --export premiere -o timeline.xml
    """
    info = _probe_video(video)
    fps = fps or info['fps']
    v_clips, a_clips = [], []
    for sg in segments:
        start = round(sg['ref_start'] * fps)
        dur = max(1, round(sg['ref_end'] * fps) - start)
        clip = {'src': os.path.abspath(video), 'start': start, 'dur': dur,
                # offset is in speed-scaled frames, like auto-editor writes it
                'offset': round(sg['vid_start'] * fps / max(1e-6, sg['speed'])),
                'stream': 0}
        if abs(sg['speed'] - 1.0) > SPEED_EPS:
            clip['effects'] = [f"speed:{sg['speed']:.4f}"]
        v_clips.append(clip)
    if audio:
        total = round(segments[-1]['ref_end'] * fps)
        a_clips = [{'src': os.path.abspath(audio), 'start': 0, 'dur': total,
                    'offset': 0, 'stream': 0}]
    tl = {'version': '3', 'timebase': f'{fps}/1', 'background': '#000',
          'resolution': [info['width'], info['height']], 'samplerate': 48000,
          'layout': 'stereo', 'langs': ['und'] + (['und'] if audio else []),
          'c': [], 'v': [v_clips], 'a': [a_clips] if a_clips else [], 'sidecar': []}
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(tl, f, indent=2)
    return dest


def _file_url(path):
    from urllib.parse import quote
    p = os.path.abspath(path).replace('\\', '/')
    if not p.startswith('/'):
        p = '/' + p              # C:/x -> /C:/x
    return 'file://localhost' + quote(p, safe='/:')


def export_xml(video, segments, dest, audio=None, fps=None, name='BackstageHero autoedit'):
    """Final Cut Pro 7 XML (xmeml v5), the format auto-editor's --export premiere
    writes; Premiere Pro, DaVinci Resolve and Kdenlive import it. Speed changes
    are Time Remap filters, the same way auto-editor writes them."""
    from xml.sax.saxutils import escape as esc
    info = _probe_video(video)
    fps = fps or info['fps']
    rate = f'<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>'
    total = round(segments[-1]['ref_end'] * fps)
    vname = esc(os.path.basename(video))
    vfile = (f'<file id="file-1"><name>{vname}</name><pathurl>{esc(_file_url(video))}</pathurl>'
             f'{rate}<duration>{round(info["duration"] * fps)}</duration>'
             f'<media><video><samplecharacteristics>{rate}<width>{info["width"]}</width>'
             f'<height>{info["height"]}</height><pixelaspectratio>square</pixelaspectratio>'
             f'</samplecharacteristics></video></media></file>')
    clips = []
    for k, sg in enumerate(segments, 1):
        start = round(sg['ref_start'] * fps)
        end = max(start + 1, round(sg['ref_end'] * fps))
        sp = sg['speed']
        # <in>/<out> count speed-scaled source frames (as auto-editor's fcp7 export)
        cin = round(sg['vid_start'] * fps / sp)
        cout = cin + (end - start)
        filt = ''
        if abs(sp - 1.0) > SPEED_EPS:
            filt = ('<filter><effect><name>Time Remap</name><effectid>timeremap</effectid>'
                    '<parameter authoringApp="PremierePro"><parameterid>variablespeed</parameterid>'
                    '<name>variablespeed</name><valuemin>0</valuemin><valuemax>1</valuemax>'
                    '<value>0</value></parameter>'
                    '<parameter authoringApp="PremierePro"><parameterid>speed</parameterid>'
                    f'<name>speed</name><valuemin>-100000</valuemin><valuemax>100000</valuemax>'
                    f'<value>{sp * 100:.4f}</value></parameter>'
                    '<parameter authoringApp="PremierePro"><parameterid>frameblending</parameterid>'
                    '<name>frameblending</name><value>FALSE</value></parameter>'
                    '</effect></filter>')
        f = vfile if k == 1 else '<file id="file-1"/>'
        clips.append(f'<clipitem id="clipitem-{k}"><name>{vname}</name><enabled>TRUE</enabled>'
                     f'<start>{start}</start><end>{end}</end><in>{cin}</in><out>{cout}</out>'
                     f'{f}{filt}</clipitem>')
    atrack = ''
    if audio:
        aname = esc(os.path.basename(audio))
        atrack = (f'<audio><track><clipitem id="clipitem-a1"><name>{aname}</name>'
                  f'<enabled>TRUE</enabled><start>0</start><end>{total}</end><in>0</in>'
                  f'<out>{total}</out><file id="file-2"><name>{aname}</name>'
                  f'<pathurl>{esc(_file_url(audio))}</pathurl>{rate}'
                  f'<media><audio><samplecharacteristics><depth>16</depth>'
                  f'<samplerate>48000</samplerate></samplecharacteristics>'
                  f'<channelcount>2</channelcount></audio></media></file>'
                  f'<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex>'
                  f'</sourcetrack></clipitem></track></audio>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5">'
           f'<sequence><name>{esc(name)}</name><duration>{total}</duration>{rate}'
           f'<media><video><format><samplecharacteristics>{rate}<width>{info["width"]}</width>'
           f'<height>{info["height"]}</height><pixelaspectratio>square</pixelaspectratio>'
           f'</samplecharacteristics></format><track>{"".join(clips)}</track></video>'
           f'{atrack}</media></sequence></xmeml>\n')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(xml)
    return dest


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='autoedit.py', description=(
        'Auto-edit a music video so it follows the album/chart audio.'))
    ap.add_argument('reference', help='album or chart audio (mp3/ogg/wav/...)')
    ap.add_argument('video', help='music video (with its own audio track)')
    ap.add_argument('output', nargs='?', help='rendered video (omit with --plan)')
    ap.add_argument('--video-audio', help='use this file as the video audio (when the video file has none)')
    ap.add_argument('--audio', action='store_true', help='mux the reference audio into the output')
    ap.add_argument('--keep-audio', action='store_true', help="keep the video's own audio, cut the same way")
    ap.add_argument('--crop', action='store_true', help='crop black bars to a standard aspect ratio')
    ap.add_argument('--coverage', type=float, default=1.0, help='share of frames the crop must keep whole (default 1.0)')
    ap.add_argument('--plan', action='store_true', help='only print the plan and log, do not render')
    ap.add_argument('--json', help='write the plan + edit log to this JSON file')
    ap.add_argument('--v3', help='write an auto-editor v3 timeline')
    ap.add_argument('--xml', help='write a Final Cut Pro 7 / Premiere XML timeline')
    a = ap.parse_args(argv)
    if not a.output and not (a.plan or a.json or a.v3 or a.xml):
        ap.error('output is required unless --plan, --json, --v3 or --xml is given')

    p = plan(a.reference, a.video_audio or a.video)
    print(p['info'])
    for s in p['segments']:
        print(f"  album {ts(s['ref_start'])}-{ts(s['ref_end'])}  <-  "
              f"video {ts(s['vid_start'])}-{ts(s['vid_end'])}  x{s['speed']}  cost {s['match']}")
    vdur = _probe_video(a.video)['duration']
    p['log'] = edit_log(p['segments'], vdur)
    print('edit log:')
    for e in p['log']:
        print('  ' + e['text'])
    ae = auto_editor_args(p['log'])
    if ae:
        print('auto-editor equivalent (cuts/speeds only):', ' '.join(ae))
    if not p['ok']:
        print('match too weak, not rendering (wrong video?)')
        sys.exit(2)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(p, f, indent=1)
        print('wrote', a.json)
    if a.v3:
        print('wrote', export_v3(a.video, p['segments'], a.v3, audio=a.reference))
    if a.xml:
        print('wrote', export_xml(a.video, p['segments'], a.xml, audio=a.reference))
    if a.plan or not a.output:
        return p
    crop = ''
    if a.crop:
        import aspectcrop
        c = aspectcrop.analyze(a.video, coverage=a.coverage)
        print(c['info'])
        crop = aspectcrop.crop_filter(c.get('crop'))
        p['crop'] = crop
    render(a.video, p['segments'], a.output, audio=a.reference if a.audio else None,
           keep_audio=a.keep_audio, crop=crop)
    print('wrote', a.output)
    return p


if __name__ == '__main__':
    main()
