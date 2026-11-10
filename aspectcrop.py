"""
Find the black bars of a video and crop to a standard aspect ratio.

Auto-crop tools (HandBrake, Topaz) cut to the exact detected edge, which eats
real pixels on frames where the picture reaches further than the detector
thinks. Here the whole video is scanned, and the crop is snapped *outwards* to
the nearest common ratio, so a thin black line may stay but picture never goes.

Videos like Blinding Lights change aspect ratio along the way. Every sampled
frame is measured, and `coverage` decides how many frames must fit uncropped:
1.0 (default) keeps every frame whole; 0.9 lets the widest 10% of frames lose
their edges in exchange for a tighter (more ultra-wide) crop.

CLI:  python aspectcrop.py video [--coverage 0.9] [--fps 2]
"""

import subprocess
import sys

import numpy as np

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# width / height. Flat and scope cinema ratios plus the usual TV/phone ones.
RATIOS = {
    '1:1': 1.0, '4:3': 4 / 3, '3:2': 1.5, '16:10': 1.6, '16:9': 16 / 9,
    '1.85 flat': 1.85, '2:1 Univisium': 2.0, '2.20 70mm': 2.2,
    '2.35 Scope': 2.35, '2.39 Scope': 2.39, '2.55 CinemaScope': 2.55,
    '2.76 Ultra Panavision': 2.76, '32:9': 32 / 9,
}

THRESHOLD = 20       # luma 0-255; above this a pixel counts as picture
MIN_RUN = 0.004      # a line needs this share of bright pixels (ignores specks)
PROBE_W = 0          # analysis width; 0 = full resolution (exact edges)


def _probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                          '-show_entries', 'stream=width,height:format=duration',
                          '-of', 'default=nw=1', path], capture_output=True,
                         text=True, creationflags=_NO_WINDOW).stdout
    d = dict(l.split('=', 1) for l in out.split() if '=' in l)
    return int(d['width']), int(d['height']), float(d.get('duration', 0) or 0)


def _frames(path, w, h, fps):
    """Yield grayscale frames sampled `fps` times per second over the whole video."""
    pw = PROBE_W or w
    ph = int(round(h * pw / w / 2)) * 2
    cmd = ['ffmpeg', '-v', 'error', '-i', path, '-an',
           '-vf', f'fps={fps},scale={pw}:{ph}:flags=area,format=gray',
           '-f', 'rawvideo', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, creationflags=_NO_WINDOW)
    size = pw * ph
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(ph, pw)
    p.wait()


def _extent(mask_lines, n):
    idx = np.nonzero(mask_lines)[0]
    return (int(idx[0]), int(idx[-1]) + 1) if idx.size else None


def analyze(path, coverage=1.0, fps=2.0):
    """Scan the video and pick the crop.

    Returns dict: crop (w, h, x, y) in source pixels or None when no bars,
    ratio name, per-ratio share of frames, and a short info string.
    """
    W, H, _ = _probe(path)
    boxes = []
    for f in _frames(path, W, H, fps):
        bright = f > THRESHOLD
        rows = _extent(bright.mean(axis=1) > MIN_RUN, f.shape[0])
        cols = _extent(bright.mean(axis=0) > MIN_RUN, f.shape[1])
        if rows and cols:            # fully black frames say nothing
            boxes.append((rows[0], rows[1], cols[0], cols[1], f.shape))
    if not boxes:
        return {'crop': None, 'info': 'no picture found'}

    ph, pw = boxes[0][4]
    sx, sy = W / pw, H / ph
    tops = np.array([b[0] for b in boxes]) * sy
    bots = np.array([b[1] for b in boxes]) * sy
    lefts = np.array([b[2] for b in boxes]) * sx
    rights = np.array([b[3] for b in boxes]) * sx
    heights, widths = bots - tops, rights - lefts

    # how many frames would fit uncropped in each standard ratio
    src_ratio = W / H
    share = {}
    for name, r in RATIOS.items():
        cw, ch = (W, W / r) if r >= src_ratio else (H * r, H)
        share[name] = float(np.mean((heights <= ch + 1) & (widths <= cw + 1)))

    # content size at the requested coverage (quantile, padded by 1 probe pixel)
    # analysed below full resolution, an edge can be off by one probe pixel
    need_h = min(H, np.quantile(heights, coverage) + (sy if sy > 1 else 0))
    need_w = min(W, np.quantile(widths, coverage) + (sx if sx > 1 else 0))

    # tightest standard ratio that still contains need_w x need_h
    best = None
    for name, r in RATIOS.items():
        cw, ch = (W, W / r) if r >= src_ratio else (H * r, H)
        if ch + 0.5 >= need_h and cw + 0.5 >= need_w:
            area = cw * ch
            if best is None or area < best[0]:
                best = (area, name, cw, ch)
    _, name, cw, ch = best
    cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2

    if cw >= W - 2 and ch >= H - 2:
        return {'crop': None, 'ratio': name, 'share': share, 'frames': len(boxes),
                'info': f'no bars to crop ({len(boxes)} frames)'}

    # centre on where the picture actually sits (bars aren't always symmetric)
    cy = float(np.median((tops + bots) / 2))
    cx = float(np.median((lefts + rights) / 2))
    y = int(min(max(0, cy - ch / 2), H - ch)) // 2 * 2
    x = int(min(max(0, cx - cw / 2), W - cw)) // 2 * 2
    saved = 1 - (cw * ch) / (W * H)
    info = (f'{W}x{H} -> {cw}x{ch} ({name}), {saved:.0%} fewer pixels, '
            f'{len(boxes)} frames, coverage {coverage:.0%}')
    return {'crop': (cw, ch, x, y), 'ratio': name, 'share': share,
            'frames': len(boxes), 'info': info}


def crop_filter(crop):
    """ffmpeg filter string for analyze()['crop'], or '' for no crop."""
    if not crop:
        return ''
    w, h, x, y = crop
    return f'crop={w}:{h}:{x}:{y}'


if __name__ == '__main__':
    args = sys.argv[1:]
    cov = float(args[args.index('--coverage') + 1]) if '--coverage' in args else 1.0
    fps = float(args[args.index('--fps') + 1]) if '--fps' in args else 2.0
    r = analyze(args[0], coverage=cov, fps=fps)
    print(r['info'])
    if r.get('crop'):
        print('filter:', crop_filter(r['crop']))
    for name, s in r.get('share', {}).items():
        print(f'  {name:24s} {s:6.1%} of frames fit uncropped')
