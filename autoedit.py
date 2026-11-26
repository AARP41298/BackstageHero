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
ANCHOR_MIN = 8.0           # seconds; the first/last pieces this long are the song's body
HOLD_MAX = 3.0             # seconds; edge gaps up to this hold a frame instead of reusing

# Bump when plan() would give a different edit for the same audio: an export
# re-plans downloads whose saved plan is older (conpack.render). Plans saved
# without a version are 1.
#   2: the count-in's stick hits no longer count as music (_music)
#   3: a first line starting before the video starts later, not slower (_segments)
#   4: lines ignore the frames off them (count-in, outro) when fitting the speed
#   5: CON audio without the crowd channels (conpack.crowd_channels)
PLAN_VERSION = 5

# auto-mix (see mix())
MIX_GOOD = 0.6             # an overlay frame fits below this share of a random pairing's cost
MIX_EXTEND = 0.8           # once a fit is found, its line runs on while below this
MIX_MIN = 4.0              # seconds; shorter fits aren't put in
MIX_SMOOTH = 2.0           # seconds of smoothing of the overlay's match cost
MIX_MAX_SPEED_DEV = 0.08   # overlay pieces faster/slower than this don't fit
MIX_SHORT = 1.0            # seconds; base pieces left shorter than this get absorbed


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


def _robust_line(xs, ys):
    """Line through a piece of the path, ignoring the frames that are off it:
    the count-in (the path waits on the video's first frame), an outro past
    the video's end. A plain fit tilts toward them: Quevedo's constant -4.00 s
    came out as speed 0.9976, 0.25 s off at the start and drifting again from
    the middle. Refit on the frames within LINE_TOLERANCE of the line."""
    slope, icpt = np.polyfit(xs, ys, 1)
    tol = LINE_TOLERANCE * FPS
    for _ in range(4):
        keep = np.abs(ys - (slope * xs + icpt)) <= tol
        if keep.sum() < max(3, len(xs) // 2):
            break
        s2, i2 = np.polyfit(xs[keep], ys[keep], 1)
        done = abs(s2 - slope) < 1e-5 and abs(i2 - icpt) < 0.01
        slope, icpt = s2, i2
        if done:
            break
    return slope, icpt


def _segments(path, pieces):
    segs = []
    for a, b in pieces:
        xs = np.arange(a, b)
        if b - a >= 3:
            slope, icpt = _robust_line(xs, path[a:b].astype(float))
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

    # the first line may start before the video does (the chart's count-in is
    # longer than the video's pre-roll): start it later in the song, at the
    # same speed. Clamping vid_start to 0 instead stretched it into a slowdown
    # (Rap God: 3.2 s late became -0.87 % over the whole song, 3 s off at the start)
    f = merged[0]
    if f['vid_start'] < 0 and f['vid_end'] > 0:
        sp = (f['vid_end'] - f['vid_start']) / max(1e-6, f['ref_end'] - f['ref_start'])
        if sp > 0:
            f['ref_start'] = min(f['ref_end'], f['ref_start'] - f['vid_start'] / sp)
            f['vid_start'] = 0.0
    for s in merged:
        s['vid_start'] = max(0.0, s['vid_start'])
        s['speed'] = round((s['vid_end'] - s['vid_start']) /
                           max(1e-6, s['ref_end'] - s['ref_start']), 4)
        for k in ('ref_start', 'ref_end', 'vid_start', 'vid_end'):
            s[k] = round(s[k], 3)
    return merged


_REF_CACHE = {}


def _ref_features(path):
    """_features of the reference audio, kept while the file is unchanged:
    auto-mix matches several videos against the same chart audio."""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_mtime, st.st_size)
    except OSError:
        key = None
    if key and key in _REF_CACHE:
        return _REF_CACHE[key]
    out = _features(_decode(path))
    if key:
        _REF_CACHE.clear()
        _REF_CACHE[key] = out
    return out


def _analyze(reference_audio, video_audio):
    """(cost matrix ref x video 0..1, reference energy, quiet reference frames)."""
    ref_f, ref_e = _ref_features(reference_audio)
    vid_f, _ = _features(_decode(video_audio))
    cost = (1.0 - ref_f @ vid_f.T).astype(np.float32) * 0.5    # 0..1

    # silent reference frames (album intro/outro silence) match anything
    quiet = ref_e < ref_e.max() * 1e-4
    cost[quiet] = 0.2
    return cost, ref_e, quiet


def plan(reference_audio, video_audio):
    """Work out the edit. Returns a dict with 'segments', 'cost', 'ok', 'info'."""
    cost, ref_e, quiet = _analyze(reference_audio, video_audio)

    path, jumps = _viterbi(cost)
    segs = _segments(path, _fit_pieces(path, jumps))
    segs = _fix_edges(segs, cost, len(path) / FPS, cost.shape[1] / FPS, _music(ref_e))
    for sg in segs:
        sg['match'] = _seg_cost(sg, cost)
    mean_cost = float(cost[np.arange(len(path)), path][~quiet].mean())
    # relative to a random pairing, so it doesn't depend on the absolute scale:
    # a real match is far below the matrix median, a wrong song sits near it
    rel = mean_cost / max(1e-6, float(np.median(cost[~quiet][:, ::7])))
    cuts = len(segs) - 1
    speeds = [s['speed'] for s in segs if not s.get('hold')] or [1.0]
    info = (f'{len(segs)} segment(s), {cuts} cut(s), speed '
            f'{min(speeds):.3f}-{max(speeds):.3f}, match {rel:.2f} of random (lower is better)')
    return {'segments': segs, 'cost': round(mean_cost, 3),
            'ok': rel < BAD_MATCH, 'relative_cost': round(rel, 3), 'info': info,
            'duration': round(len(path) / FPS, 3)}


# Intro / outro
#
# The path treats the song's intro and outro like any other part, so it may
# jump anywhere for them (Almost Easy: the intro came from 1:05 although the
# video had footage right before the song starts; Blinding Lights: the outro
# reused footage although the video's own outro was long enough). Before the
# first long piece and after the last one:
#   1. continue that piece into the video's own pre-roll / outro, no jump
#   2. intro the video can't cover: its silent start (Rock Band / Guitar Hero
#      charts start with a count-in) stays black, like a positive offset; from
#      where the music starts, up to 3 s hold the first frame and longer parts
#      reuse footage from the video's first half (E-Pro used to take its ending)
#   3. outro the video can't cover: short gaps hold the last frame, longer
#      ones reuse footage from the video's second half

