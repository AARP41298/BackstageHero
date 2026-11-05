"""
Works out how far into a downloaded video the song actually starts, so it can be
lined up with the chart instead of guessing a fixed offset.

Landmark fingerprinting: find peaks in each spectrogram, hash pairs of them, then
look for a consistent time shift between the chart audio and the video audio.
Matching peak patterns instead of the raw waveform means it still works when the
YouTube rip is louder or mastered differently than the chart stems. Weak match
(live version, remix, wrong song) and it returns nothing, so the caller falls
back to the default.

Needs numpy and ffmpeg.
"""

import os
import subprocess

# Suppress the console window ffmpeg would otherwise flash on a windowed build.
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    _NUMPY = True
except Exception:
    _NUMPY = False


# Tunables

SAMPLE_RATE = 8000      # Hz. Plenty for fingerprinting; keeps the FFTs small.
ANALYZE_SECONDS = 240   # Only the first few minutes are needed to find the offset.

N_FFT = 1024            # ~128 ms window at 8 kHz.
HOP = 128               # ~16 ms between frames -> 16 ms offset resolution.

PEAK_TIME_RADIUS = 5    # Local-maximum neighbourhood, in frames...
PEAK_FREQ_RADIUS = 10   # ...and in frequency bins.
PEAK_QUANTILE = 0.97    # Per-frame magnitude cutoff before a point can be a peak.

FAN_OUT = 8             # Pair each anchor peak with up to this many later peaks.
DT_MIN = 1              # Minimum frame gap between paired peaks.
DT_MAX = 120            # Maximum frame gap (~2 s).

HIST_BIN = 3            # Offset histogram bin width, in frames (~48 ms).

# All three have to pass before an alignment counts. Picked these by running
# real charts against the matching YouTube audio: a true match blows past every
# threshold (thousands of votes, concentration over 0.2), a wrong song barely
# registers (a handful of votes, under 0.01). Huge gap between the two, so these
# just need to land somewhere in the middle.
MIN_MATCHES = 40        # Votes in the winning offset bin.
MIN_SCORE = 50.0        # Winning bin height relative to the average bin.
MIN_CONC = 0.05         # Share of all votes that land on the winning offset.

# A looped song section lines up equally well one loop apart, which makes the
# offset a coin flip. If any bin away from the winner gets within this factor
# of the winning bin, the alignment is ambiguous and we pass on it.
AMBIGUITY_RATIO = 2.5

# Different cuts of the same song (added intermission, extended edit) align
# perfectly at the start and then jump. The early and late halves of the match
# are checked separately; if they land more than this many bins apart the
# video is a different edit and syncing the start would drift later on.
DRIFT_BINS = 4          # ~200 ms

# Reject offsets outside a plausible window for a music-video lead-in.
MIN_LEAD_SECONDS = -30.0
MAX_LEAD_SECONDS = 90.0

# Audio stems that should not feed the fingerprint: a short preview clip is not
# the full song, and crowd noise is not in the studio video.
EXCLUDE_STEMS = {'preview', 'crowd'}
AUDIO_EXTS = {'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'}


def is_available():
    """True if auto-sync can run (numpy importable)."""
    return _NUMPY


# ffmpeg decoding

def _decode(inputs, sr, seconds):
    """Decode one or more audio files down to a single mono float array.

    Multiple inputs are mixed together with ffmpeg's amix so a chart split into
    instrument stems is treated as one full-song waveform. Returns None on any
    failure (missing ffmpeg, unreadable file, empty output).
    """
    cmd = ['ffmpeg', '-v', 'quiet']
    for path in inputs:
        cmd += ['-i', path]

    if len(inputs) > 1:
        mix = ''.join(f'[{i}:a]' for i in range(len(inputs)))
        mix += f'amix=inputs={len(inputs)}[a]'
        cmd += ['-filter_complex', mix, '-map', '[a]']

    cmd += ['-t', str(seconds), '-ac', '1', '-ar', str(sr), '-f', 's16le', '-']

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    samples = np.frombuffer(proc.stdout, dtype='<i2').astype(np.float32) / 32768.0
    return samples if samples.size >= N_FFT else None


def chart_stems(folder, exclude=None):
    """A song folder's audio stems, minus preview/crowd and any excluded file.

    The fingerprinter and the GUI preview both call this so they always mix the
    same set of files. `exclude` is the throwaway audio we fetch for syncing,
    which sits in the same folder and isn't a real stem."""
    exclude = os.path.abspath(exclude) if exclude else None
    stems = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return stems
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.lower() not in AUDIO_EXTS or base.lower() in EXCLUDE_STEMS:
            continue
        path = os.path.join(folder, name)
        if exclude and os.path.abspath(path) == exclude:
            continue
        stems.append(path)
    return stems


# old name, kept so existing callers don't break
_chart_stems = chart_stems


# Fingerprinting

def _spectrogram(x):
    frames = sliding_window_view(x, N_FFT)[::HOP]
    frames = frames * np.hanning(N_FFT).astype(np.float32)
    return np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)


def _peaks(spec):
    """Spectral peaks as (time_frame, freq_bin) arrays.

    Keep a point if it's the max in its time/freq rectangle and above a per-frame
    cutoff. A rectangular max filter is separable, so do a rolling max down each
    axis instead of one 2D pass.
    """
    padded = np.pad(spec, ((0, 0), (PEAK_FREQ_RADIUS, PEAK_FREQ_RADIUS)))
    fmax = sliding_window_view(padded, 2 * PEAK_FREQ_RADIUS + 1, axis=1).max(axis=-1)
    padded = np.pad(fmax, ((PEAK_TIME_RADIUS, PEAK_TIME_RADIUS), (0, 0)))
    local_max = sliding_window_view(padded, 2 * PEAK_TIME_RADIUS + 1, axis=0).max(axis=-1)

    floor = spec.max() * 1e-3
    frame_cut = np.quantile(spec, PEAK_QUANTILE, axis=1, keepdims=True)
    mask = (spec == local_max) & (spec >= frame_cut) & (spec > floor)

    t, f = np.nonzero(mask)   # np.nonzero returns time-ascending order
    return t, f


def _fingerprint(t, f):
    """Map each fingerprint hash to the anchor times that produced it.

    Peaks come in sorted by time, so the targets to pair with are just the next
    few peaks inside the allowed gap. Each hash packs both frequency bins and the
    gap between them.
    """
    table = {}
    n = len(t)
    for i in range(n):
        ti, fi = t[i], f[i]
        paired = 0
        j = i + 1
        while j < n and paired < FAN_OUT:
            dt = t[j] - ti
            if dt < DT_MIN:
                j += 1
                continue
            if dt > DT_MAX:
                break
            h = (int(fi) & 0x3FF) | ((int(f[j]) & 0x3FF) << 10) | ((int(dt) & 0x7F) << 20)
            table.setdefault(h, []).append(int(ti))
            paired += 1
            j += 1
    return table