def _seg_cost(sg, cost):
    """Mean match cost along a segment's line (holds: their single frame)."""
    n, m = cost.shape
    a = int(sg['ref_start'] * FPS)
    b = max(a + 1, int(sg['ref_end'] * FPS))
    i = np.arange(a, min(b, n))
    if i.size == 0:
        return 0.0
    t = sg['vid_start'] + (i / FPS - sg['ref_start']) * (0 if sg.get('hold') else sg['speed'])
    j = np.clip(np.round(t * FPS).astype(int), 0, m - 1)
    return round(float(cost[i, j].mean()), 3)


def _match_region(cost, r0, r1, v0, v1):
    """Segments for album frames [r0, r1) matched only against video frames
    [v0, v1), in absolute seconds."""
    sub = cost[r0:r1, v0:v1]
    if sub.shape[0] < 2 or sub.shape[1] < 2:
        return []
    path, jumps = _viterbi(sub)
    segs = _segments(path, _fit_pieces(path, jumps))
    for sg in segs:
        sg['ref_start'] = round(sg['ref_start'] + r0 / FPS, 3)
        sg['ref_end'] = round(sg['ref_end'] + r0 / FPS, 3)
        sg['vid_start'] = round(sg['vid_start'] + v0 / FPS, 3)
        sg['vid_end'] = round(sg['vid_end'] + v0 / FPS, 3)
    return segs


MUSIC_FLOOR = 0.008  # share of the song's median energy; see _music
MUSIC_WINDOW = 1.0   # seconds


def _music(ref_e):
    """Frames where the chart audio has music. Loudness alone can't tell: a
    count-in's hits peak at up to 0.13 of the median (The Real Slim Shady) and
    a quiet guitar intro sits at 0.03 (Wanted Dead or Alive). But between the
    hits a count-in drops to silence (<= 0.002), while music keeps sounding:
    music is where the quietest fifth of the next second stays above MUSIC_FLOOR."""
    w = max(1, int(round(MUSIC_WINDOW * FPS)))
    e = np.concatenate([ref_e, np.zeros(w - 1, ref_e.dtype)])
    floor = np.percentile(np.lib.stride_tricks.sliding_window_view(e, w), 20, axis=1)
    lvl = max(float(np.median(ref_e)) * MUSIC_FLOOR, float(ref_e.max()) * 1e-4)
    return floor > lvl


def _hold(ref_start, ref_end, at):
    return {'ref_start': round(ref_start, 3), 'ref_end': round(ref_end, 3),
            'vid_start': round(at, 3), 'vid_end': round(at, 3), 'speed': 0.0, 'hold': True}


def _fix_edges(segs, cost, ref_dur, vid_dur, loud=None):
    long_ = [k for k, sg in enumerate(segs) if sg['ref_end'] - sg['ref_start'] >= ANCHOR_MIN]
    if not long_:
        return segs
    # a long piece only counts as the body if it's in order with the next/previous
    # long one: an intro taken from the end of the video (E-Pro) isn't
    while len(long_) > 1 and segs[long_[1]]['vid_start'] < segs[long_[0]]['vid_start'] - 1:
        long_.pop(0)
    while len(long_) > 1 and segs[long_[-1]]['vid_end'] < segs[long_[-2]]['vid_end'] - 1:
        long_.pop()
    first, last = segs[long_[0]], segs[long_[-1]]
    body = [dict(sg) for sg in segs[long_[0]:long_[-1] + 1]]
    half = vid_dur / 2

    # intro: run the first piece backwards into the video's pre-roll
    lead = []
    if first['ref_start'] > 0.05:
        sp = first['speed']
        back = first['ref_start'] * sp                      # video seconds wanted
        if first['vid_start'] - back >= -0.05:
            body[0]['vid_start'] = round(max(0.0, first['vid_start'] - back), 3)
            body[0]['ref_start'] = 0.0
        else:
            have = first['vid_start'] / sp                  # album seconds it covers
            gap_end = first['ref_start'] - have
            body[0]['vid_start'] = 0.0
            body[0]['ref_start'] = round(gap_end, 3)
            # silent start (count-in): black. From where the music starts, fill.
            g1 = int(gap_end * FPS)
            if loud is not None and g1 > 0 and loud[:g1].any():
                music = int(np.argmax(loud[:g1]))
                m0 = music / FPS
            elif loud is not None:
                music, m0 = g1, gap_end                     # all count-in: black
            else:
                music, m0 = 0, 0.0
            if gap_end - m0 >= 0.2:
                if gap_end - m0 <= HOLD_MAX:
                    lead = [_hold(m0, gap_end, 0.0)]
                else:
                    lead = _match_region(cost, music, g1, 0, int(half * FPS)) \
                        or [_hold(m0, gap_end, 0.0)]
                    lead[0]['ref_start'] = round(m0, 3)
                    lead[-1]['ref_end'] = round(gap_end, 3)

    # outro: run the last piece forwards into the video's own ending
    tail = []
    if ref_dur - last['ref_end'] > 0.05:
        sp = last['speed']
        fwd = (ref_dur - last['ref_end']) * sp
        if last['vid_end'] + fwd <= vid_dur + 0.05:
            body[-1]['vid_end'] = round(min(vid_dur, last['vid_end'] + fwd), 3)
            body[-1]['ref_end'] = round(ref_dur, 3)
        else:
            have = (vid_dur - last['vid_end']) / sp
            gap_start = last['ref_end'] + have
            body[-1]['vid_end'] = round(vid_dur, 3)
            body[-1]['ref_end'] = round(gap_start, 3)
            if ref_dur - gap_start <= HOLD_MAX:
                tail = [_hold(gap_start, ref_dur, max(0.0, vid_dur - 0.05))]
            else:
                tail = _match_region(cost, int(gap_start * FPS), cost.shape[0],
                                     int(half * FPS), cost.shape[1]) \
                    or [_hold(gap_start, ref_dur, max(0.0, vid_dur - 0.05))]
                tail[0]['ref_start'] = round(gap_start, 3)
    out = lead + body + tail
    for sg in out:
        if not sg.get('hold'):
            span = sg['ref_end'] - sg['ref_start']
            sg['speed'] = round((sg['vid_end'] - sg['vid_start']) / max(1e-6, span), 4)
    return out


# Auto-mix
#
# A song with several linked videos (the official video, and a movie scene or
# a game intro that uses the song) can mix them: the base video covers the
# whole song (its normal plan), and each higher-priority video is put in
# wherever its audio fits the chart audio, in its own order, each part of it
# once. The clips that use a song are usually too short to carry it alone.