def _offset_seconds(ref_table, probe_table):
    """Most consistent time offset between probe (audio) and reference (chart).

    For every shared hash, the difference between the probe anchor time and the
    reference anchor time is one vote. The true lead-in shows up as a sharp spike
    in the vote histogram; everything else is spread thin. Returns
    (lead_seconds, votes, score, conc) or None when no spike clears the gates.
    """
    tps, deltas = [], []
    for h, probe_times in probe_table.items():
        ref_times = ref_table.get(h)
        if not ref_times:
            continue
        for tp in probe_times:
            for tr in ref_times:
                tps.append(tp)
                deltas.append(tp - tr)   # audio time minus chart time = lead-in

    if len(deltas) < MIN_MATCHES:
        return None

    tps = np.asarray(tps, dtype=np.int64)
    deltas = np.asarray(deltas, dtype=np.float64)
    bins = np.round(deltas / HIST_BIN).astype(np.int64)
    values, counts = np.unique(bins, return_counts=True)

    top = counts.argmax()
    votes = int(counts[top])
    score = votes / counts.mean()

    # Votes within one bin of the winner, as a share of all votes. A real match
    # piles most of its votes onto a single offset; noise stays spread out.
    near = np.abs(bins - values[top]) <= 1
    conc = float(near.sum()) / len(deltas)

    if votes < MIN_MATCHES or score < MIN_SCORE or conc < MIN_CONC:
        return None

    # Second-spike check: if a rival offset away from the winner gets anywhere
    # close, the song probably repeats itself and either offset is a guess.
    # Bins within DRIFT_BINS of the winner are the same alignment smeared by a
    # slightly different master/speed, not a loop; a repeat is seconds away.
    rival_mask = np.abs(values - values[top]) > DRIFT_BINS
    if rival_mask.any():
        rival = int(counts[rival_mask].max())
        if rival * AMBIGUITY_RATIO > votes:
            return None

    # Drift check: judge the early and late halves of the matched span on
    # their own. A same-cut video puts both halves on the winning offset; a
    # different edit shows a second consistent offset later in the song, and
    # a start-only sync would visibly drift there.
    lo, hi = int(tps[near].min()), int(tps[near].max())
    mid = (lo + hi) / 2
    for half in (tps < mid, tps >= mid):
        hbins = bins[half]
        if hbins.size < MIN_MATCHES:
            continue    # too little signal in this half to judge either way
        hvals, hcounts = np.unique(hbins, return_counts=True)
        hwin = int(hvals[hcounts.argmax()])
        if int(hcounts.max()) >= MIN_MATCHES and abs(hwin - int(values[top])) > DRIFT_BINS:
            return None

    # Refine using the exact deltas in and next to the winning bin.
    lead = float(deltas[near].mean()) * HOP / SAMPLE_RATE
    if not (MIN_LEAD_SECONDS <= lead <= MAX_LEAD_SECONDS):
        return None
    return lead, votes, score, conc


# One-entry cache of the chart-side fingerprint table. select_video probes
# several candidates against the same chart back to back and the stems don't
# change between probes, so the chart only needs decoding and fingerprinting
# once per song instead of once per candidate.
_ref_cache = {'key': None, 'table': None}


def _chart_table(folder, exclude):
    """Fingerprint table for the chart stems, cached across candidate probes."""
    stems = _chart_stems(folder, exclude=exclude)
    if not stems:
        return None, 'no chart audio found'
    try:
        key = (folder, tuple((s, os.path.getmtime(s), os.path.getsize(s))
                             for s in stems))
    except OSError:
        key = None
    if key is not None and _ref_cache['key'] == key:
        return _ref_cache['table'], None
    chart = _decode(stems, SAMPLE_RATE, ANALYZE_SECONDS)
    if chart is None:
        return None, 'could not decode audio'
    table = _fingerprint(*_peaks(_spectrogram(chart)))
    if not table:
        return None, 'not enough audio detail'
    if key is not None:
        _ref_cache['key'], _ref_cache['table'] = key, table
    return table, None


def compute_offset_ms(folder, probe_audio):
    """Work out video_start_time (ms) for one song folder.

    probe_audio is the audio of the chosen YouTube result (video.mp4 has no audio
    so it's fetched separately), aligned against the chart's stems.

    Returns (milliseconds, info_string, confidence 0..1), or (None, reason, 0.0)
    if sync isn't available or the match can't be trusted. Sign matches what v1
    used: audio starting L seconds into the video gives video_start_time =
    -L*1000, so a 3s lead-in is -3000.
    """
    if not _NUMPY:
        return None, 'numpy not available', 0.0

    if not probe_audio or not os.path.exists(probe_audio):
        return None, 'no audio to sync', 0.0

    ref_table, err = _chart_table(folder, probe_audio)
    if ref_table is None:
        return None, err, 0.0

    probe = _decode([probe_audio], SAMPLE_RATE, ANALYZE_SECONDS)
    if probe is None:
        return None, 'could not decode audio', 0.0

    probe_table = _fingerprint(*_peaks(_spectrogram(probe)))
    if not probe_table:
        return None, 'not enough audio detail', 0.0

    result = _offset_seconds(ref_table, probe_table)
    if result is None:
        return None, 'no confident match', 0.0

    lead, votes, score, conc = result
    ms = -int(round(lead * 1000))
    info = f'lead-in {lead:.2f}s, {votes} matches, {score:.0f}x confidence'
    # concentration is the strongest single quality signal; a solid match sits
    # around 0.2-0.5, the gate floor is 0.05. Scale so a good match reports ~1.
    conf = min(1.0, conc / 0.25)
    return ms, info, conf