def overlay_pieces(reference_audio, video_audio):
    """Where a video fits the reference: segments (ref/vid times, speed,
    'match' = cost relative to a random pairing) for every stretch of at
    least MIX_MIN seconds that matches well, each part of the video used once
    (where it fits best). [] when nothing fits (a wrong video)."""
    cost, _, quiet = _analyze(reference_audio, video_audio)
    n, m = cost.shape
    if not (~quiet).any():
        return []
    med = max(1e-6, float(np.median(cost[~quiet][:, ::7])))
    path, jumps = _viterbi(cost)
    w = max(1, int(MIX_SMOOTH * FPS))
    need = int(MIX_MIN * FPS)
    cands = []
    for sg in _segments(path, _fit_pieces(path, jumps)):
        sp = sg['speed']
        if abs(sp - 1) > MIX_MAX_SPEED_DEV:
            continue
        a = int(round(sg['ref_start'] * FPS))
        b = min(n, int(round(sg['ref_end'] * FPS)))
        if b - a < need:
            continue
        i = np.arange(a, b)
        t = sg['vid_start'] + (i / FPS - sg['ref_start']) * sp
        j = np.clip(np.round(t * FPS).astype(int), 0, m - 1)
        c = cost[i, j] / med
        c = np.convolve(np.pad(c, (w // 2, w - 1 - w // 2), mode='edge'),
                        np.ones(w) / w, 'valid')
        # silence inside a run (a break) doesn't end it, but doesn't count
        good = (c < MIX_EXTEND) | quiet[i]
        k = 0
        while k < len(good):
            if not good[k]:
                k += 1
                continue
            e = k
            while e < len(good) and good[e]:
                e += 1
            r0, r1 = k, e
            while r0 < r1 and quiet[i[r0]]:
                r0 += 1
            while r1 > r0 and quiet[i[r1 - 1]]:
                r1 -= 1
            if (~quiet[i[r0:r1]]).sum() >= need:
                p = _fit_line(cost, quiet, med, a + r0, a + r1,
                              (sg['vid_start'] + ((a + r0) / FPS - sg['ref_start']) * sp) * FPS,
                              sp)
                if p:
                    cands.append(p)
            k = e
    # each part of the video once, the longest fits first: a clip that has a
    # chorus once isn't put in at every chorus of the song, and a continuous
    # clip stays where it runs on
    out = []
    for p in sorted(cands, key=lambda p: (-(p['ref_end'] - p['ref_start']), p['match'])):
        if all(min(p['vid_end'], q['vid_end']) - max(p['vid_start'], q['vid_start']) < 1.0
               and min(p['ref_end'], q['ref_end']) - max(p['ref_start'], q['ref_start']) < 1.0
               for q in out):
            out.append(p)
    return sorted(out, key=lambda p: p['ref_start'])


def _line_rel(cost, quiet, med, r0, r1, v0, sp):
    """Smoothed relative cost along the line ref frame r0 -> video frame v0
    (fractional), speed sp, for ref frames [r0, r1); None if it leaves the video."""
    n, m = cost.shape
    i = np.arange(max(0, r0), min(n, r1))
    if i.size < 2:
        return None, i
    j = np.round(v0 + (i - r0) * sp).astype(int)
    ok = (j >= 0) & (j < m)
    i, j = i[ok], j[ok]
    if i.size < 2:
        return None, i
    c = cost[i, j] / med
    w = max(1, int(MIX_SMOOTH * FPS))
    c = np.convolve(np.pad(c, (w // 2, w - 1 - w // 2), mode='edge'), np.ones(w) / w, 'valid')
    return c, i


def _fit_line(cost, quiet, med, r0, r1, v0, sp):
    """A candidate fit, refined (speed 1.0 or the fitted one, offset within
    +-0.5 s) and extended both ways along its line while it still fits."""
    by_speed = {}
    for s in {round(float(sp), 4), 1.0}:
        for dv in np.arange(-0.5 * FPS, 0.5 * FPS + 0.01, 0.5):
            c, i = _line_rel(cost, quiet, med, r0, r1, v0 + dv, s)
            if c is None:
                continue
            loud = ~quiet[i]
            if not loud.any():
                continue
            score = float(c[loud].mean())
            if s not in by_speed or score < by_speed[s][0]:
                by_speed[s] = (score, v0 + dv, s)
    # clips use the song as released: speed 1.0 unless another fits clearly
    # better (the line fit's speed is easily off by a percent or two)
    best = by_speed.get(1.0)
    other = [b for s, b in by_speed.items() if s != 1.0]
    if other and (best is None or other[0][0] < best[0] * 0.9):
        best = other[0]
    if best is None or best[0] >= MIX_GOOD:
        return None
    _, v0, sp = best
    n, m = cost.shape
    # extend: walk back / forward while the match holds
    lo, hi = r0, r1
    lo_lim = max(0, r0 - int(np.ceil(v0 / sp)) if sp else 0)
    hi_lim = min(n, r1 + int((m - (v0 + (r1 - r0) * sp)) / sp))
    c, i = _line_rel(cost, quiet, med, lo_lim, r0, v0 - (r0 - lo_lim) * sp, sp)
    if c is not None:
        good = (c < MIX_EXTEND) | quiet[i]
        k = len(good)
        while k > 0 and good[k - 1]:
            k -= 1
        lo = int(i[k]) if k < len(good) else r0
    c, i = _line_rel(cost, quiet, med, r1, hi_lim, v0 + (r1 - r0) * sp, sp)
    if c is not None:
        good = (c < MIX_EXTEND) | quiet[i]
        k = 0
        while k < len(good) and good[k]:
            k += 1
        hi = int(i[k - 1]) + 1 if k else r1
    while lo < hi and quiet[lo]:
        lo += 1
    while hi > lo and quiet[hi - 1]:
        hi -= 1
    c, i = _line_rel(cost, quiet, med, lo, hi, v0 + (lo - r0) * sp, sp)
    if c is None:
        return None
    loud = ~quiet[i]
    vs = (v0 + (lo - r0) * sp) / FPS
    return {'ref_start': round(lo / FPS, 3), 'ref_end': round(hi / FPS, 3),
            'vid_start': round(float(max(0.0, vs)), 3),
            'vid_end': round(float(min(m / FPS, vs + (hi - lo) / FPS * sp)), 3),
            'speed': round(float(sp), 4), 'match': round(float(c[loud].mean()), 3)}


def _part(sg, r0, r1):
    """The piece of a segment between reference times r0 and r1."""
    sp = 0.0 if sg.get('hold') else sg['speed']
    out = dict(sg)
    out['vid_start'] = round(sg['vid_start'] + (r0 - sg['ref_start']) * sp, 3)
    out['vid_end'] = round(sg['vid_start'] + (r1 - sg['ref_start']) * sp, 3)
    out['ref_start'], out['ref_end'] = round(r0, 3), round(r1, 3)
    return out


def mix(base, layers):
    """Put overlay pieces into a base plan.

    base: plan()['segments'] of the base video (source 0), covering the song.
    layers: [(src, pieces)] from the lowest priority to the highest, pieces
        from overlay_pieces(); a later layer wins where they overlap.
    Returns segments with 'src' (index of the video they come from)."""
    tl = [dict(sg, src=sg.get('src', 0)) for sg in base]
    if not tl:
        return tl
    start = tl[0]['ref_start']          # the count-in stays black
    for src, pieces in layers:
        for p in pieces:
            p = dict(p, src=src)
            if p['ref_start'] < start:
                if p['ref_end'] - start < MIX_MIN:
                    continue
                p = _part(p, start, p['ref_end'])
            out = []
            for sg in tl:
                if sg['ref_end'] <= p['ref_start'] or sg['ref_start'] >= p['ref_end']:
                    out.append(sg)
                    continue
                if sg['ref_start'] < p['ref_start']:
                    out.append(_part(sg, sg['ref_start'], p['ref_start']))
                if sg['ref_end'] > p['ref_end']:
                    out.append(_part(sg, p['ref_end'], sg['ref_end']))
            out.append(p)
            tl = sorted(out, key=lambda sg: sg['ref_start'])

    # slivers left between inserts: the piece before plays on over them
    k = 0
    while k < len(tl) and len(tl) > 1:
        sg = tl[k]
        d = sg['ref_end'] - sg['ref_start']
        if d >= MIX_SHORT:
            k += 1
            continue
        prev = tl[k - 1] if k else None
        nxt = tl[k + 1] if k + 1 < len(tl) else None
        if prev is not None and not prev.get('hold'):
            prev['vid_end'] = round(prev['vid_end'] + d * prev['speed'], 3)
            prev['ref_end'] = sg['ref_end']
        elif nxt is not None and not nxt.get('hold') \
                and nxt['vid_start'] - d * nxt['speed'] >= 0:
            nxt['vid_start'] = round(nxt['vid_start'] - d * nxt['speed'], 3)
            nxt['ref_start'] = sg['ref_start']
        else:
            k += 1
            continue
        tl.pop(k)

    # neighbours that are one continuous piece of the same video
    merged = [tl[0]]
    for sg in tl[1:]:
        p = merged[-1]
        if sg['src'] == p['src'] and not sg.get('hold') and not p.get('hold') \
                and abs(sg['vid_start'] - p['vid_end']) < 0.25 \
                and abs(sg['speed'] - p['speed']) < 0.03:
            p['ref_end'], p['vid_end'] = sg['ref_end'], sg['vid_end']
            continue
        merged.append(sg)
    for sg in merged:
        if not sg.get('hold'):
            span = max(1e-6, sg['ref_end'] - sg['ref_start'])
            sg['speed'] = round((sg['vid_end'] - sg['vid_start']) / span, 4)
    return merged


def mix_log(segments, names):
    """Edit log lines for a mix: which video plays where. names[src] says
    what each video is ('official', 'meme: Shrek', ...)."""
    out = []
    for sg in segments:
        src = sg.get('src', 0)
        if not src:
            continue
        out.append({'action': 'mix', 'at': sg['ref_start'], 'src': src,
                    'start': sg['vid_start'], 'end': sg['vid_end'],
                    'text': f"{ts(sg['ref_start'])}-{ts(sg['ref_end'])}: {names[src]} "
                            f"{ts(sg['vid_start'])}-{ts(sg['vid_end'])} put in"})
    return out


# Rendering

def build_cmd(video, segments, audio=None, keep_audio=False, fps=30, crop='', post='',
              size=None):
    """ffmpeg command (without codec/output args) that cuts/stretches `video`
    per `segments`. Output streams: [v], plus [a] when audio or keep_audio.

    audio: a file to use as the output audio (e.g. the reference itself).
    keep_audio: cut and re-time the video's own audio the same way instead,
        handy to check the sync by ear or fix it later in an editor.
    crop: ffmpeg crop filter (see aspectcrop.crop_filter), applied per piece.
    post: extra video filters after the pieces are joined (e.g. a watermark).
    Auto-mix: video is a list of files (a segment's 'src' picks one), crop a
    list with one filter per file, and size (w, h) the frame every piece is
    scaled and padded to.
    """
    multi = isinstance(video, (list, tuple))
    if multi and not size:
        raise ValueError('a mix of videos needs a frame size')
    parts, vlabels, alabels, inputs = [], [], [], []
    for k, s in enumerate(segments):
        out_len = s['ref_end'] - s['ref_start']
        if s.get('hold'):
            # freeze one frame: a sliver of video padded out by tpad below
            src_len, factor = 0.1, 1.0
        else:
            src_len = max(1e-3, s['vid_end'] - s['vid_start'])
            factor = out_len / src_len
        # each piece gets its own seeked input: one shared input with several
        # trims makes ffmpeg buffer every frame for the later pieces (OOM)
        src = s.get('src', 0) if multi else 0
        path = video[src] if multi else video
        c = (crop[src] if src < len(crop) else '') if isinstance(crop, (list, tuple)) else crop
        inputs += ['-ss', f"{s['vid_start']:.3f}", '-t', f"{src_len:.3f}", '-i', path]
        pre = c + ',' if c else ''
        if size:
            w, h = size
            pre += (f'scale={w}:{h}:force_original_aspect_ratio=decrease,'
                    f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,')
        parts.append(f"[{k}:v]{pre}setpts=(PTS-STARTPTS)*{factor:.6f},fps={fps},"
                     f"tpad=stop_mode=clone:stop_duration={out_len:.3f},"
                     f"trim=duration={out_len:.3f},setpts=PTS-STARTPTS[v{k}]")
        vlabels.append(f'[v{k}]')
        if keep_audio and not audio:
            tempo = 1.0 if s.get('hold') else min(2.0, max(0.5, 1.0 / factor))
            fmt = 'aformat=sample_rates=48000:channel_layouts=stereo,' if multi else ''
            parts.append(f"[{k}:a]{fmt}asetpts=PTS-STARTPTS,atempo={tempo:.6f},"
                         f"apad,atrim=duration={out_len:.3f},asetpts=PTS-STARTPTS[a{k}]")
            alabels.append(f'[a{k}]')
    # the song may start before the video does (count-in): black until then
    lead = segments[0]['ref_start']
    graph = ';'.join(parts) + ';' + ''.join(vlabels) + f'concat=n={len(vlabels)}:v=1:a=0'
    if lead > 0.01:
        # before the video starts (the chart's count-in): black
        graph += f',tpad=start_duration={lead:.3f}:color=black'
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
           keep_audio=False, post='', size=None):
    """Cut/stretch `video` per `segments` into `dest` (see build_cmd).

    extra: ffmpeg codec args, default libx264 crf 20 + AAC.
    The result already starts with the song, so its offset is 0.
    """
    cmd = build_cmd(video, segments, audio=audio, keep_audio=keep_audio, fps=fps,
                    crop=crop, post=post, size=size)
    has_audio = bool(audio) or keep_audio
    cmd += extra or (['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20']
                     + (['-c:a', 'aac', '-b:a', '192k'] if has_audio else []))
    cmd += [dest]
    subprocess.run(cmd, check=True, creationflags=_NO_WINDOW)
    return dest


def needs_edit(plan_result):
    """True when the plan is more than a single offset (cuts, inserts or a
    speed change), i.e. when a plain video_start_time offset can't sync it.
    A frame held at the very start or end is what an offset does anyway."""
    segs = [s for s in plan_result.get('segments') or []
            if not (s.get('hold') and (s is plan_result['segments'][0]
                                       or s is plan_result['segments'][-1]))]
    return len(segs) > 1 or any(abs(s.get('speed', 1) - 1) > SPEED_EPS for s in segs)


def offset_ms(plan_result):
    """video_start_time for a single-segment plan (same sign as audiosync)."""
    s = next(x for x in plan_result['segments'] if not x.get('hold'))
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
    if segments and segments[0]['ref_start'] > GAP:
        entries.append({'action': 'black', 'at': 0.0, 'start': 0.0, 'end': 0.0,
                        'length': round(segments[0]['ref_start'], 3)})
    for sg in segments:
        if sg.get('hold'):
            entries.append({'action': 'hold', 'at': sg['ref_start'], 'start': sg['vid_start'],
                            'end': sg['vid_start'],
                            'length': round(sg['ref_end'] - sg['ref_start'], 3)})
    segments = [sg for sg in segments if not sg.get('hold')]
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
    order = {'black': -1, 'cut': 0, 'insert': 1, 'speed': 2, 'hold': 3}
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
    if e['action'] == 'black':
        return f"0:00.000-{ts(e['length'])} black (count-in, before the music)"
    if e['action'] == 'hold':
        return f"at {ts(e['at'])}, the frame at {ts(e['start'])} was held for {e['length']:.3f}s"
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
#
# The same formats auto-editor's --export offers. Every export puts the chart
# audio on the audio track and the edit's pieces of video on the video track
# (the videos' own audio is left off / muted: the chart audio is what counts).
# A frame hold becomes a 1x clip from that frame; a count-in is a gap (black).

def _probe_video(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                          '-show_entries', 'stream=width,height,r_frame_rate:format=duration',
                          '-of', 'default=nw=1', path], capture_output=True, text=True,
                         creationflags=_NO_WINDOW).stdout
    d = dict(l.split('=', 1) for l in out.split() if '=' in l)
    num, den = (d.get('r_frame_rate', '30/1').split('/') + ['1'])[:2]
    num, den = int(num or 30), max(1, int(den or 1))
    fps = round(num / den) or 30
    ntsc = den == 1001 and fps in (24, 30, 60)       # 23.976 / 29.97 / 59.94
    return {'width': int(d.get('width', 1920)), 'height': int(d.get('height', 1080)),
            'fps': fps, 'ntsc': ntsc,
            'rate': (fps * 1000, 1001) if ntsc else (fps, 1),   # exact frame rate
            'duration': float(d.get('duration', 0) or 0)}


def frame_size(video, crop=''):
    """(width, height) of a video after an aspectcrop crop filter, even
    numbers: the frame an auto-mix scales every video to."""
    if crop:
        w, h = (int(x) for x in crop.split('=', 1)[1].split(':')[:2])
    else:
        info = _probe_video(video)
        w, h = info['width'], info['height']
    return max(2, w - w % 2), max(2, h - h % 2)


# key -> (menu label, file extension, what opens it)
EDITOR_FORMATS = {
    'premiere': ('Adobe Premiere Pro', '.xml',
                 'File > Import, pick the .xml (Final Cut Pro 7 XML)'),
    'resolve':  ('DaVinci Resolve', '.xml',
                 'File > Import > Timeline, pick the .xml (Final Cut Pro 7 XML)'),
    'final-cut-pro': ('Final Cut Pro', '.fcpxml', 'File > Import > XML, pick the .fcpxml'),
    'shotcut':  ('Shotcut', '.mlt', 'File > Open, pick the .mlt'),
    'v3':       ('auto-editor (v3 timeline)', '.v3',
                 'auto-editor timeline.v3 -o out.mp4  (or --export <editor>)'),
}


def _clips(videos, segments, fps_exact):
    """Each segment as frame numbers at the sequence rate:
    (k, src index, start, end, source in, speed). Source in counts
    speed-scaled frames, the way auto-editor writes it."""
    out = []
    for k, sg in enumerate(segments, 1):
        sp = 1.0 if sg.get('hold') else max(1e-6, sg['speed'])
        start = round(sg['ref_start'] * fps_exact)
        end = max(start + 1, round(sg['ref_end'] * fps_exact))
        src = min(sg.get('src', 0), len(videos) - 1)
        out.append((k, src, start, end, round(sg['vid_start'] * fps_exact / sp), sp))
    return out


def _videos(video):
    return [video] if isinstance(video, str) else list(video)


def _timeline_info(videos, segments, audio, fps):
    info = _probe_video(videos[0])
    if fps:
        info.update(fps=fps, ntsc=False, rate=(fps, 1))
    num, den = info['rate']
    exact = num / den
    total = round(segments[-1]['ref_end'] * exact)
    if audio:
        total = max(total, round(_audio_duration(audio) * exact))
    return info, exact, total


def _audio_duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=nw=1:nk=1', path], capture_output=True, text=True,
                         creationflags=_NO_WINDOW).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def export_v3(video, segments, dest, audio=None, fps=None):
    """auto-editor v3 timeline. Render it or convert it with auto-editor:
        auto-editor timeline.v3 -o out.mp4
        auto-editor timeline.v3 --export premiere -o timeline.xml
    video: one file, or the auto-mix's list (a segment's 'src' picks one)."""
    videos = _videos(video)
    info, exact, total = _timeline_info(videos, segments, audio, fps)
    num, den = info['rate']
    v_clips = []
    for k, src, start, end, cin, sp in _clips(videos, segments, exact):
        clip = {'src': os.path.abspath(videos[src]), 'start': start, 'dur': end - start,
                'offset': cin, 'stream': 0}
        if abs(sp - 1.0) > SPEED_EPS:
            clip['effects'] = [f'speed:{sp:.4f}']
        v_clips.append(clip)
    a_clips = []
    if audio:
        a_clips = [{'src': os.path.abspath(audio), 'start': 0, 'dur': total,
                    'offset': 0, 'stream': 0}]
    tl = {'version': '3', 'timebase': f'{num}/{den}', 'background': '#000',
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


def _audio_info(path):
    """(channels, sample rate) of a file's first audio stream, (0, 0) if none."""
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                          '-show_entries', 'stream=channels,sample_rate',
                          '-of', 'default=nw=1', path], capture_output=True, text=True,
                         creationflags=_NO_WINDOW).stdout
    d = dict(l.split('=', 1) for l in out.split() if '=' in l)
    try:
        return int(d.get('channels', 0)), int(d.get('sample_rate', 0))
    except ValueError:
        return 0, 0


def export_xml(video, segments, dest, audio=None, fps=None, name='BackstageHero autoedit'):
    """Final Cut Pro 7 XML (xmeml v5), written the way auto-editor's --export
    premiere does; Premiere Pro and DaVinci Resolve import it. Speed changes
    are Time Remap filters. Audio follows Premiere's layout: the sequence
    declares its audio format and a stereo file is two "exploded" mono
    tracks (left, right). A1/A2 = the chart audio; A3/A4 = the videos' own
    audio, clips disabled (enable them to sync by ear).
    video: one file, or the auto-mix's list (a segment's 'src' picks one)."""
    from xml.sax.saxutils import escape as esc
    videos = _videos(video)
    info, exact, total = _timeline_info(videos, segments, audio, fps)
    ntsc = 'TRUE' if info['ntsc'] else 'FALSE'
    rate = f'<rate><timebase>{info["fps"]}</timebase><ntsc>{ntsc}</ntsc></rate>'
    tc = (f'<timecode><string>00:00:00:00</string><displayformat>NDF</displayformat>'
          f'{rate}</timecode>')
    vinfo = [info] + [_probe_video(v) for v in videos[1:]]
    vaud = [_audio_info(v) for v in videos]
    sr = 48000

    def aud_media(ch, rate_hz):
        return (f'<audio><samplecharacteristics><depth>16</depth><samplerate>{rate_hz}'
                f'</samplerate></samplecharacteristics><channelcount>{ch}</channelcount></audio>')

    written = set()

    def vfile(src):
        fid = f'file-{src + 1}'
        if src in written:
            return f'<file id="{fid}"/>'
        written.add(src)
        i = vinfo[src]
        ch, hz = vaud[src]
        return (f'<file id="{fid}"><name>{esc(os.path.basename(videos[src]))}</name>'
                f'<pathurl>{esc(_file_url(videos[src]))}</pathurl>{rate}'
                f'<duration>{round(i["duration"] * exact)}</duration>{tc}'
                f'<media><video><samplecharacteristics>{rate}<width>{i["width"]}</width>'
                f'<height>{i["height"]}</height><pixelaspectratio>square</pixelaspectratio>'
                f'</samplecharacteristics></video>'
                f'{aud_media(min(ch, 2), hz or sr) if ch else ""}</media></file>')

    def remap(sp):
        if abs(sp - 1.0) <= SPEED_EPS:
            return ''
        return ('<filter><effect><name>Time Remap</name><effectid>timeremap</effectid>'
                '<parameter authoringApp="PremierePro"><parameterid>variablespeed</parameterid>'
                '<name>variablespeed</name><valuemin>0</valuemin><valuemax>1</valuemax>'
                '<value>0</value></parameter>'
                '<parameter authoringApp="PremierePro"><parameterid>speed</parameterid>'
                f'<name>speed</name><valuemin>-100000</valuemin><valuemax>100000</valuemax>'
                f'<value>{sp * 100:.4f}</value></parameter>'
                '<parameter authoringApp="PremierePro"><parameterid>frameblending</parameterid>'
                '<name>frameblending</name><value>FALSE</value></parameter>'
                '</effect></filter>')

    pieces = _clips(videos, segments, exact)
    n = len(pieces)
    ids = iter(range(1, 10 ** 6))
    vids = [next(ids) for _ in pieces]
    # the videos' own audio: one clip per piece and channel, linked to its video clip
    has_vaud = all(ch for ch, _ in vaud)
    nch = 2 if has_vaud and min(ch for ch, _ in vaud) >= 2 else (1 if has_vaud else 0)
    aids = [[next(ids) for _ in pieces] for _ in range(nch)]

    def links(j):
        out = (f'<link><linkclipref>clipitem-{vids[j]}</linkclipref><mediatype>video</mediatype>'
               f'<trackindex>1</trackindex><clipindex>{j + 1}</clipindex></link>')
        for c in range(nch):
            out += (f'<link><linkclipref>clipitem-{aids[c][j]}</linkclipref>'
                    f'<mediatype>audio</mediatype><trackindex>{c + 3 if audio else c + 1}'
                    f'</trackindex><clipindex>{j + 1}</clipindex></link>')
        return out

    vclips = []
    for j, (k, src, start, end, cin, sp) in enumerate(pieces):
        vclips.append(f'<clipitem id="clipitem-{vids[j]}">'
                      f'<name>{esc(os.path.basename(videos[src]))}</name>'
                      f'<enabled>TRUE</enabled><start>{start}</start><end>{end}</end>'
                      f'<in>{cin}</in><out>{cin + end - start}</out>{vfile(src)}'
                      f'<compositemode>normal</compositemode>{remap(sp)}{links(j)}</clipitem>')

    def atrack(c, count, items):
        kind = 'Stereo' if count == 2 else 'Mono'
        out = (f' outputchannelindex="{c + 1}"' if count == 2 else '')
        return (f'<track currentExplodedTrackIndex="{c}" totalExplodedTrackCount="{count}" '
                f'premiereTrackType="{kind}"{out}>{"".join(items)}'
                f'<enabled>TRUE</enabled><locked>FALSE</locked></track>')

    tracks = []
    if audio:
        ach, ahz = _audio_info(audio)
        ach = 2 if ach >= 2 else 1
        aname = esc(os.path.basename(audio))
        for c in range(ach):
            f = (f'<file id="file-a"><name>{aname}</name>'
                 f'<pathurl>{esc(_file_url(audio))}</pathurl>{rate}'
                 f'<duration>{total}</duration>{tc}<media>{aud_media(ach, ahz or sr)}</media>'
                 f'</file>' if c == 0 else '<file id="file-a"/>')
            item = (f'<clipitem id="clipitem-{next(ids)}" premiereChannelType='
                    f'"{"stereo" if ach == 2 else "mono"}"><name>{aname}</name>'
                    f'<enabled>TRUE</enabled><start>0</start><end>{total}</end><in>0</in>'
                    f'<out>{total}</out>{f}<sourcetrack><mediatype>audio</mediatype>'
                    f'<trackindex>{c + 1}</trackindex></sourcetrack></clipitem>')
            tracks.append(atrack(c, ach, [item]))
    for c in range(nch):
        items = []
        for j, (k, src, start, end, cin, sp) in enumerate(pieces):
            items.append(f'<clipitem id="clipitem-{aids[c][j]}" premiereChannelType='
                         f'"{"stereo" if nch == 2 else "mono"}">'
                         f'<name>{esc(os.path.basename(videos[src]))}</name>'
                         f'<enabled>FALSE</enabled><start>{start}</start><end>{end}</end>'
                         f'<in>{cin}</in><out>{cin + end - start}</out>'
                         f'<file id="file-{src + 1}"/><sourcetrack><mediatype>audio</mediatype>'
                         f'<trackindex>{c + 1}</trackindex></sourcetrack>{remap(sp)}'
                         f'{links(j)}</clipitem>')
        tracks.append(atrack(c, nch, items))
    seq_audio = (f'<audio><numOutputChannels>2</numOutputChannels><format>'
                 f'<samplecharacteristics><depth>16</depth><samplerate>{sr}</samplerate>'
                 f'</samplecharacteristics></format>{"".join(tracks)}</audio>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5">'
           f'<sequence explodedTracks="true"><name>{esc(name)}</name>'
           f'<duration>{total}</duration>{rate}'
           f'<media><video><format><samplecharacteristics>{rate}<width>{info["width"]}</width>'
           f'<height>{info["height"]}</height><pixelaspectratio>square</pixelaspectratio>'
           f'</samplecharacteristics></format><track>{"".join(vclips)}</track></video>'
           f'{seq_audio}</media></sequence></xmeml>\n')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(xml)
    return dest


def export_fcpxml(video, segments, dest, audio=None, fps=None, name='BackstageHero autoedit'):
    """Final Cut Pro X XML (FCPXML 1.9). The chart audio is the primary
    storyline; the video pieces are connected clips above it (video only),
    speed changes are timeMaps.
    video: one file, or the auto-mix's list (a segment's 'src' picks one)."""
    from xml.sax.saxutils import escape as esc, quoteattr as qa
    videos = _videos(video)
    info, exact, total = _timeline_info(videos, segments, audio, fps)
    num, den = info['rate']

    def t(frames):                           # frames at the sequence rate -> rational
        return '0s' if not frames else f'{frames * den}/{num}s'

    def url(p):
        return 'file:///' + _file_url(p)[len('file://localhost/'):]

    res = [f'<format id="r0" frameDuration="{t(1)}" width="{info["width"]}" '
           f'height="{info["height"]}"/>']
    for k, v in enumerate(videos):
        dur = round(_probe_video(v)['duration'] * exact)
        res.append(f'<asset id="v{k}" name={qa(os.path.basename(v))} start="0s" '
                   f'duration="{t(dur)}" hasVideo="1" hasAudio="1" format="r0" '
                   f'audioSources="1" audioChannels="2">'
                   f'<media-rep kind="original-media" src={qa(url(v))}/></asset>')
    if audio:
        res.append(f'<asset id="a0" name={qa(os.path.basename(audio))} start="0s" '
                   f'duration="{t(total)}" hasAudio="1" audioSources="1" audioChannels="2" '
                   f'audioRate="48000"><media-rep kind="original-media" src={qa(url(audio))}/>'
                   f'</asset>')
    conn = []
    for k, src, start, end, cin, sp in _clips(videos, segments, exact):
        tmap = ''
        src_in = cin
        if abs(sp - 1.0) > SPEED_EPS:
            # local clip time -> source time; source frames back at 1x
            s0 = round(cin * sp)
            s1 = round((cin + end - start) * sp)
            tmap = (f'<timeMap><timept time="0s" value="{t(s0)}" interp="linear"/>'
                    f'<timept time="{t(end - start)}" value="{t(s1)}" interp="linear"/></timeMap>')
            src_in = 0
        conn.append(f'<asset-clip ref="v{src}" lane="1" offset="{t(start)}" '
                    f'start="{t(src_in)}" duration="{t(end - start)}" srcEnable="video" '
                    f'name={qa(os.path.basename(videos[src]))}>{tmap}</asset-clip>')
    if audio:
        spine = (f'<asset-clip ref="a0" offset="0s" start="0s" duration="{t(total)}" '
                 f'name={qa(os.path.basename(audio))}>{"".join(conn)}</asset-clip>')
    else:
        spine = f'<gap offset="0s" start="0s" duration="{t(total)}">{"".join(conn)}</gap>'
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.9">'
           f'<resources>{"".join(res)}</resources><library><event name="BackstageHero">'
           f'<project name={qa(name)}><sequence format="r0" duration="{t(total)}" tcStart="0s" '
           f'tcFormat="NDF" audioLayout="stereo" audioRate="48k"><spine>{spine}</spine>'
           f'</sequence></project></event></library></fcpxml>\n')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(xml)
    return dest


def export_mlt(video, segments, dest, audio=None, fps=None, name='BackstageHero autoedit'):
    """Shotcut project (MLT XML). V1 has the video pieces (their own audio
    muted), A1 the chart audio; speed changes are timewarp producers.
    video: one file, or the auto-mix's list (a segment's 'src' picks one)."""
    from xml.sax.saxutils import escape as esc, quoteattr as qa
    videos = _videos(video)
    info, exact, total = _timeline_info(videos, segments, audio, fps)
    num, den = info['rate']
    w, h = info['width'], info['height']

    def prop(k, v):
        return f'<property name="{k}">{esc(str(v))}</property>'

    producers, ids = [], {}

    def producer(src, sp):
        key = (src, round(sp, 4))
        if key not in ids:
            pid = ids[key] = f'producer{len(ids)}'
            path = os.path.abspath(videos[src]).replace('\\', '/')
            if abs(sp - 1.0) > SPEED_EPS:
                body = (prop('resource', f'{sp:.4f}:{path}') + prop('mlt_service', 'timewarp')
                        + prop('warp_speed', f'{sp:.4f}') + prop('warp_resource', path)
                        + prop('warp_pitch', 0))
            else:
                body = prop('resource', path) + prop('mlt_service', 'avformat-novalidate')
            producers.append(f'<producer id="{pid}">{body}'
                             + prop('shotcut:caption', os.path.basename(path)) + '</producer>')
        return ids[key]

    entries, at = [], 0
    for k, src, start, end, cin, sp in _clips(videos, segments, exact):
        if start > at:
            entries.append(f'<blank length="{start - at}"/>')
        pid = producer(src, sp)
        entries.append(f'<entry producer="{pid}" in="{cin}" out="{cin + end - start - 1}"/>')
        at = end
    tracks = ['<track producer="background"/>', '<track producer="playlist0" hide="audio"/>']
    trans = ['<transition id="transition0">' + prop('a_track', 0) + prop('b_track', 1)
             + prop('mlt_service', 'mix') + prop('always_active', 1) + prop('sum', 1)
             + '</transition>',
             '<transition id="transition1">' + prop('a_track', 0) + prop('b_track', 1)
             + prop('version', '0.1') + prop('mlt_service', 'frei0r.cairoblend')
             + prop('threads', 0) + prop('disable', 1) + '</transition>']
    apl = ''
    if audio:
        path = os.path.abspath(audio).replace('\\', '/')
        producers.append('<producer id="audio0">' + prop('length', total)
                         + prop('resource', path) + prop('mlt_service', 'avformat-novalidate')
                         + prop('shotcut:caption', os.path.basename(path)) + '</producer>')
        apl = ('<playlist id="playlist1">' + prop('shotcut:audio', 1)
               + prop('shotcut:name', 'A1')
               + f'<entry producer="audio0" in="0" out="{total - 1}"/></playlist>')
        tracks.append('<track producer="playlist1" hide="video"/>')
        trans.append('<transition id="transition2">' + prop('a_track', 0) + prop('b_track', 2)
                     + prop('mlt_service', 'mix') + prop('always_active', 1) + prop('sum', 1)
                     + '</transition>')
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           f'<mlt LC_NUMERIC="C" version="7.0.0" title={qa(name)} producer="main_bin">'
           f'<profile description="{w}x{h} {num / den:.3f} fps" width="{w}" height="{h}" '
           f'progressive="1" sample_aspect_num="1" sample_aspect_den="1" '
           f'display_aspect_num="{w}" display_aspect_den="{h}" frame_rate_num="{num}" '
           f'frame_rate_den="{den}" colorspace="709"/>'
           '<playlist id="main_bin">' + prop('xml_retain', 1) + '</playlist>'
           f'<producer id="black" in="0" out="{total - 1}">' + prop('length', total)
           + prop('eof', 'pause') + prop('resource', '0') + prop('aspect_ratio', 1)
           + prop('mlt_service', 'color') + prop('mlt_image_format', 'rgba')
           + prop('set.test_audio', 0) + '</producer>'
           f'<playlist id="background"><entry producer="black" in="0" out="{total - 1}"/>'
           '</playlist>' + ''.join(producers)
           + '<playlist id="playlist0">' + prop('shotcut:video', 1) + prop('shotcut:name', 'V1')
           + ''.join(entries) + '</playlist>' + apl
           + f'<tractor id="tractor0" title={qa(name)} in="0" out="{total - 1}">'
           + prop('shotcut', 1) + ''.join(tracks) + ''.join(trans) + '</tractor></mlt>\n')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(xml)
    return dest


def export_timeline(fmt, video, segments, dest, audio=None, name='BackstageHero autoedit'):
    """Write the edit for an editor; fmt is an EDITOR_FORMATS key."""
    if fmt in ('premiere', 'resolve'):
        return export_xml(video, segments, dest, audio=audio, name=name)
    if fmt == 'final-cut-pro':
        return export_fcpxml(video, segments, dest, audio=audio, name=name)
    if fmt == 'shotcut':
        return export_mlt(video, segments, dest, audio=audio, name=name)
    if fmt == 'v3':
        return export_v3(video, segments, dest, audio=audio)
    raise ValueError(f'unknown editor format {fmt!r}')


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
    ap.add_argument('--xml', help='write a Final Cut Pro 7 XML timeline (Premiere, Resolve)')
    ap.add_argument('--fcpxml', help='write a Final Cut Pro X (FCPXML) timeline')
    ap.add_argument('--mlt', help='write a Shotcut (MLT) project')
    ap.add_argument('--mix', action='append', default=[], metavar='CLIP',
                    help='auto-mix: put this video in wherever it fits the audio (repeat it; '
                         'later ones win where they overlap)')
    a = ap.parse_args(argv)
    timelines = [(f, getattr(a, f)) for f in ('v3', 'xml', 'fcpxml', 'mlt') if getattr(a, f)]
    if not a.output and not (a.plan or a.json or timelines):
        ap.error('output is required unless --plan, --json, --v3, --xml, --fcpxml or --mlt is given')

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
    videos = [a.video]
    if a.mix:
        layers = []
        for k, clip in enumerate(a.mix, 1):
            pieces = overlay_pieces(a.reference, clip)
            print(f'mix {clip}: ' + (f'{len(pieces)} part(s) fit' if pieces else 'nothing fits'))
            if pieces:
                videos.append(clip)
                layers.append((len(videos) - 1, pieces))
        p['segments'] = mix(p['segments'], layers)
        p['log'] = mix_log(p['segments'], [os.path.basename(v) for v in videos]) + p['log']
        for e in p['log']:
            if e['action'] == 'mix':
                print('  ' + e['text'])
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(p, f, indent=1)
        print('wrote', a.json)
    fmt = {'v3': 'v3', 'xml': 'premiere', 'fcpxml': 'final-cut-pro', 'mlt': 'shotcut'}
    for f, dest in timelines:
        print('wrote', export_timeline(fmt[f], videos, p['segments'], dest, audio=a.reference))
    if a.plan or not a.output:
        return p
    crops = []
    for v in videos:
        crop = ''
        if a.crop:
            import aspectcrop
            c = aspectcrop.analyze(v, coverage=a.coverage)
            print(c['info'])
            crop = aspectcrop.crop_filter(c.get('crop'))
        crops.append(crop)
    p['crop'] = crops[0]
    if len(videos) > 1:
        render(videos, p['segments'], a.output, audio=a.reference if a.audio else None,
               keep_audio=a.keep_audio, crop=crops, size=frame_size(videos[0], crops[0]))
    else:
        render(a.video, p['segments'], a.output, audio=a.reference if a.audio else None,
               keep_audio=a.keep_audio, crop=crops[0])
    print('wrote', a.output)
    return p


if __name__ == '__main__':
    main()
