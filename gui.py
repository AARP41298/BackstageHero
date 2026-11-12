# BackstageHero GUI.
# All download/sync logic lives in VideoDownload.py; this file owns the window.

import os
import sys
import logging
import random
import re
import subprocess
import tempfile
import threading
import queue
import time
from dataclasses import dataclass

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from VideoDownload import (
    read_metadata, build_query, run_song_with_backoff,
    quality_format, get_stored_resolution, set_ini_values,
    ffmpegAvailable, ffplayPath, audiosync, __version__,
    DEFAULT_START_TIME, get_stored_source, NO_WINDOW,
    SONG_DELAY_MIN, SONG_DELAY_MAX, probe_resolution, scan_song,
    probe_video_height, _read_ini_value as _ini_value,
)
import conpack
from concurrent.futures import ThreadPoolExecutor, as_completed
import updater
import resolver_client
import mvdb

log = logging.getLogger('backstagehero')

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('blue')

# Every font in the app goes through this, so text size is one knob.
FONT_SCALE = 1.25


def _fs(size):
    return int(round(size * FONT_SCALE))


def _font(size, **kw):
    return ctk.CTkFont(size=_fs(size), **kw)


# widgets without an explicit font use the theme default
ctk.ThemeManager.theme['CTkFont']['size'] = _fs(
    ctk.ThemeManager.theme['CTkFont'].get('size', 13))


def _asset_path(name):
    """Resolve a bundled asset path - works both frozen (PyInstaller) and from source."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'assets', name)


def _load_mpv():
    """Load the optional embedded player (libmpv via python-mpv).

    Returns the mpv module if libmpv-2.dll is present and loads, else None.
    The sync editor uses it for a live, in-window preview; if it's missing
    the editor falls back to launching ffplay."""
    try:
        dirs = []
        if getattr(sys, 'frozen', False):
            dirs.append(getattr(sys, '_MEIPASS', ''))
            dirs.append(os.path.dirname(sys.executable))
        else:
            dirs.append(os.path.dirname(os.path.abspath(__file__)))
        for d in dirs:
            if d and os.path.exists(os.path.join(d, 'libmpv-2.dll')):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
                break
        import mpv as _mpv
        return _mpv
    except Exception:
        return None


mpvlib = _load_mpv()

_BG      = '#1e1e2e'
_SURFACE = '#252535'
_BORDER  = '#383850'
_TEXT    = '#cdd6f4'
_SUBTEXT = '#7f849c'
_GREEN   = '#a6e3a1'
_RED     = '#f38ba8'
_YELLOW  = '#f9e2af'
_BLUE    = '#89b4fa'
_MAUVE   = '#cba6f7'

# Actions column for CON songs: key -> (icon, hover hint), one narrow tree
# column per icon so a click can tell which one was hit.
_ACTIONS = {
    'play':    ('▶',    'Play video in your default player'),
    'collage': ('👁',    'Show frame collage'),
    'search':  ('🔍',   'Wrong video: search YouTube again (skips the database link)'),
    'download': ('⬇',   'Download the video type picked in the Video type column (again)'),
    'mp4':     ('Export as MP4',     'Render to MP4 (fast) and approve this video'),
    'webm':    ('Add WebM to queue', 'Add to the WebM queue (smaller, slow): render them all with Export WebM(s)'),
    'clear':   ('🗑',   'Clear cache: delete the kept original download'),
    'more':    ('@',    'Video links: search YouTube, open, set or fix the links'),
}
_ACTION_KEYS = tuple(_ACTIONS)

# the ▾ next to Start: key -> (button label, export format after download, help)
_DL_MODES = {
    'review': ('Search & Review', '', 'stop for review (play, collage, then export)'),
    'webm':   ('Download → WebM', 'webm', 'export each synced CON song to WebM'),
    'mp4':    ('Download → MP4',  'mp4',  'export each synced CON song to MP4'),
}
_ACTION_COLS = tuple('act_' + k for k in _ACTION_KEYS)
_ACTION_W = _fs(46)
# the text actions need more room than the icons
_ACTION_WIDTHS = {k: _fs({'mp4': 110, 'webm': 140}.get(k, 46)) for k in _ACTION_KEYS}

_SONGS_FILE = os.path.join(updater.data_dir(), 'songs_path.txt')


def _load_songs_path():
    try:
        with open(_SONGS_FILE) as f:
            return f.read().strip()
    except Exception:
        return ''


def _save_songs_path(path):
    try:
        with open(_SONGS_FILE, 'w') as f:
            f.write(path)
    except Exception:
        pass


import json as _json
_SETTINGS_FILE = os.path.join(updater.data_dir(), 'settings.json')


def _load_settings():
    try:
        with open(_SETTINGS_FILE, encoding='utf-8') as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_settings(data):
    try:
        with open(_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            _json.dump(data, f)
    except Exception:
        pass


def _validate_folder(path):
    """Quick checks only, returns (ok: bool, message: str). Whether the folder
    has any songs is found by the library scan (a full walk, too slow for the
    UI thread on big libraries)."""
    if not path or not os.path.isdir(path):
        return False, 'That path does not exist or is not a folder.'

    # They picked an individual song folder (song.ini at the root level)
    if os.path.exists(os.path.join(path, 'song.ini')):
        return False, (
            'That looks like an individual song folder, not your Songs library.\n\n'
            'Please select the folder that contains all your song packs, '
            'not a folder inside one of them.')
    return True, ''


# shown when the library scan finds nothing
_NO_SONGS_MSG = (
    'No songs found in that folder.\n\n'
    'Make sure you\'re selecting your Clone Hero / YARG Songs folder.\n'
    'It should contain song folders with a song.ini file inside, '
    'or Rock Band CON files.')


@dataclass
class Song:
    filename : str          # abs path to song.ini
    folder   : str
    label    : str          # "Artist – Title" for display
    key      : str          # label.lower() for search
    has_video: bool
    res      : str          # "720p" / "1080p" / "480p" / "-" / "..."
    checked  : bool = False
    status   : str  = ''    # live status text during a run
    stag     : str  = ''    # colour tag: 'done'|'error'|'busy'|'dim'|''
    # songs inside a packed CON (YARG): video is <song_id>.webm next to the CON
    con      : str  = ''    # abs path to the CON file, '' for song folders
    song_id  : str  = ''
    artist   : str  = ''
    title    : str  = ''
    game_origin: str = ''   # songs.dta game_origin of CON songs (mvdb source)
    mv       : object = None   # music video database entry (mvdb), None = No Entry
    webm_queued: bool = False  # waiting for "Export N WebM(s)"
    dl_mode  : str  = 'auto'   # next download: 'auto' | 'db' (⬇) | 'search' (🔍)
    review   : object = None   # CON download waiting for review (conpack.load_review)
    rendering: str  = ''       # '' | 'queued' | 'running'
    # the finished video was made by BackstageHero: True / False, None = not
    # known yet (CON videos are checked in the background) or no video
    ours     : object = None
    auto_export: str = ''      # 'webm' / 'mp4': export when the download is ready

    @property
    def video(self):
        """The song's finished background video (may not exist yet)."""
        if self.con:
            return (conpack.existing_video(self.con, self.song_id)
                    or conpack.video_path(self.con, self.song_id))
        return os.path.join(self.folder, 'video.mp4')


def _open_in_file_manager(path):
    """Open a folder in the OS file manager (Windows/macOS/Linux)."""
    try:
        if sys.platform == 'win32':
            os.startfile(path)              # noqa: only exists on Windows
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        log.warning('Could not open folder: %s', path)


def _open_log():
    """Open log.txt in the default text viewer (the data folder if there's no log yet)."""
    for h in log.handlers:
        try:
            h.flush()
        except Exception:
            pass
    path = os.path.join(updater.data_dir(), 'log.txt')
    _open_in_file_manager(path if os.path.exists(path) else updater.data_dir())


def _review_status(review):
    """Status text for a CON song waiting for review (or exported, with its
    original download still in the cache)."""
    if not review:
        return ''
    if review.get('rendered'):
        return f'✔  {review["rendered"].upper()} exported  (original kept)'
    plan = review.get('autoedit')
    if plan:
        return f'◆  Review download (auto-edit, {len(plan.get("log") or [])} edits)'
    return '◆  Review download'


def _fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024


def _review_tag(review):
    if not review:
        return ''
    return 'done' if review.get('rendered') else 'warn'


class ScanCancelled(Exception):
    pass


def _scan_library(songs_folder, progress=None, stop=None):
    """Return (list[Song] sorted alphabetically, CON files, CON errors).
    progress(info) gets a dict with the running tally (stage, folders, songs,
    cons, con_done, con_total, path), at most ~10 times a second.
    stop: a threading.Event, set it to cancel (raises ScanCancelled)."""
    songs = []
    info = {'stage': 'folders', 'folders': 0, 'songs': 0, 'cons': 0,
            'con_done': 0, 'con_total': 0, 'path': ''}
    last = [0.0]

    def report(force=False):
        if stop is not None and stop.is_set():
            raise ScanCancelled()
        now = time.monotonic()
        if progress and (force or now - last[0] >= 0.1):
            last[0] = now
            progress(dict(info))

    def on_folder(dirpath):
        info['folders'] += 1
        info['path'] = dirpath
        report()

    for ini in conpack.find_song_inis(songs_folder, on_folder=on_folder):
        folder = os.path.dirname(ini)
        artist, title, stored, source = scan_song(folder)
        label = build_query(artist, title) or os.path.basename(folder)
        has_vid = os.path.exists(os.path.join(folder, 'video.mp4'))
        res = '-'
        if has_vid:
            res = stored if stored else '...'   # '...' = needs probing
        songs.append(Song(
            filename=ini, folder=folder,
            label=label, key=label.lower(),
            has_video=has_vid, res=res, artist=artist or '', title=title or '',
            ours=bool(source) if has_vid else None))
        info['songs'] = len(songs)
        report()

    # packed Rock Band CONs: one row per song inside, listed through Nautilus
    # second walk, same folders: only the path moves (folders stays the total)
    info.update(stage='cons', path='')
    report(True)

    def on_con_folder(dirpath):
        info['path'] = dirpath
        report()
    cons = conpack.find_con_files(songs_folder, on_folder=on_con_folder)
    info.update(cons=len(cons), path='')
    report(True)
    con_errors = {}
    if cons and conpack.nautilus_ready():
        info.update(stage='con_read', con_total=len(cons))
        report(True)

        def on_con(done, total):
            info.update(con_done=done, con_total=total,
                        path=cons[done] if done < len(cons) else '')
            report()
        listing, con_errors = conpack.list_songs(cons, progress=on_con)
        info.update(con_done=len(cons), path='')
        for con, entries in listing.items():
            pack = os.path.basename(con)
            for e in entries:
                if not e['artist'] and e.get('game_origin') == 'beatles':
                    # The Beatles: Rock Band customs leave the artist out
                    e = dict(e, artist='The Beatles')
                label = (build_query(e['artist'], e['name']) or e['id']) + f'   [{pack}]'
                has_vid = conpack.existing_video(con, e['id']) is not None
                review = conpack.load_review(con, e['id'])
                songs.append(Song(
                    filename=con, folder=os.path.dirname(con),
                    label=label, key=label.lower(),
                    has_video=has_vid, res='...' if has_vid else '-',
                    con=con, song_id=e['id'], artist=e['artist'], title=e['name'],
                    game_origin=e.get('game_origin', ''),
                    review=review,
                    status=_review_status(review),
                    stag=_review_tag(review)))
        for con, err in con_errors.items():
            log.warning('Could not read %s: %s', con, err)
    songs.sort(key=lambda s: s.key)
    return songs, cons, con_errors


class SyncEditor(ctk.CTkToplevel):
    """Manual video-offset editor.

    When libmpv is available the video plays embedded in this window and the
    slider shifts the audio against it live, with no respawn. Otherwise it
    falls back to launching ffplay for a one-shot preview."""

    _MS_MIN = -30_000
    _MS_MAX =  90_000

    def __init__(self, parent, song: Song, on_save=None):
        super().__init__(parent)
        self._song      = song
        self._on_save   = on_save
        self._player    = None    # mpv handle (embedded preview)
        self._proc      = None    # ffplay process (fallback)
        self._proc_aux  = None    # ffmpeg feeder (fallback)
        self._tmp_mux   = None    # temp muxed video+audio file for the preview
        self._embedded  = mpvlib is not None and ffmpegAvailable

        # SW_SHOWNOACTIVATE: ffplay (fallback) opens without stealing focus
        self._si = subprocess.STARTUPINFO()
        self._si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        self._si.wShowWindow = 4

        self._ms    = tk.IntVar(value=self._read_offset())
        self._share = tk.BooleanVar(value=True)

        self.title('Sync Editor')
        self.geometry('640x760' if self._embedded else '500x470')
        self.resizable(False, False)
        self.configure(fg_color=_BG)
        self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self._close)
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        self._build()
        self._refresh()
        self.after(50, self._center)
        if self._embedded:
            self.after(120, self._start_embedded)
        elif ffplayPath:
            self.after(200, self._launch_preview)

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=60)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text=self._song.label,
                     font=_font(14, weight='bold'),
                     text_color=_TEXT, anchor='w').pack(
            side='left', padx=18, fill='y')
        res = self._song.res if self._song.res not in ('-', '...', '') else '?'
        ctk.CTkLabel(hdr, text=res,
                     font=_font(11),
                     text_color=_SUBTEXT).pack(side='right', padx=18)

        # Embedded video surface (mpv renders into this frame)
        self._video_frame = None
        if self._embedded:
            holder = ctk.CTkFrame(self, fg_color='#000000', corner_radius=8,
                                  width=600, height=338)
            holder.pack(padx=20, pady=(16, 0))
            holder.pack_propagate(False)
            # plain tk.Frame gives a stable native window id for mpv's wid
            self._video_frame = tk.Frame(holder, bg='#000000',
                                         width=600, height=338,
                                         highlightthickness=0, bd=0)
            self._video_frame.pack(fill='both', expand=True)

        # Offset readout card
        card = ctk.CTkFrame(self, fg_color='#252540', corner_radius=10)
        card.pack(fill='x', padx=20, pady=(18, 0))
        top_row = ctk.CTkFrame(card, fg_color='transparent')
        top_row.pack(fill='x', padx=16, pady=(12, 0))
        self._ms_lbl = ctk.CTkLabel(
            top_row, text='',
            font=_font(30, weight='bold'), text_color=_BLUE)
        self._ms_lbl.pack(side='left')
        # live indicator, only when ffplay is around
        if ffplayPath:
            self._live_lbl = ctk.CTkLabel(
                top_row, text='',
                font=_font(10), text_color=_SUBTEXT)
            self._live_lbl.pack(side='right', padx=4, pady=(8, 0))
        else:
            self._live_lbl = None
        self._desc_lbl = ctk.CTkLabel(
            card, text='',
            font=_font(11), text_color=_SUBTEXT)
        self._desc_lbl.pack(pady=(2, 12))

        # Slider
        sf = ctk.CTkFrame(self, fg_color='transparent')
        sf.pack(fill='x', padx=20, pady=(14, 0))
        ctk.CTkLabel(sf, text='-30s', font=_font(10),
                     text_color=_SUBTEXT).pack(side='left')
        ctk.CTkLabel(sf, text='+90s', font=_font(10),
                     text_color=_SUBTEXT).pack(side='right')
        self._slider = ctk.CTkSlider(
            sf, from_=self._MS_MIN, to=self._MS_MAX,
            command=self._on_slider, height=16,
            button_color=_BLUE, button_hover_color='#7aaef8',
            progress_color=_BLUE)
        self._slider.set(self._ms.get())
        self._slider.pack(fill='x', padx=8)

        # Fine-tune buttons
        bf = ctk.CTkFrame(self, fg_color='transparent')
        bf.pack(fill='x', padx=20, pady=(10, 0))
        _b = dict(height=26, corner_radius=6,
                  fg_color='#2a2a3e', hover_color='#383858',
                  text_color=_TEXT, font=_font(10))
        for lbl, delta in (('-5s', -5000), ('-1s', -1000),
                           ('-100ms', -100), ('-10ms', -10),
                           ('+10ms', 10),   ('+100ms', 100),
                           ('+1s', 1000),   ('+5s', 5000)):
            ctk.CTkButton(bf, text=lbl, width=54,
                          command=lambda d=delta: self._nudge(d), **_b).pack(
                side='left', padx=2)

        # Transport row
        prev_row = ctk.CTkFrame(self, fg_color='transparent')
        prev_row.pack(fill='x', padx=20, pady=(10, 0))
        if self._embedded:
            self._play_btn = ctk.CTkButton(
                prev_row, text='⏸  Pause', width=100, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=_font(11),
                command=self._toggle_play)
            self._play_btn.pack(side='left')
            ctk.CTkButton(
                prev_row, text='↻  Restart', width=100, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=_font(11),
                command=self._restart_at_song).pack(side='left', padx=(8, 0))
            ctk.CTkLabel(prev_row,
                         text='Drag the slider, sync updates live as it plays.',
                         font=_font(10),
                         text_color=_SUBTEXT).pack(side='right')
        elif ffplayPath:
            ctk.CTkButton(
                prev_row, text='▶  Preview', width=110, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=_font(11),
                command=self._launch_preview).pack(side='left')
        else:
            ctk.CTkLabel(prev_row, text='Player not bundled. Adjust the offset and Save.',
                         font=_font(10),
                         text_color=_SUBTEXT).pack(side='left')

        # Divider
        ctk.CTkFrame(self, fg_color=_BORDER, height=1).pack(
            fill='x', padx=20, pady=14)

        # Share
        shr = ctk.CTkFrame(self, fg_color='transparent')
        shr.pack(fill='x', padx=20)
        ctk.CTkCheckBox(shr, text='Share with community',
                        variable=self._share,
                        font=_font(12), text_color=_TEXT,
                        checkbox_width=18, checkbox_height=18,
                        checkmark_color=_BG, fg_color=_BLUE,
                        hover_color='#7aaef8').pack(anchor='w')
        ctk.CTkLabel(shr,
                     text='Once enough users confirm the same offset it becomes\n'
                          'the default for this chart, no fingerprinting needed.',
                     font=_font(10), text_color=_SUBTEXT,
                     justify='left', anchor='w').pack(anchor='w', pady=(4, 0))

        # Cancel / Save
        foot = ctk.CTkFrame(self, fg_color='transparent')
        foot.pack(fill='x', padx=20, pady=(14, 20))
        ctk.CTkButton(foot, text='Cancel', width=110,
                      fg_color='transparent', border_width=1,
                      border_color=_BORDER, hover_color='#30304a',
                      text_color=_SUBTEXT, font=_font(12),
                      command=self._close).pack(side='left')
        ctk.CTkButton(foot, text='Save', width=110,
                      fg_color=_BLUE, hover_color='#7aaef8',
                      text_color='#11111b',
                      font=_font(13, weight='bold'),
                      command=self._save).pack(side='right')

    def _read_offset(self):
        path = os.path.join(self._song.folder, 'song.ini')
        try:
            with open(path, encoding='utf-8-sig', errors='replace') as f:
                for line in f:
                    if '=' in line:
                        k, _, v = line.partition('=')
                        if k.strip().lower() == 'video_start_time':
                            val = v.strip()
                            if val.lstrip('-').isdigit():
                                return int(val)
        except Exception:
            pass
        return DEFAULT_START_TIME

    def _refresh(self):
        ms = self._ms.get()
        sign = '+' if ms > 0 else ''
        self._ms_lbl.configure(text=f'{sign}{ms:,} ms' if ms != 0 else '0 ms')
        s = abs(ms) / 1000.0
        if ms < -50:
            desc = f'Video has a {s:.1f}s intro before the song starts'
        elif ms > 50:
            desc = f'Song plays {s:.1f}s before the video starts'
        else:
            desc = 'Video and song start together'
        self._desc_lbl.configure(text=desc)

    def _on_slider(self, value):
        self._ms.set(int(round(value)))
        self._refresh()
        self._apply_live_delay()

    def _nudge(self, delta):
        new = max(self._MS_MIN, min(self._MS_MAX, self._ms.get() + delta))
        self._ms.set(new)
        self._slider.set(new)
        self._refresh()
        self._apply_live_delay()

    def _audio_delay(self):
        """mpv audio-delay (seconds) for the current offset. A negative
        video_start_time means the video has intro before the song, so the chart
        audio comes in that much later, which is a positive delay."""
        return -self._ms.get() / 1000.0

    def _song_start_pos(self):
        """Where playback / Restart begins: a couple seconds before the spot
        where the song lines up with the video."""
        return max(0.0, (-self._ms.get() / 1000.0) - 2.0)

    def _find_stems(self):
        """Every audio stem in the song folder. Clone Hero splits a song into
        stems (song/guitar/bass/drums/vocals/...) and plays them all together; on
        its own a single stem has gaps where the others carry the song, so the
        preview mixes the lot or you'd hear it cut in and out. Same rule the
        fingerprinter uses so the preview matches what gets analysed."""
        if audiosync is not None:
            return audiosync.chart_stems(self._song.folder)
        # if audiosync didn't import, same extensions/exclusions by hand
        folder = self._song.folder
        stems = []
        try:
            for f in sorted(os.listdir(folder)):
                base, ext = os.path.splitext(f.lower())
                if ext in ('.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac') \
                        and base not in ('preview', 'crowd'):
                    stems.append(os.path.join(folder, f))
        except Exception:
            pass
        return stems

    # ---- embedded mpv preview --------------------------------------------

    def _build_preview_source(self, video):
        """Combine the silent video with all the chart's stems mixed down into one
        temp file, so mpv just plays a single file with the full song. The mix is
        re-encoded to PCM, the video is copied. No stems or no ffmpeg and it falls
        back to the silent video."""
        stems = self._find_stems()
        if not (stems and ffmpegAvailable):
            return video
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               f'bh_preview_{os.getpid()}_{id(self) & 0xffffff}.mkv')
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', video]
            for s in stems:
                cmd += ['-i', s]
            n = len(stems)
            if n == 1:
                cmd += ['-map', '0:v:0', '-map', '1:a:0']
            else:
                # sum the stems. normalize=0 keeps their levels, so you get the
                # original mix back instead of everything scaled down by count.
                labels = ''.join(f'[{i + 1}:a]' for i in range(n))
                cmd += ['-filter_complex',
                        f'{labels}amix=inputs={n}:normalize=0:duration=longest[a]',
                        '-map', '0:v:0', '-map', '[a]']
            cmd += ['-c:v', 'copy', '-c:a', 'pcm_s16le', '-y', tmp]
            subprocess.run(cmd, check=True, creationflags=NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                self._tmp_mux = tmp
                return tmp
        except Exception:
            pass
        return video

    def _start_embedded(self):
        video = os.path.join(self._song.folder, 'video.mp4')
        if not (self._video_frame and os.path.exists(video)):
            self._embedded = False
            return
        # mixing the stems takes a sec, so do it off the UI thread and start
        # playback back on the main thread once the file's ready
        threading.Thread(target=self._prepare_and_play,
                         args=(video,), daemon=True).start()

    def _prepare_and_play(self, video):
        source = self._build_preview_source(video)
        self.after(0, lambda: self._play_source(source))

    def _play_source(self, source):
        if not self._video_frame or not self._video_frame.winfo_exists():
            return   # editor closed while preparing
        try:
            wid = int(self._video_frame.winfo_id())
            _kw = dict(
                wid=str(wid), vo='gpu', osc=False, keep_open='yes',
                force_media_title=f'{self._song.label} (BackstageHero preview)',
                log_handler=lambda *a: None)
            _dbg = os.environ.get('BH_MPV_LOG')   # diagnostics: path to mpv log file
            if _dbg:
                _kw['log_file'] = _dbg
                _kw['msg_level'] = 'all=v,ao=debug'
            self._player = mpvlib.MPV(**_kw)
            a = self._song_start_pos()
            self._player.audio_delay = self._audio_delay()
            self._player.loadfile(source, 'replace', start=str(a))
            self.after(250, self._post_load)
        except Exception:
            self._teardown_player()
            self._embedded = False

    def _post_load(self):
        """Make sure the preview is audible once the file is loaded. No looping:
        the clip plays through so the video gets past its intro into real
        footage, and Restart replays from the sync point on demand."""
        p = self._player
        if not p:
            return
        try:
            p.mute = False
            p.volume = 100
        except Exception:
            pass

    def _apply_live_delay(self):
        if self._player:
            try:
                self._player.audio_delay = self._audio_delay()
            except Exception:
                pass

    def _toggle_play(self):
        if not self._player:
            return
        try:
            paused = not bool(self._player.pause)
            self._player.pause = paused
            self._play_btn.configure(text='▶  Play' if paused else '⏸  Pause')
        except Exception:
            pass

    def _restart_at_song(self):
        if not self._player:
            return
        try:
            self._player.pause = False
            self._play_btn.configure(text='⏸  Pause')
            self._player.seek(self._song_start_pos(), reference='absolute')
        except Exception:
            pass

    def _teardown_player(self):
        if self._player:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        if self._tmp_mux:
            try:
                os.remove(self._tmp_mux)
            except Exception:
                pass
            self._tmp_mux = None

    # ---- ffplay fallback (no libmpv) -------------------------------------

    def _kill_preview(self):
        for p in (self._proc, self._proc_aux):
            if p and p.poll() is None:
                p.terminate()
        self._proc = self._proc_aux = None

    def _launch_preview(self):
        if not ffplayPath:
            return
        self._kill_preview()
        ms     = self._ms.get()
        v_seek = max(0.0, -ms / 1000.0)
        a_seek = max(0.0,  ms / 1000.0)
        video  = os.path.join(self._song.folder, 'video.mp4')
        if not os.path.exists(video):
            return
        stems = self._find_stems()
        ffplay_base = [ffplayPath, '-hide_banner',
                       '-x', '640', '-y', '360', '-autoexit',
                       '-window_title', f'Preview: {self._song.label}']
        if stems and ffmpegAvailable:
            # Mix all stems (so the audio doesn't drop out on guitar-only
            # sections) and copy the video, piped to ffplay.
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                   '-ss', f'{v_seek:.3f}', '-i', video]
            for s in stems:
                cmd += ['-ss', f'{a_seek:.3f}', '-i', s]
            n = len(stems)
            if n == 1:
                cmd += ['-map', '0:v', '-map', '1:a']
            else:
                labels = ''.join(f'[{i + 1}:a]' for i in range(n))
                cmd += ['-filter_complex',
                        f'{labels}amix=inputs={n}:normalize=0:duration=longest[a]',
                        '-map', '0:v', '-map', '[a]']
            cmd += ['-c:v', 'copy', '-shortest', '-f', 'nut', 'pipe:1']
            feeder = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            self._proc = subprocess.Popen(
                ffplay_base + ['-'], stdin=feeder.stdout,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW, startupinfo=self._si)
            feeder.stdout.close()
            self._proc_aux = feeder
        else:
            self._proc = subprocess.Popen(
                ffplay_base + ['-ss', f'{v_seek:.3f}', video],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW, startupinfo=self._si)
        if self._live_lbl:
            self._live_lbl.configure(text='● live', text_color=_GREEN)

    def _save(self):
        ms, share = self._ms.get(), self._share.get()
        self._close()
        if self._on_save:
            self._on_save(ms, share)

    def _close(self):
        self._teardown_player()
        self._kill_preview()
        self.grab_release()
        self.destroy()

    def _center(self):
        self.update_idletasks()
        pw = self.master.winfo_x() + self.master.winfo_width() // 2
        ph = self.master.winfo_y() + self.master.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f'+{pw - w // 2}+{ph - h // 2}')


class UpdateDialog(ctk.CTkToplevel):
    """Small modal window shown while the app updates itself: status line and a
    progress bar so the user always sees what the updater is doing."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title('Updating BackstageHero')
        self.geometry('420x150')
        self.resizable(False, False)
        self.configure(fg_color=_BG)
        self.transient(parent)
        # no close button, the updater runs this to the end itself
        self.protocol('WM_DELETE_WINDOW', lambda: None)
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        ctk.CTkLabel(self, text='Updating BackstageHero',
                     font=_font(15, weight='bold'),
                     text_color=_TEXT).pack(padx=24, pady=(22, 4), anchor='w')
        self._stat = ctk.CTkLabel(self, text='Preparing...',
                                  font=_font(12), text_color=_SUBTEXT)
        self._stat.pack(padx=24, anchor='w')
        self._bar = ctk.CTkProgressBar(self, width=372, height=14,
                                       progress_color=_BLUE)
        self._bar.set(0)
        self._bar.pack(padx=24, pady=(12, 0))
        self._bar.configure(mode='indeterminate')
        self._bar.start()
        self._detail = ctk.CTkLabel(self, text='',
                                    font=_font(10), text_color=_SUBTEXT)
        self._detail.pack(padx=24, pady=(6, 0), anchor='w')

        self.after(50, self._center)
        self.grab_set()

    def _center(self):
        self.update_idletasks()
        try:
            pw = self.master.winfo_x() + self.master.winfo_width() // 2
            ph = self.master.winfo_y() + self.master.winfo_height() // 2
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f'+{pw - w // 2}+{ph - h // 2}')
        except Exception:
            pass

    def _det(self):
        # Stop any running indeterminate animation before showing a real value.
        try:
            self._bar.stop()
        except Exception:
            pass
        self._bar.configure(mode='determinate')

    def update_stage(self, stage, **info):
        if not self.winfo_exists():
            return
        if stage == 'download':
            got, total = info.get('got', 0), info.get('total', 0)
            self._stat.configure(text='Downloading the new version...')
            if total:
                self._det()
                self._bar.set(got / total)
                self._detail.configure(
                    text=f'{got / 1048576:.1f} MB of {total / 1048576:.1f} MB')
        elif stage == 'verify':
            self._bar.configure(mode='indeterminate'); self._bar.start()
            self._stat.configure(text='Verifying the download...')
            self._detail.configure(text='')
        elif stage == 'install':
            self._stat.configure(text='Installing...')
        elif stage == 'restart':
            self._det(); self._bar.set(1.0)
            self._stat.configure(text='Done, restarting BackstageHero...')
            self._detail.configure(text='')
        elif stage == 'error':
            self._det(); self._bar.set(0)
            self._stat.configure(text='Update failed. Keeping the current version.',
                                 text_color=_RED)
            self._detail.configure(text=info.get('msg', ''))
            self.protocol('WM_DELETE_WINDOW', self.destroy)
            ctk.CTkButton(self, text='Close', width=80, command=self.destroy,
                          fg_color='#313244', hover_color='#414160').pack(pady=(8, 0))


class ScanWindow(ctk.CTkToplevel):
    """Modal window shown while the library is scanned, so a big Songs folder
    doesn't look like a freeze: what's being done, running counts, the folder
    being read, elapsed time, and Cancel."""

    _STAGES = {
        'folders':  'Looking for song folders...',
        'cons':     'Looking for Rock Band CON files...',
        'con_read': 'Reading the CON files...',
        'mvdb':     'Matching the music video database...',
        'table':    'Filling the song list...',
    }

    def __init__(self, app, path, on_cancel):
        super().__init__(app)
        self._on_cancel = on_cancel
        self._t0 = time.monotonic()
        self._job = None
        self._det = False
        self.title('Scanning library')
        self.configure(fg_color=_BG)
        self.geometry(f'{_fs(480)}x{_fs(250)}')
        self.resizable(False, False)
        self.transient(app)
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        shown = path if len(path) < 60 else '...' + path[-57:]
        ctk.CTkLabel(self, text='Scanning your library', font=_font(15, weight='bold'),
                     text_color=_TEXT).pack(padx=24, pady=(18, 0), anchor='w')
        ctk.CTkLabel(self, text=shown, font=_font(10),
                     text_color=_SUBTEXT).pack(padx=24, anchor='w')
        self._stage = ctk.CTkLabel(self, text=self._STAGES['folders'],
                                   font=_font(12), text_color=_TEXT)
        self._stage.pack(padx=24, pady=(10, 0), anchor='w')
        self._bar = ctk.CTkProgressBar(self, width=_fs(432), height=12,
                                       progress_color=_BLUE)
        self._bar.pack(padx=24, pady=(6, 0))
        self._bar.configure(mode='indeterminate')
        self._bar.start()
        self._counts = ctk.CTkLabel(self, text='', font=_font(12),
                                    text_color=_TEXT, justify='left')
        self._counts.pack(padx=24, pady=(8, 0), anchor='w')
        self._where = ctk.CTkLabel(self, text='', font=_font(10),
                                   text_color=_SUBTEXT)
        self._where.pack(padx=24, anchor='w')
        row = ctk.CTkFrame(self, fg_color='transparent')
        row.pack(fill='x', padx=24, pady=(8, 12), side='bottom')
        self._time = ctk.CTkLabel(row, text='', font=_font(10), text_color=_SUBTEXT)
        self._time.pack(side='left')
        self._cancel_btn = ctk.CTkButton(row, text='Cancel', width=_fs(90),
                                         fg_color='#313244', hover_color='#414160',
                                         command=self._cancel)
        self._cancel_btn.pack(side='right')

        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.bind('<Escape>', lambda e: self._cancel())
        self.update_info({'stage': 'folders', 'folders': 0, 'songs': 0,
                          'cons': 0, 'con_done': 0, 'con_total': 0, 'path': ''})
        self.after(50, self._center)
        self.after(60, self._grab)
        self._tick()

    def _grab(self):
        try:
            self.grab_set()
        except tk.TclError:      # not viewable yet
            self.after(60, self._grab)

    def _center(self):
        self.update_idletasks()
        try:
            m = self.master
            x = m.winfo_x() + m.winfo_width() // 2 - self.winfo_width() // 2
            y = m.winfo_y() + m.winfo_height() // 2 - self.winfo_height() // 2
            self.geometry(f'+{max(0, x)}+{max(0, y)}')
        except Exception:
            pass

    def _tick(self):
        s = int(time.monotonic() - self._t0)
        self._time.configure(text=f'{s // 60}:{s % 60:02d} elapsed')
        self._job = self.after(1000, self._tick)

    def update_info(self, info):
        if not self.winfo_exists():
            return
        stage = info.get('stage', '')
        text = self._STAGES.get(stage, '')
        lines = [f'Folders scanned:  {info.get("folders", 0):,}',
                 f'Songs found:  {info.get("songs", 0):,}']
        if stage != 'folders':
            lines.append(f'CON files found:  {info.get("cons", 0):,}')
        if stage == 'con_read' and info.get('con_total'):
            done, total = info.get('con_done', 0), info['con_total']
            text = f'Reading the CON files...  {done:,} / {total:,}'
            if not self._det:
                self._det = True
                self._bar.stop()
                self._bar.configure(mode='determinate')
            self._bar.set(done / total)
        elif self._det:          # back to an open-ended stage
            self._det = False
            self._bar.configure(mode='indeterminate')
            self._bar.start()
        self._stage.configure(text=text)
        self._counts.configure(text='\n'.join(lines))
        p = info.get('path') or ''
        if p:
            p = os.path.normpath(p)
            p = p if len(p) < 70 else '...' + p[-67:]
        self._where.configure(text=p)
        if stage in ('mvdb', 'table'):
            self._cancel_btn.configure(state='disabled')

    def _cancel(self):
        if str(self._cancel_btn.cget('state')) == 'disabled':
            return
        self._cancel_btn.configure(state='disabled', text='Stopping...')
        self._stage.configure(text='Stopping the scan...')
        self._on_cancel()

    def close(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        try:
            self._bar.stop()
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class CollageViewer(ctk.CTkToplevel):
    """Gallery for checking a download: the collage first, then each frame used
    in it, in order. Left/Right (or A/D, mouse wheel) to move, Home/End to
    jump, Esc to close."""

    def __init__(self, parent, title, collage, frames):
        super().__init__(parent)
        from PIL import Image
        self._Image = Image
        self.title(f'Collage  ·  {title}')
        self.configure(fg_color=_BG)
        self.geometry('1100x760')
        self.minsize(480, 360)
        # (path, caption)
        self._items = [(collage, f'Collage  ·  {len(frames)} frames')] + [
            (p, f'Frame {k}/{len(frames)}  ·  {int(t // 60)}:{t % 60:05.2f}')
            for k, (p, t) in enumerate(frames, 1)]
        self._idx = 0
        self._photo = None

        self._img = tk.Label(self, bg=_BG, bd=0)
        self._img.pack(fill='both', expand=True, padx=8, pady=(8, 0))
        bar = ctk.CTkFrame(self, fg_color='transparent')
        bar.pack(fill='x', padx=8, pady=8)
        ctk.CTkButton(bar, text='◀', width=44, command=lambda: self._go(-1)).pack(side='left')
        ctk.CTkButton(bar, text='▶', width=44, command=lambda: self._go(1)).pack(side='left', padx=(6, 0))
        self._cap = ctk.CTkLabel(bar, text='', text_color=_TEXT)
        self._cap.pack(side='left', padx=12)
        ctk.CTkLabel(bar, text='← →  browse    Home/End  first/last    Esc  close',
                     text_color=_SUBTEXT).pack(side='right')

        for key, step in (('<Left>', -1), ('<Right>', 1), ('<a>', -1), ('<d>', 1),
                          ('<Prior>', -1), ('<Next>', 1)):
            self.bind(key, lambda e, st=step: self._go(st))
        self.bind('<Home>', lambda e: self._jump(0))
        self.bind('<End>', lambda e: self._jump(len(self._items) - 1))
        self.bind('<Escape>', lambda e: self.destroy())
        self.bind('<MouseWheel>', lambda e: self._go(-1 if e.delta > 0 else 1))
        self._img.bind('<Configure>', lambda e: self._show())
        self.after(50, self._focus)

    def _focus(self):
        self.lift()
        self.focus_force()

    def _go(self, step):
        self._jump((self._idx + step) % len(self._items))

    def _jump(self, idx):
        self._idx = idx
        self._show()

    def _show(self):
        from PIL import ImageTk
        path, caption = self._items[self._idx]
        w = max(50, self._img.winfo_width())
        h = max(50, self._img.winfo_height())
        try:
            im = self._Image.open(path)
            im.thumbnail((w, h), self._Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(im)
            self._img.configure(image=self._photo)
        except Exception as e:
            self._img.configure(image='', text=str(e), fg=_RED)
        self._cap.configure(text=f'{self._idx + 1}/{len(self._items)}   {caption}')


class VideoLinksWindow(ctk.CTkToplevel):
    """The "@" action: every video type the music video database knows for a
    song, with its YouTube link. Links open in the browser; Edit / Set link
    change them. Changes are saved in the local mvdb layer and are what
    "Missed entries" submits."""

    _TYPE_HELP = {
        'official':  'The artist\'s official music video',
        'alternate': 'Live performance, visualizer or live visual that matches the album audio',
        'fan':       'Fan-made video or fan edit synced to the album audio',
        'meme':      'TV / movie / game clip or meme set to the song',
    }

    _open = None    # only one at a time

    @classmethod
    def show(cls, app, song):
        """Open the window for a song: the one already open comes to the front
        if it's the same song, otherwise it's closed and replaced."""
        win = cls._open
        if win is not None:
            try:
                alive = bool(win.winfo_exists())
            except Exception:
                alive = False
            if alive and win._s is song:
                win.deiconify()
                win.lift()
                win.focus_force()
                return win
            if alive:
                win.destroy()
        cls._open = cls(app, song)
        return cls._open

    def __init__(self, app, song):
        super().__init__(app)
        self._app, self._s = app, song
        self.title(f'Video links  ·  {song.label}')
        self.configure(fg_color=_BG)
        self.geometry(f'{_fs(720)}x{_fs(470)}')
        self.minsize(_fs(560), _fs(380))

        head = ctk.CTkFrame(self, fg_color='transparent')
        head.pack(fill='x', padx=16, pady=(14, 4))
        ctk.CTkLabel(head, text=song.label, font=_font(15, weight='bold'),
                     text_color=_TEXT, anchor='w').pack(side='left', fill='x', expand=True)
        ctk.CTkButton(head, text='🔍  Search on YouTube', width=_fs(170),
                      command=self._search).pack(side='right')
        self._info = ctk.CTkLabel(self, text='', font=_font(11), text_color=_SUBTEXT,
                                  anchor='w', justify='left')
        self._info.pack(fill='x', padx=16)

        self._rows = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=8)
        self._rows.pack(fill='both', expand=True, padx=16, pady=10)
        self._rows.grid_columnconfigure(2, weight=1)

        foot = ctk.CTkFrame(self, fg_color='transparent')
        foot.pack(fill='x', padx=16, pady=(0, 12))
        ctk.CTkLabel(foot, text='Links you set or fix are saved on this PC and go into '
                                '⇪ Missed entries, to share them.',
                     font=_font(11), text_color=_SUBTEXT).pack(side='left')
        ctk.CTkButton(foot, text='Close', width=_fs(90), fg_color='#313244',
                      hover_color='#414160', command=self.destroy).pack(side='right')

        self._build_rows()
        self.bind('<Escape>', lambda e: self.destroy())
        self.after(50, lambda: (self.lift(), self.focus_force()))

    # what to search / show

    def _query(self):
        s = self._s
        return (build_query(s.artist, s.title) or s.label).strip()

    def _search(self):
        import webbrowser
        from urllib.parse import quote_plus
        webbrowser.open('https://www.youtube.com/results?search_query='
                        + quote_plus(self._query() + ' official video'))

    def _build_rows(self):
        for w in self._rows.winfo_children():
            w.destroy()
        s = self._s
        if s.mv is None:
            self._info.configure(text='No Entry: this song isn\'t in the database yet. '
                                      'Downloads use the automatic YouTube search.')
        else:
            src = self._app._mv_source(s)
            con = s.mv.get('con_id') or '-'
            self._info.configure(text=f'Source: {mvdb.SOURCES.get(src, src)} ({src})'
                                      f'    CON ID: {con}')
        for r, (vtype, v) in enumerate(mvdb.types_of(s.mv)):
            pad = dict(padx=10, pady=(10 if r == 0 else 4, 4))
            vid = (v or {}).get('youtube') or ''
            fav = v is not None and bool(v.get('directors_choice'))
            star = ctk.CTkButton(
                self._rows, text='★' if fav else '☆', width=_fs(28), height=_fs(28),
                font=_font(16), fg_color='transparent', hover_color='#414160',
                text_color='#f9e2af' if fav else _SUBTEXT,
                text_color_disabled='#45475a',
                state='normal' if (vid or fav) else 'disabled',
                command=lambda t=vtype, f=fav: self._favorite(None if f else t))
            star.grid(row=2 * r, column=0, sticky='w', padx=(10, 0), pady=pad['pady'])
            name = ctk.CTkLabel(self._rows, text=vtype.capitalize(), width=_fs(90),
                                font=_font(13, weight='bold'), text_color=_TEXT, anchor='w')
            name.grid(row=2 * r, column=1, sticky='w', padx=(4, 10), pady=pad['pady'])
            if vid:
                url = 'https://www.youtube.com/watch?v=' + vid
                text = url
                if v.get('title'):
                    text += '   ·   ' + v['title']
                link = ctk.CTkLabel(self._rows, text=text, text_color=_BLUE,
                                    font=_font(12, underline=True), anchor='w', cursor='hand2')
                link.bind('<Button-1>', lambda e, u=url: __import__('webbrowser').open(u))
            else:
                text = 'no link'
                if v is not None:
                    text += ' (the database has this type, but not the video)'
                link = ctk.CTkLabel(self._rows, text=text, text_color=_SUBTEXT,
                                    font=_font(12), anchor='w')
            link.grid(row=2 * r, column=2, sticky='we', **pad)
            ctk.CTkButton(self._rows, text='✎  Edit' if vid else '+  Set link', width=_fs(100),
                          fg_color='#313244' if vid else None,
                          hover_color='#414160' if vid else None,
                          command=lambda t=vtype, old=vid: self._edit(t, old)
                          ).grid(row=2 * r, column=3, sticky='e', **pad)
            if vid:
                ctk.CTkButton(self._rows, text='🗑', width=_fs(36), fg_color='#313244',
                              hover_color='#8b2c3d',
                              command=lambda t=vtype, old=vid: self._clear(t, old)
                              ).grid(row=2 * r, column=4, sticky='e', padx=(0, 10),
                                     pady=pad['pady'])
            details = [self._TYPE_HELP[vtype]]
            if vtype == mvdb.chosen_type(s.mv):
                details.insert(0, '● in use')
            if fav:
                details.insert(0, '★ director\'s choice')
            if v is not None:
                if v.get('autoedit'):
                    details.append(f'auto-edit, {len(v["autoedit"].get("log") or [])} edits')
                elif v.get('offset_ms') is not None:
                    details.append(f'offset {v["offset_ms"]} ms')
                if v.get('edited'):
                    details.append('changed by you')
                credit = v.get('sync_credit') or v.get('submitted_by')
                if credit:
                    details.append(f'by {credit}')
            ctk.CTkLabel(self._rows, text='   ·   '.join(details), font=_font(10),
                         text_color=_SUBTEXT, anchor='w'
                         ).grid(row=2 * r + 1, column=2, columnspan=3, sticky='w', padx=10)

    def _favorite(self, vtype):
        """☆ / ★: make a type the director's choice (the one used by default),
        or clear it. Saved on this PC only, never in Missed entries."""
        s = self._s
        if s.mv is None:
            return
        mvdb.set_favorite(self._app._mvdb, self._app._mv_source(s),
                          {'con_id': s.mv.get('con_id', ''), 'artist': s.mv.get('artist', ''),
                           'title': s.mv.get('title', '')}, vtype)
        s.mv.pop('chosen', None)
        self._app._update_row(s)
        self._build_rows()

    def _song_ref(self):
        s = self._s
        return {'con_id': s.song_id if s.con else (s.mv or {}).get('con_id', ''),
                'artist': (s.mv or {}).get('artist') or s.artist,
                'title': (s.mv or {}).get('title') or s.title or s.label}

    def _clear(self, vtype, old):
        """🗑: remove a bad link of one type (saved on this PC)."""
        if not messagebox.askyesno(
                'Video links',
                f'Remove the {vtype} link of {self._s.label}?\n\n'
                f'https://www.youtube.com/watch?v={old}\n\n'
                'Set link adds a new one.', parent=self):
            return
        s = self._s
        ref = self._song_ref()
        db = self._app._mvdb
        mvdb.clear_link(db, self._app._mv_source(s), ref, vtype, old)
        s.mv = mvdb.resolve(db, ref['con_id'], ref['artist'], ref['title'])
        self._app._update_row(s)
        self._build_rows()

    def _edit(self, vtype, old):
        dlg = ctk.CTkInputDialog(
            title=f'{vtype.capitalize()} video',
            text=f'YouTube link or video ID for the {vtype} video of\n{self._s.label}:')
        raw = (dlg.get_input() or '').strip()
        if not raw:
            return
        vid = mvdb.youtube_id(raw)
        if not vid:
            messagebox.showerror('Video links', f'That isn\'t a YouTube link:\n{raw}', parent=self)
            return
        if vid == old:
            return
        s = self._s
        ref = {'con_id': s.song_id if s.con else (s.mv or {}).get('con_id', ''),
               'artist': (s.mv or {}).get('artist') or s.artist,
               'title': (s.mv or {}).get('title') or s.title or s.label}
        db = self._app._mvdb
        mvdb.set_link(db, self._app._mv_source(s), ref, vtype, vid, old or None)
        s.mv = mvdb.resolve(db, ref['con_id'], ref['artist'], ref['title'])
        self._app._update_row(s)
        self._build_rows()


class ShutdownCountdown(ctk.CTkToplevel):
    """Shown when the WebM exports finish with "Shut down when done" on:
    counts down, then shuts Windows down. Cancel stops it."""

    def __init__(self, app, seconds=60):
        super().__init__(app)
        self._app, self._left, self._job = app, seconds, None
        self.title('Shutting down')
        self.configure(fg_color=_BG)
        self.geometry(f'{_fs(420)}x{_fs(170)}')
        self.resizable(False, False)
        self.attributes('-topmost', True)
        ctk.CTkLabel(self, text='All WebM exports are done.', font=_font(14, weight='bold'),
                     text_color=_TEXT).pack(pady=(18, 4))
        self._lbl = ctk.CTkLabel(self, text='', font=_font(13), text_color=_YELLOW)
        self._lbl.pack(pady=4)
        row = ctk.CTkFrame(self, fg_color='transparent')
        row.pack(pady=12)
        ctk.CTkButton(row, text='Cancel', width=_fs(120), command=self._cancel
                      ).pack(side='left', padx=6)
        ctk.CTkButton(row, text='Shut down now', width=_fs(140), fg_color='#4a1a2a',
                      hover_color='#6a2a3e', text_color=_RED, command=self._shutdown
                      ).pack(side='left', padx=6)
        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.bind('<Escape>', lambda e: self._cancel())
        self.after(50, lambda: (self.lift(), self.focus_force()))
        self._tick()

    def _tick(self):
        self._lbl.configure(text=f'The PC shuts down in {self._left} s')
        if self._left <= 0:
            self._shutdown()
            return
        self._left -= 1
        self._job = self.after(1000, self._tick)

    def _cancel(self):
        if self._job:
            self.after_cancel(self._job)
        self._app._shutdown_var.set(False)
        self._app._status_lbl.configure(text='Shutdown cancelled')
        self.destroy()

    def _shutdown(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        log.info('Shutting down after the WebM exports')
        try:
            if sys.platform == 'win32':
                subprocess.run(['shutdown', '/s', '/t', '0'], creationflags=NO_WINDOW)
            else:
                subprocess.run(['shutdown', '-h', 'now'])
        except Exception as e:
            messagebox.showerror('Shut down', f'Could not shut down:\n{e}', parent=self)
        self.destroy()


class App(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title(f'BackstageHero  v{__version__}')
        self.geometry('1020x680')
        self.minsize(900, 520)
        self.configure(fg_color=_BG)

        # Window + taskbar icon
        try:
            ico = _asset_path('icon.ico')
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        # State
        self._songs       : list[Song]       = []
        self._mvdb        : dict             = {}   # music video database (+ your edits)
        self._yt_titles   : dict             = {}   # YouTube ID -> title (cache)
        self._yt_pending  : set              = set()
        self._yt_jobs     = queue.Queue()
        self._yt_thread   = None
        self._yt_save_after = None
        self._load_yt_titles()
        self._cache_bytes = 0
        self._webm_batch = False
        self._filtered    : list[Song]       = []
        self._iid_map     : dict[str, Song]  = {}
        self._iid_of      : dict[int, str]   = {}   # id(Song) -> tree iid (O(1) row updates)
        self._sort_col    : str  = 'label'
        self._sort_asc    : bool = True
        self._filter_mode : str  = 'missing'
        self._running     : bool = False
        self._polling     : bool = False
        self._stop_evt    = threading.Event()
        self._queue       : queue.Queue = queue.Queue()
        self._songs_folder: str  = ''
        self._pending_update = None      # (version, asset, sha) deferred during a run
        self._search_after = None        # debounce handle for the search box
        self._settings    = _load_settings()
        resolver_client.set_sharing(self._settings.get('share_matches', True))
        conpack.set_nautilus_path(self._settings.get('nautilus_path'))
        self._nautilus_asked = False     # ask for Nautilus.exe once per session
        self._scan_win = None            # ScanWindow while a library scan runs
        self._action_hint  = ''
        self._render_jobs  : queue.Queue = queue.Queue()
        self._render_thread = None
        self._pending      : list = []   # (song, replace, resync) waiting for the download thread
        self._dl_lock      = threading.Lock()
        self._dl_alive     = False
        self._run_quality  = None
        self._sync_ready  : bool = (
            ffmpegAvailable and audiosync is not None
            and audiosync.is_available())

        self._build_ui()
        # Poll the queue from the start so update/ping results are handled even
        # before a library is loaded.
        self._polling = True
        self.after(200, self._poll_queue)
        self.after(120, self._startup)
        self.after(500, self._start_update_check)

    def _build_ui(self):
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=52)
        hdr.grid(row=0, column=0, sticky='ew')
        hdr.grid_propagate(False)
        # col 1 is a spacer; col 2 holds the folder path and expands
        hdr.grid_columnconfigure(1, weight=0)
        hdr.grid_columnconfigure(2, weight=1)

        logo_png = _asset_path('logo.png')
        if os.path.exists(logo_png):
            from PIL import Image as _PILImage
            _img = _PILImage.open(logo_png)
            _h = 30
            _w = int(_img.width * _h / _img.height)
            ctk.CTkLabel(hdr, text='',
                         image=ctk.CTkImage(_img, size=(_w, _h))).grid(
                row=0, column=0, padx=(16, 6), pady=11)
        else:
            ctk.CTkLabel(hdr,
                         text='BackstageHero',
                         font=_font(17, weight='bold'),
                         text_color=_TEXT).grid(row=0, column=0, padx=(16, 6), pady=14)
        ctk.CTkLabel(hdr,
                     text=f'v{__version__}',
                     font=_font(11),
                     text_color=_SUBTEXT).grid(row=0, column=1, pady=14,
                                               padx=(0, 16), sticky='w')

        folder_row = ctk.CTkFrame(hdr, fg_color='transparent')
        folder_row.grid(row=0, column=2, padx=(0, 12), sticky='e')
        folder_row.grid_columnconfigure(0, weight=1)
        self._folder_lbl = ctk.CTkLabel(
            folder_row, text='No folder selected',
            font=_font(11), text_color=_SUBTEXT, anchor='e')
        self._folder_lbl.grid(row=0, column=0, padx=(0, 10), sticky='e')
        ctk.CTkButton(folder_row, text='Show log',
                      width=80, height=28, font=_font(11),
                      fg_color=_SURFACE, text_color=_TEXT,
                      border_color=_BORDER, border_width=1, hover_color='#30304a',
                      command=_open_log).grid(row=0, column=1, padx=(0, 6))
        ctk.CTkButton(folder_row, text='Open folder',
                      width=95, height=28, font=_font(11),
                      fg_color=_SURFACE, text_color=_TEXT,
                      border_color=_BORDER, border_width=1, hover_color='#30304a',
                      command=self._open_songs_folder).grid(row=0, column=2, padx=(0, 6))
        ctk.CTkButton(folder_row, text='Change folder',
                      width=115, height=28, font=_font(11),
                      command=self._pick_folder).grid(row=0, column=3)

        # Filter / search bar
        fbar = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=50)
        fbar.grid(row=1, column=0, sticky='ew', pady=(1, 0))
        fbar.grid_propagate(False)
        # Column 3 is a spacer that absorbs leftover space;
        # column 4 is the search box pinned to the right.
        fbar.grid_columnconfigure(3, weight=1)

        _btn = dict(height=30, width=130, font=_font(12),
                    corner_radius=6)

        self._btn_missing = ctk.CTkButton(
            fbar, text='Missing video',
            fg_color=_BLUE, text_color='#11111b', hover_color='#7aaef8',
            command=lambda: self._set_filter('missing'), **_btn)
        self._btn_missing.grid(row=0, column=0, padx=(12, 4), pady=10)

        self._btn_has = ctk.CTkButton(
            fbar, text='Has video',
            fg_color=_SURFACE, text_color=_TEXT,
            border_color=_BORDER, border_width=1, hover_color='#30304a',
            command=lambda: self._set_filter('has'), **_btn)
        self._btn_has.grid(row=0, column=1, padx=4, pady=10)

        self._btn_all = ctk.CTkButton(
            fbar, text='All',
            fg_color=_SURFACE, text_color=_TEXT,
            border_color=_BORDER, border_width=1, hover_color='#30304a',
            command=lambda: self._set_filter('all'), **_btn)
        self._btn_all.grid(row=0, column=2, padx=4, pady=10)

        # Search expands with the window, minimum 200px, no fixed width
        self._search_var = tk.StringVar()
        self._search_var.trace_add('write', lambda *_: self._debounced_filter())
        search = ctk.CTkEntry(fbar, textvariable=self._search_var,
                              placeholder_text='Search artist or title...',
                              height=30)
        search.grid(row=0, column=4, padx=(4, 12), pady=10, sticky='ew')
        fbar.grid_columnconfigure(4, weight=2, minsize=200)

        # Treeview
        tree_outer = tk.Frame(self, bg=_BG, bd=0)
        tree_outer.grid(row=2, column=0, sticky='nsew')
        tree_outer.grid_rowconfigure(0, weight=1)
        tree_outer.grid_columnconfigure(0, weight=1)

        sty = ttk.Style()
        sty.theme_use('clam')
        sty.configure('BH.Treeview',
                       background=_BG, foreground=_TEXT,
                       fieldbackground=_BG, rowheight=_fs(30),
                       font=('Segoe UI', _fs(11)), borderwidth=0, relief='flat')
        sty.configure('BH.Treeview.Heading',
                       background='#181825', foreground=_SUBTEXT,
                       font=('Segoe UI', _fs(10), 'bold'),
                       relief='flat', borderwidth=0)
        sty.map('BH.Treeview',
                background=[('selected', '#313244')],
                foreground=[('selected', _TEXT)])
        sty.map('BH.Treeview.Heading',
                background=[('active', '#1e1e2e'), ('pressed', '#1e1e2e')])
        sty.layout('BH.Treeview', [('BH.Treeview.treearea', {'sticky': 'nswe'})])
        sty.configure('BH.Vertical.TScrollbar',
                       background=_BORDER, troughcolor=_BG,
                       borderwidth=0, relief='flat', arrowsize=12)

        self._tree = ttk.Treeview(
            tree_outer, style='BH.Treeview',
            columns=('check', 'label', 'res', 'status', 'vtype', 'yt') + _ACTION_COLS,
            show='headings', selectmode='none')

        self._tree.heading('check',  text='',
                           anchor='center', command=self._toggle_all)
        self._tree.heading('label',  text='Song / Artist  ↕',
                           anchor='w',      command=lambda: self._sort('label'))
        self._tree.heading('res',    text='Resolution  ↕',
                           anchor='center', command=lambda: self._sort('res'))
        self._tree.heading('status', text='Status', anchor='w')
        self._tree.heading('vtype',  text='Video type  ↕', anchor='w',
                           command=lambda: self._sort('vtype'))
        self._tree.heading('yt',     text='Open in browser', anchor='w')

        self._tree.column('check',  width=36,  minwidth=36,  anchor='center', stretch=False)
        self._tree.column('label',  width=520, minwidth=200, anchor='w',      stretch=False)
        self._tree.column('res',    width=95,  minwidth=70,  anchor='center', stretch=False)
        self._tree.column('status', width=170, minwidth=100, anchor='w',      stretch=False)
        self._tree.column('vtype',  width=130, minwidth=90,  anchor='w',      stretch=False)
        self._tree.column('yt',     width=220, minwidth=120, anchor='w',      stretch=False)
        for k, col in enumerate(_ACTION_COLS):
            self._tree.heading(col, text='Actions' if k == 0 else '', anchor='w')
            aw = _ACTION_WIDTHS[_ACTION_KEYS[k]]
            self._tree.column(col, width=aw, minwidth=aw,
                              anchor='center', stretch=False)
        # Song 60% / Resolution 20% / Status 20% of the width left after the
        # checkbox and the action icons
        self._tree.bind('<Configure>', self._size_columns, add='+')
        # dragging a column edge doesn't fire <Configure>: follow the drag too
        for ev in ('<B1-Motion>', '<ButtonRelease-1>'):
            self._tree.bind(ev, lambda e: self.after_idle(self._place_yt_border), add='+')
        self._tree.bind('<Motion>', self._on_tree_motion, add='+')

        self._tree.tag_configure('done',  foreground=_GREEN)
        self._tree.tag_configure('error', foreground=_RED)
        self._tree.tag_configure('busy',  foreground=_BLUE)
        self._tree.tag_configure('warn',  foreground=_YELLOW)
        self._tree.tag_configure('dim',   foreground=_SUBTEXT)
        # Slightly more contrast between rows so a long list is easy to scan
        self._tree.tag_configure('row_even', background='#272740')
        self._tree.tag_configure('row_odd',  background='#1e1e2e')
        self._tree.tag_configure('flash',    background='#45475a')   # clicked link
        # ttk.Treeview can't draw per-column borders: two 1px lines placed over
        # the edges of the "Open in browser" column stand in for one
        self._yt_lines = [tk.Frame(self._tree, bg=_BLUE, width=1) for _ in range(2)]

        vsb = ttk.Scrollbar(tree_outer, orient='vertical',
                            style='BH.Vertical.TScrollbar',
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        self._tree.bind('<Button-1>', self._on_tree_click)
        self._tree.bind('<Button-3>', self._on_tree_right_click)

        # Footer
        foot = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=64)
        foot.grid(row=3, column=0, sticky='ew', pady=(1, 0))
        foot.grid_propagate(False)
        foot.grid_columnconfigure(7, weight=1)

        _fbtn = dict(height=34, corner_radius=7)
        _foot_font = _font(12)
        _foot_bold = _font(13, weight='bold')

        selbox = ctk.CTkFrame(foot, fg_color='transparent')
        selbox.grid(row=0, column=0, padx=(12, 4), pady=15)
        self._sel_btn = ctk.CTkButton(
            selbox, text='Select all', width=105, font=_foot_font,
            fg_color=_BORDER, hover_color='#44445a', text_color=_TEXT,
            command=self._select_all_visible, **_fbtn)
        self._sel_btn.pack(side='left')
        # the songs ⬇ would download: their chosen type has a database link
        self._sel_links_btn = ctk.CTkButton(
            selbox, text='Select with links', width=125, font=_foot_font,
            fg_color=_BORDER, hover_color='#44445a', text_color=_TEXT,
            command=self._select_with_links, **_fbtn)
        self._sel_links_btn.pack(side='left', padx=(8, 0))

        self._clr_btn = ctk.CTkButton(
            foot, text='Clear all', width=90, font=_foot_font,
            fg_color='transparent', hover_color='#30304a',
            text_color=_SUBTEXT, border_color=_BORDER, border_width=1,
            command=self._clear_all, **_fbtn)
        self._clr_btn.grid(row=0, column=1, padx=4, pady=15)

        q = self._settings.get('quality', '720p')
        self._quality_var = tk.StringVar(value=q if q in ('720p', '1080p') else '720p')
        ctk.CTkOptionMenu(
            foot, variable=self._quality_var,
            values=['720p', '1080p'], width=88, height=34,
            command=lambda *_: self._persist_setting('quality', self._quality_var.get()),
            font=_foot_font).grid(row=0, column=2, padx=4, pady=15)

        # what Start does with CON songs: stop for review, or export right away
        m = self._settings.get('download_mode', 'review')
        self._dl_mode_var = tk.StringVar(value=m if m in _DL_MODES else 'review')
        startbox = ctk.CTkFrame(foot, fg_color='transparent')
        startbox.grid(row=0, column=3, padx=4, pady=15)
        self._start_btn = ctk.CTkButton(
            startbox, text='', width=200, font=_foot_bold,
            corner_radius=7, height=34,
            command=self._start_download)
        self._start_btn.pack(side='left')
        self._mode_btn = ctk.CTkButton(
            startbox, text='▾', width=28, font=_foot_bold, corner_radius=7, height=34,
            command=self._mode_menu)
        self._mode_btn.pack(side='left', padx=(2, 0))

        self._resync_btn = ctk.CTkButton(
            foot, text='↺  Auto-sync', width=120, font=_foot_font,
            fg_color='#313244', hover_color='#414160', text_color=_TEXT,
            command=self._start_resync, **_fbtn)
        self._resync_btn.grid(row=0, column=4, padx=4, pady=15)

        self._stop_btn = ctk.CTkButton(
            foot, text='■  Stop', width=90, font=_foot_font,
            fg_color='#4a1a2a', hover_color='#6a2a3e', text_color=_RED,
            state='disabled', command=self._stop, **_fbtn)
        self._stop_btn.grid(row=0, column=5, padx=4, pady=15)

        # Opt-in/out of contributing confirmed matches back to the community pool.
        self._share_var = tk.BooleanVar(
            value=self._settings.get('share_matches', True))
        share_cb = ctk.CTkCheckBox(
            foot, text='Share matches', variable=self._share_var,
            font=_font(11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18,
            command=self._on_share_toggle)
        share_cb.grid(row=0, column=6, padx=(12, 4), pady=15)

        # Videos you synced that the music video database doesn't have yet
        self._missed_btn = ctk.CTkButton(
            foot, text='⇪  Missed entries', width=140, font=_foot_font,
            fg_color='#313244', hover_color='#414160', text_color=_TEXT,
            command=self._export_missed, **_fbtn)
        self._missed_btn.grid(row=0, column=7, padx=4, pady=15)

        # renders every song queued with the WebM action
        self._webm_btn = ctk.CTkButton(
            foot, text='Export 0 WebMs', width=150, font=_foot_bold,
            fg_color='#2e7d4f', hover_color='#3a9a62', text_color='#ffffff',
            text_color_disabled='#9fb8a8', state='disabled',
            command=self._export_webms, **_fbtn)
        self._webm_btn.grid(row=0, column=8, padx=4, pady=15)

        # off on every start on purpose: a shutdown should never come from an
        # old setting
        self._shutdown_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            foot, text='Shut down\nwhen done', variable=self._shutdown_var,
            font=_font(11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18,
            command=self._on_shutdown_toggle).grid(row=0, column=9, padx=(4, 8), pady=15)

        self._cache_btn = ctk.CTkButton(
            foot, text='🗑  Clear cache', width=150, font=_foot_font,
            fg_color='#313244', hover_color='#414160', text_color=_TEXT,
            command=self._clear_all_cache, **_fbtn)
        self._cache_btn.grid(row=0, column=10, padx=4, pady=15)

        # Progress + status (right side of footer)
        prog_frame = ctk.CTkFrame(foot, fg_color='transparent')
        prog_frame.grid(row=0, column=11, padx=(8, 16), pady=15, sticky='e')

        self._progress = ctk.CTkProgressBar(prog_frame, width=190, height=8,
                                             corner_radius=4)
        self._progress.set(0)
        self._progress.pack(pady=(2, 4))

        self._status_lbl = ctk.CTkLabel(
            prog_frame, text='Ready',
            font=_font(10), text_color=_SUBTEXT,
            anchor='e', width=190)
        self._status_lbl.pack()

        self._update_buttons()

    # ---- music video database ----------------------------------------------------

    @staticmethod
    def _match_mvdb(songs):
        """Find each song's database entry: CON songs by con_id, else (and
        song folders) by artist + title. Name matches of CON songs get their
        con_id linked, saved in one go. Returns the loaded database. Runs on
        the scan thread (touches only the new songs and a fresh database)."""
        try:
            db = mvdb.load_all()
        except Exception as e:
            log.warning('Could not load the music video database: %s', e)
            return {}
        pending = []
        for s in songs:
            s.mv = mvdb.resolve(db, s.song_id if s.con else '',
                                s.artist, s.title, s.game_origin or None, pending)
        if pending:
            log.info('mvdb: linked %d con_id(s) by name', len(pending))
            mvdb.remember_con_ids(db, pending)
        return db

    # ---- "Open in browser" column ----------------------------------------------

    def _yt_text(self, s):
        v = mvdb.chosen_video(s.mv)
        vid = (v or {}).get('youtube')
        if not vid:
            name = (f'{s.artist} - {s.title}' if s.artist and s.title
                    else s.title or s.label)
            return f'🔍  Search {name} on YouTube'
        title = self._yt_titles.get(vid)
        if title is None:
            self._want_title(vid)
            return '↗  youtu.be/' + vid
        return '↗  ' + (title or 'youtu.be/' + vid)

    def _open_yt(self, s):
        import webbrowser
        from urllib.parse import quote_plus
        v = mvdb.chosen_video(s.mv)
        vid = (v or {}).get('youtube')
        if vid:
            webbrowser.open('https://www.youtube.com/watch?v=' + vid)
        else:
            # no video yet: search YouTube and open the links window to paste it
            q = (build_query(s.artist, s.title) or s.label) + ' official video'
            webbrowser.open('https://www.youtube.com/results?search_query=' + quote_plus(q))
            VideoLinksWindow.show(self, s)
        # highlight the clicked row for a moment
        iid = self._iid_of.get(id(s))
        if iid:
            tags = tuple(self._tree.item(iid, 'tags'))
            self._tree.item(iid, tags=tags + ('flash',))
            self.after(900, lambda: self._unflash(s))

    def _unflash(self, s):
        iid = self._iid_of.get(id(s))
        if iid:
            self._tree.item(iid, tags=tuple(t for t in self._tree.item(iid, 'tags')
                                            if t != 'flash'))

    def _place_yt_border(self):
        try:
            # where Tk actually drew the column: the cell box of a visible row
            rows = self._tree.get_children()
            box = self._tree.bbox(rows[0], 'yt') if rows else None
            if not box:
                for w_ in self._yt_lines:
                    w_.place_forget()
                return
            x, _, w, _ = box
            h = self._tree.winfo_height()
            self._yt_lines[0].place(x=x, y=0, width=1, height=h)
            self._yt_lines[1].place(x=x + w - 1, y=0, width=1, height=h)
        except Exception:
            pass

    # YouTube titles: fetched in the background with oEmbed (no API key, one
    # small request per video) and cached in the data dir

    _YT_CACHE = os.path.join(updater.data_dir(), 'youtube_titles.json')

    def _load_yt_titles(self):
        try:
            with open(self._YT_CACHE, encoding='utf-8') as f:
                self._yt_titles = _json.load(f)
        except Exception:
            self._yt_titles = {}

    def _want_title(self, vid):
        if vid in self._yt_pending:
            return
        self._yt_pending.add(vid)
        self._yt_jobs.put(vid)
        if not self._yt_thread or not self._yt_thread.is_alive():
            self._yt_thread = threading.Thread(target=self._yt_worker, daemon=True)
            self._yt_thread.start()

    def _yt_worker(self):
        import urllib.request
        import urllib.error
        while True:
            try:
                vid = self._yt_jobs.get(timeout=2)
            except queue.Empty:
                return
            url = ('https://www.youtube.com/oembed?format=json&url='
                   'https://www.youtube.com/watch?v=' + vid)
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    title = _json.load(r).get('title') or ''
            except urllib.error.HTTPError as e:
                # 401/403: embedding disabled, still a real video; 404: gone
                title = '(video unavailable)' if e.code in (400, 404) else ''
            except Exception:
                self._yt_pending.discard(vid)   # offline: try again next time
                continue
            self._queue.put(('yt_title', vid, title))

    def _on_yt_title(self, vid, title):
        self._yt_titles[vid] = title
        for s in self._filtered:
            v = mvdb.chosen_video(s.mv)
            if v and v.get('youtube') == vid:
                self._update_row(s)
        if not self._yt_save_after:
            self._yt_save_after = self.after(3000, self._save_yt_titles)

    def _save_yt_titles(self):
        self._yt_save_after = None
        try:
            tmp = self._YT_CACHE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump(self._yt_titles, f, ensure_ascii=False)
            os.replace(tmp, self._YT_CACHE)
        except Exception as e:
            log.warning('Could not save YouTube titles: %s', e)

    def _type_menu(self, s, event):
        """Click on the Video type cell: pick the type to use for this song."""
        menu = tk.Menu(self, tearoff=0, bg=_SURFACE, fg=_TEXT, activebackground='#414160',
                       activeforeground=_TEXT, font=_font(12))
        have = mvdb.available_types(s.mv)
        pick = mvdb.chosen_type(s.mv)
        links = dict(mvdb.types_of(s.mv))
        if not have:
            menu.add_command(label='No Entry: the automatic search is used', state='disabled')
        for t in mvdb.TYPES:
            if t not in have:
                continue
            v = links[t]
            label = ('●  ' if t == pick else '     ') + t + \
                ('' if v.get('youtube') else '   (no link: automatic search)')
            if links[t].get('directors_choice'):
                label += '   ★ director\'s choice'
            if t == mvdb.default_type(s.mv) and t != pick:
                label += '   (default)'
            menu.add_command(label=label, command=lambda t=t: self._choose_type(s, t))
        menu.add_separator()
        menu.add_command(label='@  Video links...', command=lambda: VideoLinksWindow.show(self, s))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _choose_type(self, s, vtype):
        if s.mv is None or vtype == mvdb.chosen_type(s.mv):
            return
        mvdb.set_choice(self._mvdb, self._mv_source(s),
                        {'con_id': s.mv.get('con_id', ''), 'artist': s.mv.get('artist', ''),
                         'title': s.mv.get('title', '')}, vtype)
        self._update_row(s)

    def _mv_source(self, s):
        if s.mv is not None:
            for k, g in self._mvdb.items():
                if any(x is s.mv for x in g['songs']):
                    return k
        return mvdb.source_key(s.game_origin) if s.game_origin else mvdb.DEFAULT_SOURCE


    def _local_videos(self):
        """Every video this library has that we know the YouTube source of, as
        mvdb submission items: rendered CON songs plus song folders whose
        song.ini has the backstagehero_source BackstageHero writes."""
        items = mvdb.link_edits() + list(conpack.load_contributions())
        for s in getattr(self, '_songs', []) or []:
            if s.con or not s.has_video:
                continue
            vid = _ini_value(s.folder, 'backstagehero_source')
            if not vid:
                continue
            off = _ini_value(s.folder, 'video_start_time')
            try:
                off = int(float(off)) if off else None
            except ValueError:
                off = None
            song = {'artist': _ini_value(s.folder, 'artist') or s.artist,
                    'title': _ini_value(s.folder, 'name') or s.title or s.label}
            charter = _ini_value(s.folder, 'charter')
            if charter:
                song['charter'] = charter
            items.append({'source': mvdb.DEFAULT_SOURCE, 'song': song,
                          'video': mvdb.make_video(vid, 'official', offset_ms=off)})
        return items

    def _export_missed(self):
        name = self._settings.get('contributor_name', '')
        if not name:
            dlg = ctk.CTkInputDialog(
                title='Missed entries',
                text='Name to credit you with in the database\n'
                     '(GitHub / Discord / Reddit name):')
            name = (dlg.get_input() or '').strip()
            if not name:
                return
            self._persist_setting('contributor_name', name)
        try:
            db = mvdb.load()
            entries = mvdb.missed_entries(db, self._local_videos(), name)
        except Exception as e:
            messagebox.showerror('Missed entries', f'Could not read the database:\n{e}')
            return
        if not entries:
            messagebox.showinfo('Missed entries',
                                'Every video you synced is already in the database.')
            return
        path = filedialog.asksaveasfilename(
            title='Save missed entries', defaultextension='.json',
            initialfile='missed_entries.json', filetypes=[('JSON', '*.json')])
        if not path:
            return
        mvdb.write_missed(path, entries, name)
        messagebox.showinfo(
            'Missed entries',
            f'{len(entries)} video(s) the database doesn\'t have yet were saved to\n{path}\n\n'
            'Set the type (official / alternate / fan / meme) and the source if you know '
            'them, then attach the file to an issue or pull request on GitHub.')

    def _persist_setting(self, key, value):
        self._settings[key] = value
        _save_settings(self._settings)

    def _on_share_toggle(self):
        on = bool(self._share_var.get())
        resolver_client.set_sharing(on)
        self._persist_setting('share_matches', on)

    def _startup(self):
        saved = _load_songs_path()
        if saved:
            ok, _ = _validate_folder(saved)
            if ok:
                self._load_library(saved)
                return
            messagebox.showwarning(
                'Songs folder not found',
                f'Your previously used Songs folder can\'t be found at:\n\n'
                f'  {saved}\n\n'
                f'Has it been moved or renamed? Please choose its new location.')
        self._pick_folder(first_run=True)

    def _start_update_check(self):
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        """Background thread: ping the resolver, then check for app and yt-dlp updates."""
        try:
            import VideoDownload as _vd
            cur_ver   = _vd.__version__
            ytdlp_ver = getattr(_vd.yt_dlp.version, '__version__', '0')

            resolver_client.ping(sharing=resolver_client.sharing_enabled(),
                                 app_version=cur_ver)

            info = updater.check_app_update(cur_ver)
            if info:
                latest, asset, sha = info
                self._queue.put(('app_update_available', latest, asset, sha))

            new_ytdlp = updater.maybe_update_ytdlp(ytdlp_ver)
            if new_ytdlp:
                self._queue.put(('ytdlp_updated', new_ytdlp))
        except Exception:
            pass

    def _offer_update(self, latest, asset, sha):
        if messagebox.askyesno(
                'Update available',
                f'v{latest} is available. Install now?\n\n'
                'The app will restart automatically.'):
            self._do_app_update(asset, sha)

    def _flush_pending_update(self):
        """Show a deferred update prompt once a run has finished."""
        if self._pending_update and not self._running:
            latest, asset, sha = self._pending_update
            self._pending_update = None
            self._offer_update(latest, asset, sha)

    def _do_app_update(self, asset, sha):
        """Called on the main thread after user confirms. Shows a progress
        window and runs the download/install on a background thread."""
        dlg = UpdateDialog(self)

        def on_status(stage, **info):
            # runs on the worker thread, bounce it back onto the UI thread
            self.after(0, lambda: dlg.update_stage(stage, **info))

        def _worker():
            ok = updater.apply_app_update(asset, sha, status_cb=on_status)
            if ok:
                self.after(600, self.destroy)   # let "Restarting..." show briefly
        threading.Thread(target=_worker, daemon=True).start()

    def _open_songs_folder(self):
        """Open the current Songs folder in the file explorer."""
        path = self._songs_folder
        if path and os.path.isdir(path):
            _open_in_file_manager(os.path.normpath(path))
        else:
            messagebox.showinfo('No folder', 'No Songs folder is selected yet.')

    def _pick_folder(self, first_run=False):
        while True:
            path = filedialog.askdirectory(
                title='Select your Clone Hero Songs folder',
                mustexist=True)
            if not path:
                if first_run:
                    if messagebox.askyesno(
                            'No folder selected',
                            'A Songs folder is required to continue.\n\n'
                            'Would you like to choose one now?'):
                        continue
                    self.destroy()
                return

            ok, msg = _validate_folder(path)
            if ok:
                # saved once the scan finds songs (_on_library_scanned)
                self._load_library(path)
                return

            messagebox.showerror('Invalid Songs folder', msg +
                                 '\n\nPlease try again.')

    def _load_library(self, path):
        """Scan a Songs folder off the UI thread, with the ScanWindow showing
        the progress. The current library (if any) stays until the new scan
        finds songs."""
        self._status_lbl.configure(text='Scanning your library...')

        # Make sure the queue is being polled before the scan thread posts to it.
        if not self._polling:
            self._polling = True
            self.after(200, self._poll_queue)

        stop = threading.Event()
        self._scan_win = ScanWindow(self, path, on_cancel=stop.set)

        def _worker():
            last = {}

            def progress(info):
                last.update(info)
                self._queue.put(('scan_progress', info))
            try:
                songs, cons, con_errors = _scan_library(path, progress, stop)
                db = None
                if songs:
                    progress(dict(last, stage='mvdb', path='', songs=len(songs)))
                    db = self._match_mvdb(songs)
                self._queue.put(('library_scanned', path, songs, cons, con_errors, db))
            except ScanCancelled:
                self._queue.put(('scan_failed', path, None))
            except Exception as e:
                log.exception('Library scan failed')
                self._queue.put(('scan_failed', path, str(e)))
        threading.Thread(target=_worker, daemon=True).start()

    def _close_scan_win(self):
        win, self._scan_win = getattr(self, '_scan_win', None), None
        if win is not None:
            try:
                win.close()
            except Exception:
                pass

    def _on_scan_failed(self, path, error):
        """Scan cancelled (error None) or failed: keep the library that was
        loaded, or ask for a folder if there's none yet."""
        self._close_scan_win()
        if error:
            messagebox.showerror('Scan failed',
                                 f'Could not scan {path}:\n\n{error}')
        if self._songs_folder:
            n = len(self._songs)
            self._status_lbl.configure(
                text=f'Scan cancelled, {n} song{"s" if n != 1 else ""} '
                     f'from the current folder' if error is None else
                     'Scan failed, the current folder is still loaded')
        else:
            self._pick_folder(first_run=True)

    def _ask_nautilus(self, con_count):
        """CON files were found but Nautilus isn't set up. Returns True if the
        user picked a valid Nautilus.exe (library should be rescanned)."""
        if not messagebox.askyesno(
                'Rock Band CON files found',
                f'Found {con_count} CON file{"s" if con_count != 1 else ""} in your library.\n\n'
                'BackstageHero reads them through Nautilus (it decrypts the song '
                'audio for syncing). The CON files are never modified.\n\n'
                'Locate Nautilus.exe now?'):
            return False
        path = filedialog.askopenfilename(
            title='Locate Nautilus.exe',
            filetypes=[('Nautilus', 'Nautilus.exe'), ('Programs', '*.exe')])
        if not path:
            return False
        conpack.set_nautilus_path(path)
        self._persist_setting('nautilus_path', path)
        return True

    def _on_library_scanned(self, path, songs, cons=(), con_errors=None, db=None):
        if not songs and not cons:
            self._close_scan_win()
            messagebox.showerror('Invalid Songs folder', _NO_SONGS_MSG +
                                 '\n\nPlease try again.')
            if self._songs_folder:
                self._status_lbl.configure(
                    text=f'{len(self._songs)} songs (current folder kept)')
            else:
                self._pick_folder(first_run=True)
            return
        if cons and not conpack.nautilus_ready() and not self._nautilus_asked:
            self._nautilus_asked = True
            self._close_scan_win()        # its grab would block the dialogs
            if self._ask_nautilus(len(cons)):
                self._load_library(path)
                return
        win = getattr(self, '_scan_win', None)
        if win is not None and len(songs) > 500:
            win.update_info({'stage': 'table', 'songs': len(songs),
                             'cons': len(cons)})
            win.update_idletasks()
        self._songs_folder = path
        _save_songs_path(path)
        display = path if len(path) < 58 else '...' + path[-55:]
        self._folder_lbl.configure(text=f'Songs:  {display}')
        self._songs = songs
        self._mvdb = db if db is not None else {}
        self._refresh_cache_size()
        self._apply_filter()
        self._close_scan_win()
        n = len(songs)
        if con_errors:
            log.warning('%d CON file(s) could not be read', len(con_errors))
        unprobed = [s for s in songs if s.has_video and s.res == '...']
        if unprobed and ffmpegAvailable:
            self._status_lbl.configure(
                text=f'{n} songs found, reading resolutions...')
            threading.Thread(target=self._probe_resolutions,
                             args=(unprobed, n), daemon=True).start()
        else:
            self._status_lbl.configure(
                text=f'{n} song{"s" if n != 1 else ""} found')
        self._update_buttons()

    def _probe_resolutions(self, songs, total_songs):
        """Background thread: probe resolutions for unprobed videos, a few in
        parallel (ffmpeg is the bottleneck, so this is ~Nx faster on first scan)."""
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            # CON songs: read the .webm directly, there's no song.ini to cache it in
            # CON songs: read the .webm directly (resolution and our tag in one
            # ffmpeg run), there's no song.ini to cache it in
            futures = {(pool.submit(conpack.probe_video, s.video) if s.con
                        else pool.submit(probe_resolution, s.folder)): s
                       for s in songs}
            for fut in as_completed(futures):
                if self._stop_evt.is_set():
                    break
                s = futures[fut]
                try:
                    if s.con:
                        res, tag = fut.result()
                        s.res = res or '?'
                        s.ours = None if tag is None else bool(tag)
                    else:
                        s.res = fut.result() or '?'
                except Exception:
                    s.res = '?'
                done += 1
                remaining = len(songs) - done
                self._queue.put(('res_update', s,
                                 f'{total_songs} songs found, reading resolutions ({remaining} left)...'
                                 if remaining else f'{total_songs} songs found'))

    def _set_filter(self, mode):
        self._filter_mode = mode
        _active   = dict(fg_color=_BLUE, text_color='#11111b',
                         hover_color='#7aaef8', border_width=0)
        _inactive = dict(fg_color=_SURFACE, text_color=_TEXT,
                         border_color=_BORDER, border_width=1,
                         hover_color='#30304a')
        self._btn_missing.configure(**(_active if mode == 'missing' else _inactive))
        self._btn_has.configure(    **(_active if mode == 'has'     else _inactive))
        self._btn_all.configure(    **(_active if mode == 'all'     else _inactive))
        self._apply_filter()

    def _debounced_filter(self):
        """Coalesce rapid keystrokes so a big library isn't re-filtered on every
        character."""
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.after(180, self._apply_filter)

    def _apply_filter(self):
        self._search_after = None
        term = self._search_var.get().lower()
        mode = self._filter_mode
        out  = []
        for s in self._songs:
            if mode == 'missing' and s.has_video:     continue
            if mode == 'has'     and not s.has_video: continue
            if term and term not in s.key:            continue
            out.append(s)
        # Sort
        if self._sort_col == 'res':
            def _res_key(s):
                m = re.match(r'(\d+)', s.res)
                return int(m.group(1)) if m else 0
            out.sort(key=_res_key, reverse=not self._sort_asc)
        elif self._sort_col == 'vtype':
            out.sort(key=lambda s: (mvdb.sort_key(s.mv), s.key), reverse=not self._sort_asc)
        else:
            out.sort(key=lambda s: s.key, reverse=not self._sort_asc)
        self._filtered = out
        self._refresh_tree()

    def _sort(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        up = '↑' if self._sort_asc else '↓'
        self._tree.heading('label',
            text=f'Song / Artist  {up if col == "label" else "↕"}')
        self._tree.heading('res',
            text=f'Resolution  {up if col == "res" else "↕"}')
        self._tree.heading('vtype',
            text=f'Video type  {up if col == "vtype" else "↕"}')
        self._apply_filter()

    def _size_columns(self, event):
        avail = event.width - 36 - 2 - sum(_ACTION_WIDTHS.values())
        if avail < 300:
            return
        res = int(avail * 0.08)
        status = int(avail * 0.18)
        vtype = int(avail * 0.13)
        yt = int(avail * 0.22)
        self._tree.column('label',  width=avail - res - status - vtype - yt)
        self._tree.column('res',    width=res)
        self._tree.column('status', width=status)
        self._tree.column('vtype',  width=vtype)
        self._tree.column('yt',     width=yt)
        self.after_idle(self._place_yt_border)

    # ---- actions column ------------------------------------------------------

    def _actions_of(self, s):
        """Which action icons are enabled for a song."""
        if s.rendering:                      # rendering: everything locked
            return set()
        v = mvdb.chosen_video(s.mv)
        dl = {'download'} if (v and v.get('youtube') and not self._is_pending(s)) else set()
        if s.review:                         # downloaded (review, or exported and kept)
            return {'play', 'collage', 'search', 'mp4', 'webm', 'clear', 'more'} | dl
        if s.con and s.has_video:            # finished video
            return {'play', 'search', 'more'} | dl
        return {'more'} | dl                 # video links work for every song

    @staticmethod
    def _foreign(s):
        """The song has a finished video that BackstageHero didn't make."""
        return s.has_video and s.ours is False

    def _row_values(self, s):
        chk         = '  ☑' if s.checked else '  ☐'
        status_text = s.status or ('✔' if s.has_video else '✗')
        if self._foreign(s) and not status_text.startswith('⟳'):
            # a video BackstageHero didn't make (downloaded or synced by hand)
            status_text = '⚠  ' + (s.status or 'Not by BackstageHero')
        enabled = self._actions_of(s)
        icons = tuple(_ACTIONS[a][0] if a in enabled else '' for a in _ACTION_KEYS)
        return (chk, s.label, s.res or '-', status_text, mvdb.type_summary(s.mv),
                self._yt_text(s)) + icons

    def _row_tags(self, s, row_tag):
        stag = s.stag or ('warn' if self._foreign(s) else
                          'dim' if s.has_video else 'error')
        return (row_tag,) + ((stag,) if stag else ())

    def _refresh_tree(self):
        self._tree.delete(*self._tree.get_children())
        self._iid_map.clear()
        self._iid_of.clear()
        for i, s in enumerate(self._filtered):
            row_tag = 'row_even' if i % 2 == 0 else 'row_odd'
            iid = self._tree.insert('', 'end', values=self._row_values(s),
                                    tags=self._row_tags(s, row_tag))
            self._iid_map[iid] = s
            self._iid_of[id(s)] = iid
        self._update_buttons()
        self.after_idle(self._place_yt_border)

    def _update_row(self, s: Song):
        """Refresh the single visible row for Song s (O(1) lookup)."""
        iid = self._iid_of.get(id(s))
        if not iid:
            return   # not currently visible under the active filter
        existing = list(self._tree.item(iid, 'tags'))
        row_tag  = next((t for t in existing
                         if t in ('row_even', 'row_odd')), 'row_odd')
        self._tree.item(iid, values=self._row_values(s),
                        tags=self._row_tags(s, row_tag))

    def _action_at(self, event):
        """(song, action key) under the mouse, or (song, None) / (None, None)."""
        iid = self._tree.identify_row(event.y)
        s = self._iid_map.get(iid) if iid else None
        if not s:
            return None, None
        col = self._tree.identify_column(event.x)        # '#1'.. in display order
        try:
            name = self._tree['columns'][int(col[1:]) - 1]
        except (ValueError, IndexError):
            return s, None
        key = _ACTION_KEYS[_ACTION_COLS.index(name)] if name in _ACTION_COLS else None
        if name in ('vtype', 'yt', 'check'):
            key = name
        return s, key

    def _on_tree_motion(self, event):
        s, key = self._action_at(event)
        hint = ''
        if s and key == 'yt':
            v = mvdb.chosen_video(s.mv)
            hint = ('Open the video in your browser' if v and v.get('youtube')
                    else 'Search YouTube in your browser and set the link')
        elif s and key == 'check':
            hint = 'Select'
        elif s and key == 'vtype':
            hint = ('Choose which video type to use' if len(mvdb.available_types(s.mv)) > 1
                    else 'Video links')
        elif s and key and key in self._actions_of(s):
            hint = _ACTIONS[key][1]
            if key == 'play' and s.review and not s.has_video:
                hint = 'Play the downloaded video (not cut yet)'
            if key == 'play' and s.has_video:
                hint = 'Play the exported video'
            if key in ('mp4', 'webm') and s.review and s.review.get('rendered'):
                hint += ' (again, from the kept original)'
            if key == 'clear' and s.review and not s.review.get('rendered'):
                hint = 'Clear cache: discard this download (not exported yet)'
            if key == 'webm' and s.webm_queued:
                hint = 'Remove from the WebM queue'
            if key == 'download':
                hint = f'Download the {mvdb.chosen_type(s.mv)} video from the database' + \
                    (' again' if (s.has_video or s.review) else '')
        cursor = 'hand2' if hint else ''
        if str(self._tree.cget('cursor')) != cursor:
            self._tree.configure(cursor=cursor)
        if hint != self._action_hint:
            self._action_hint = hint
            if hint:
                self._status_lbl.configure(text=f'{s.label}   ·   {hint}')

    def _on_tree_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        if region == 'heading':
            return      # column-header command handles sort
        if region in ('cell', 'tree'):
            s, key = self._action_at(event)
            if not s:
                return
            if key == 'vtype':
                self._type_menu(s, event)
                return
            if key == 'yt':
                self._open_yt(s)
                return
            if key == 'check':          # only the checkbox selects a row
                s.checked = not s.checked
                self._update_row(s)
                self._update_buttons()
                return
            if key and key in self._actions_of(s):
                self._do_action(s, key)

    def _do_action(self, s, key):
        if key == 'play':
            # the export once there is one (to check the auto-edit), else the download
            path = s.video if s.has_video else s.review['raw']
            if os.path.exists(path):
                _open_in_file_manager(path)     # opens files in the default player
        elif key == 'collage':
            self._show_collage(s)
        elif key == 'search':
            self._search_again(s)
        elif key == 'download':
            self._download_db(s)
        elif key == 'clear':
            self._clear_cache(s)
        elif key == 'mp4':
            self._queue_render(s, key)
        elif key == 'webm':
            self._toggle_webm(s)
        elif key == 'more':
            VideoLinksWindow.show(self, s)

    def _show_collage(self, s):
        out_dir = os.path.join(conpack.work_dir(s.con, s.song_id), 'collage')
        s.status, s.stag = '⟳  Making collage...', 'busy'
        self._update_row(s)

        def _worker():
            try:
                result = conpack.make_collage(s.review['raw'], out_dir)
                self._queue.put(('collage_ready', s, result, None))
            except Exception as e:
                self._queue.put(('collage_ready', s, None, str(e)))
        threading.Thread(target=_worker, daemon=True).start()

    def _search_again(self, s):
        """Mark the current YouTube video bad and look for a different one."""
        if s.review and not s.has_video:
            if not messagebox.askyesno(
                    'Search again?',
                    f'{s.label}\n\nMark this video as wrong and search YouTube again?\n'
                    '(it will be skipped in future searches)'):
                return
            self._mark_wrong(s, s.review['video_id'])
            conpack.discard_work(s.con, s.song_id)
            s.review = None
            s.webm_queued = False
            s.dl_mode = 'search'
            self._enqueue_downloads([s], replace=True)
            return
        # finished video: same checks as Re-download (protects hand-made videos)
        if not self._confirm_redownload([s]):
            return
        tag = conpack.read_tag(s.video) or ''
        m = re.search(r'source=(\S+)', tag)
        if m:
            self._mark_wrong(s, m.group(1))
        if s.review:
            conpack.clear_cache(s.con, s.song_id)
            s.review = None
            s.webm_queued = False
        s.dl_mode = 'search'
        self._enqueue_downloads([s], replace=True)

    def _db_ids(self, s):
        return {v.get('youtube') for v in (s.mv or {}).get('videos', []) if v.get('youtube')}

    def _mark_wrong(self, s, video_id):
        """Search again marks the current video wrong, but never a video the
        database links for the song: that one is skipped for this search only."""
        if video_id and video_id not in self._db_ids(s):
            conpack.mark_bad(s.con, s.song_id, video_id)

    def _download_db(self, s):
        """⬇: download the chosen type's database video, replacing what's there."""
        v = mvdb.chosen_video(s.mv)
        if not (v and v.get('youtube')):
            return
        what = f'the {mvdb.chosen_type(s.mv)} video (youtu.be/{v["youtube"]})'
        if s.review and not s.has_video:
            if not messagebox.askyesno(
                    'Download again?',
                    f'{s.label}\n\nDiscard the downloaded video waiting for review and '
                    f'download {what} instead?'):
                return
            conpack.discard_work(s.con, s.song_id)
            s.review = None
            s.webm_queued = False
        elif s.has_video:
            # same checks as Re-download: protects videos BackstageHero didn't make
            if not self._confirm_redownload([s]):
                return
            if s.review:
                conpack.clear_cache(s.con, s.song_id)
                s.review = None
                s.webm_queued = False
        s.dl_mode = 'db'
        self._enqueue_downloads([s], replace=True)

    def _is_pending(self, s):
        return s.status.startswith(('○', '⟳'))

    # ---- cache (kept original downloads) -----------------------------------

    def _clear_cache(self, s):
        if not s.review:
            return
        if not s.review.get('rendered') and not messagebox.askyesno(
                'Clear cache?',
                f'{s.label}\n\nThis download hasn\'t been exported yet. '
                'Delete it anyway?\n(you\'d have to download it again)'):
            return
        conpack.clear_cache(s.con, s.song_id)
        s.review = None
        s.webm_queued = False
        s.status, s.stag = '', ''
        self._update_row(s)
        self._update_buttons()
        self._refresh_cache_size()

    def _clear_all_cache(self):
        cached = [s for s in self._songs if s.review]
        pending = [s for s in cached if not s.review.get('rendered')]
        if any(s.rendering for s in self._songs) or self._running:
            messagebox.showinfo('Clear cache', 'Wait for the downloads and exports to finish.')
            return
        size = _fmt_size(self._cache_bytes)
        msg = f'Delete the kept original downloads ({size})?'
        if pending:
            msg += (f'\n\n{len(pending)} of them haven\'t been exported yet and will need '
                    'to be downloaded again.')
        if not messagebox.askyesno('Clear cache', msg, icon='warning' if pending else 'question'):
            return
        conpack.clear_all_cache()
        for s in cached:
            s.review = None
            s.webm_queued = False
            s.status, s.stag = '', ''
            self._update_row(s)
        self._update_buttons()
        self._refresh_cache_size()

    def _refresh_cache_size(self):
        def _worker():
            try:
                n = conpack.cache_size()
            except Exception:
                n = 0
            self._queue.put(('cache_size', n))
        threading.Thread(target=_worker, daemon=True).start()

    def _toggle_webm(self, s):
        """WebM renders are slow (2-pass VP8): queue them and export them all
        at once with the green button."""
        s.webm_queued = not s.webm_queued
        if s.webm_queued:
            s.status, s.stag = '⏸  In WebM queue', 'dim'
        else:
            s.status, s.stag = _review_status(s.review), _review_tag(s.review)
        self._update_row(s)
        self._update_buttons()

    def _export_webms(self):
        queued = [s for s in self._songs if s.webm_queued and s.review and not s.rendering]
        for s in queued:
            s.webm_queued = False
            self._queue_render(s, 'webm')
        if queued:
            self._webm_batch = True
        self._update_buttons()

    # ---- shut down when the WebM exports finish ------------------------------

    def _on_shutdown_toggle(self):
        if self._shutdown_var.get():
            self._status_lbl.configure(
                text='The PC will shut down when the WebM exports finish (60 s to cancel)')

    def _renders_idle(self):
        return not any(s.rendering for s in self._songs) and self._render_jobs.empty()

    def _maybe_shutdown(self):
        """Called after each render: once the WebM batch is done, count down."""
        if not (self._webm_batch and self._renders_idle()):
            return
        if self._running:
            return      # Download & export: more renders come with the downloads
        self._webm_batch = False
        if self._shutdown_var.get() and not self._running:
            ShutdownCountdown(self, seconds=60)

    def _queue_render(self, s, fmt):
        s.webm_queued = False
        s.rendering = 'queued'
        s.status, s.stag = f'⏸  Queued for {fmt.upper()}', 'dim'
        self._update_row(s)
        self._render_jobs.put((s, fmt))
        if not self._render_thread or not self._render_thread.is_alive():
            self._render_thread = threading.Thread(target=self._render_worker, daemon=True)
            self._render_thread.start()

    def _render_worker(self):
        """Renders one video at a time (encoding is CPU heavy), while downloads
        keep going on their own thread."""
        while True:
            try:
                s, fmt = self._render_jobs.get(timeout=1)
            except queue.Empty:
                return
            self._queue.put(('render_progress', s, f'Rendering {fmt.upper()}', 0.0))
            try:
                dest = conpack.render(
                    s.con, s.song_id, fmt, __version__,
                    progress=lambda f, s=s, fmt=fmt: self._queue.put(
                        ('render_progress', s, f'Rendering {fmt.upper()}', f)))
                self._queue.put(('render_done', s, dest, None))
            except Exception as e:
                log.exception('Render failed for %s', s.label)
                self._queue.put(('render_done', s, None, str(e)))

    def _toggle_all(self):
        """Click on the checkbox column header: select/deselect all visible."""
        all_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        for s in self._filtered:
            s.checked = not all_on
        self._refresh_tree()

    def _select_all_visible(self):
        """Select/Deselect all, just the songs in the current filter view."""
        all_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        for s in self._filtered:
            s.checked = not all_on
        self._refresh_tree()

    def _select_with_links(self):
        """Select only the visible songs whose chosen video type has a database
        link (the ones downloaded without a YouTube search)."""
        for s in self._filtered:
            v = mvdb.chosen_video(s.mv)
            s.checked = bool(v and v.get('youtube'))
        self._refresh_tree()

    def _mode_menu(self):
        """▾ next to Start: what Start does after downloading CON songs."""
        menu = tk.Menu(self, tearoff=0, bg=_SURFACE, fg=_TEXT, activebackground='#414160',
                       activeforeground=_TEXT, selectcolor=_TEXT, font=_font(12))
        for key, (label, _fmt, help_) in _DL_MODES.items():
            menu.add_radiobutton(label=f'{label}   ·   {help_}', value=key,
                                 variable=self._dl_mode_var,
                                 command=self._on_mode_change)
        b = self._start_btn
        try:
            menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() - _fs(8) - 3 * _fs(26))
        finally:
            menu.grab_release()

    def _on_mode_change(self):
        self._persist_setting('download_mode', self._dl_mode_var.get())
        self._update_buttons()

    def _clear_all(self):
        """Uncheck every song in the full library, regardless of current filter."""
        for s in self._songs:
            s.checked = False
        self._refresh_tree()

    def _on_tree_right_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid or iid not in self._iid_map:
            return
        s = self._iid_map[iid]
        menu = tk.Menu(self, tearoff=0,
                       bg=_SURFACE, fg=_TEXT,
                       activebackground='#313244', activeforeground=_TEXT,
                       relief='flat', borderwidth=1,
                       activeborderwidth=0)
        if s.has_video and not s.con:   # CON videos are cut, not offset
            menu.add_command(label='Adjust sync offset',
                             command=lambda: self._open_sync_editor(s))
            menu.add_separator()
        if s.has_video:
            menu.add_command(label='Re-download video...',
                             command=lambda: self._redownload_one(s))
            menu.add_separator()
        menu.add_command(label='Open folder',
                         command=lambda: _open_in_file_manager(s.folder))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_sync_editor(self, song: Song):
        def on_save(ms: int, share: bool):
            set_ini_values(song.folder, {'video_start_time': str(ms)})
            s_abs = abs(ms) / 1000.0
            if ms < -50:
                song.status = f'Synced  (−{s_abs:.1f}s intro)'
            elif ms > 50:
                song.status = f'Synced  (+{s_abs:.1f}s)'
            else:
                song.status = 'Synced  (aligned)'
            song.stag = 'done'
            self._update_row(song)
            if share and resolver_client.enabled():
                ch  = resolver_client.chart_hash(song.folder)
                vid = get_stored_source(song.folder)
                if ch and vid:
                    artist, title = read_metadata(song.folder)
                    resolver_client.report(ch, vid, ms, 0.5, artist, title)
        SyncEditor(self, song, on_save=on_save)

    def _update_buttons(self):
        checked    = [s for s in self._songs if s.checked]
        n          = len(checked)
        has_vid    = [s for s in checked if s.has_video and not s.con]
        can_start  = n > 0 and not self._running
        can_resync = len(has_vid) > 0 and not self._running and self._sync_ready

        label = _DL_MODES[self._dl_mode_var.get()][0]
        self._start_btn.configure(
            text=f'▶  {label} ({n})' if n else f'▶  {label}',
            state='normal' if can_start else 'disabled')
        self._mode_btn.configure(state='normal' if not self._running else 'disabled')
        self._resync_btn.configure(
            state='normal' if can_resync else 'disabled')
        self._stop_btn.configure(
            state='normal' if self._running else 'disabled')

        self._cache_btn.configure(
            text=f'🗑  Clear cache ({_fmt_size(self._cache_bytes)})' if self._cache_bytes
            else '🗑  Clear cache',
            state='normal' if self._cache_bytes else 'disabled')
        nq = sum(1 for s in self._songs if s.webm_queued)
        self._webm_btn.configure(text=f'Export {nq} WebM{"" if nq == 1 else "s"}',
                                 state='normal' if nq else 'disabled')

        all_vis_on = bool(self._filtered) and all(s.checked for s in self._filtered)
        self._sel_btn.configure(
            text='Deselect all' if all_vis_on else 'Select all')

    _BIG_BATCH = 25   # confirm before kicking off a run this large

    def _start_download(self):
        if self._running:
            return
        checked = [s for s in self._songs if s.checked]
        if not checked:
            return
        missing  = [s for s in checked if not s.has_video]
        existing = [s for s in checked if s.has_video]

        if missing:
            # usual case: just fill the gaps, leave the done ones alone
            work, replace = missing, False
            if existing:
                for s in existing:
                    s.status = '✔  Has video'
                    s.stag   = 'dim'
                    self._update_row(s)
                self._status_lbl.configure(
                    text=f'{len(existing)} already have video, skipping; '
                         f'downloading {len(missing)} missing')
        else:
            # they only picked songs that already have a video, so Start would do
            # nothing. probably means they want them re-fetched, so ask first.
            work = self._confirm_redownload(existing)
            if not work:
                self._status_lbl.configure(
                    text='Nothing to download, selection already has videos')
                return
            replace = True

        if not self._confirm_batch(work, 'download'):
            return
        fmt = _DL_MODES[self._dl_mode_var.get()][1]
        for s in work:
            s.auto_export = fmt if s.con else ''
        if fmt and any(s.con for s in work):
            self._webm_batch = self._webm_batch or fmt == 'webm'
        self._launch(work, replace=replace, resync=False)

    def _start_resync(self):
        if self._running:
            return
        # CON videos have the sync baked into the cut; re-download to redo them
        work = [s for s in self._songs if s.checked and s.has_video and not s.con]
        if not work:
            return
        if not self._confirm_batch(work, 'auto-sync'):
            return
        self._launch(work, replace=False, resync=True)

    @staticmethod
    def _made_by_us(s):
        """True if the song's current video came from BackstageHero: a CON video
        carries our WebM tag, a folder video has backstagehero_source in song.ini.
        A video fixed by hand and re-exported from an editor loses the tag."""
        if s.con:
            return conpack.is_ours(s.video)
        return bool(get_stored_source(s.folder))

    def _confirm_redownload(self, existing):
        """Ask before replacing existing videos, and ask again for any that
        BackstageHero didn't make (manual downloads/syncs). Returns the songs to
        replace, or [] to do nothing."""
        ne = len(existing)
        if not messagebox.askyesno(
                'Re-download videos?',
                f'{"All " if ne != 1 else ""}{ne} selected song{"s" if ne != 1 else ""} '
                f'already {"have" if ne != 1 else "has"} a video.\n\n'
                f'Re-download from YouTube and replace {"them" if ne != 1 else "it"}?'):
            return []
        self._status_lbl.configure(text='Checking which videos were made by BackstageHero...')
        self.update_idletasks()
        foreign = [s for s in existing if not self._made_by_us(s)]
        if not foreign:
            return existing
        names = '\n'.join('  ' + s.label for s in foreign[:8])
        if len(foreign) > 8:
            names += f'\n  ...and {len(foreign) - 8} more'
        if len(foreign) == len(existing) == 1:
            ok = messagebox.askyesno(
                'Not a BackstageHero video',
                f'This video wasn\'t downloaded by BackstageHero:\n\n{names}\n\n'
                'It may be one you downloaded or synced by hand.\n'
                'Are you really sure you want to re-download it?',
                icon='warning', default='no')
            return existing if ok else []
        ans = messagebox.askyesnocancel(
            'Not BackstageHero videos',
            f'{len(foreign)} of these videos weren\'t downloaded by BackstageHero:\n\n'
            f'{names}\n\n'
            'They may be ones you downloaded or synced by hand.\n\n'
            'Yes: re-download them too\n'
            'No: keep them, only replace the BackstageHero ones\n'
            'Cancel: do nothing',
            icon='warning', default='no')
        if ans is None:
            return []
        if ans:
            return existing
        return [s for s in existing if s not in foreign]

    def _redownload_one(self, s):
        work = self._confirm_redownload([s])
        if work:
            self._enqueue_downloads(work, replace=True)

    def _confirm_batch(self, work, verb):
        """Guard against an accidental huge run. Returns False to abort."""
        if len(work) <= self._BIG_BATCH:
            return True
        return messagebox.askyesno(
            'Large batch',
            f'About to {verb} {len(work)} songs. This can take a while'
            + (' and use a lot of bandwidth and disk space' if verb == 'download' else '')
            + '.\n\nContinue?')

    def _launch(self, targets, replace, resync):
        self._enqueue_downloads(targets, replace, resync)

    def _enqueue_downloads(self, targets, replace, resync=False):
        """Queue songs for the download thread; starts it if it isn't running.
        Songs added mid-run (e.g. 'search again') join the end of the queue."""
        if not targets:
            return
        for s in targets:
            s.status = '○  Pending'
            s.stag   = 'dim'
            self._update_row(s)
        with self._dl_lock:
            self._pending.extend((s, replace, resync) for s in targets)
            if self._dl_alive:
                return
            self._dl_alive = True
        self._run_quality = quality_format(1080 if self._quality_var.get() == '1080p' else 720)
        self._running = True
        self._stop_evt.clear()
        self._progress.set(0)
        self._update_buttons()
        threading.Thread(target=self._dl_thread, daemon=True).start()

    def _next_pending(self):
        with self._dl_lock:
            if self._pending and not self._stop_evt.is_set():
                return self._pending.pop(0)
            self._pending.clear()
            self._dl_alive = False
            return None

    def _dl_thread(self):
        quality = self._run_quality
        done = skipped = errors = 0
        # adaptive pacing: pause between songs that hit YouTube, scaled by how
        # the run is going. clean streaks creep the delay down, getting throttled
        # doubles it. skipped songs make no requests at all so they get no pause,
        # which is what makes re-runs over a mostly-done library fast.
        pace = 1.0
        clean_streak = 0
        prev_hit_network = False
        i = -1
        while True:
            item = self._next_pending()
            if item is None:
                break
            s, replace, resync = item
            i += 1
            total = i + 1 + len(self._pending)
            if self._stop_evt.is_set():
                self._abandon_pending()
                self._queue.put(('stopped', i, total, done, skipped, errors))
                return

            if prev_hit_network:
                # pause between YouTube downloads, counting down in the GUI
                end = time.monotonic() + random.uniform(SONG_DELAY_MIN, SONG_DELAY_MAX) * pace
                stopped = False
                while True:
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    self._queue.put(('song_wait', s, i, total, int(left + 0.999)))
                    if self._stop_evt.wait(min(1.0, left)):
                        stopped = True
                        break
                if stopped:
                    self._abandon_pending()
                    self._queue.put(('stopped', i, total, done, skipped, errors))
                    return

            self._queue.put(('song_start', s, i, total))
            errored = []
            events  = []
            result  = run_song_with_backoff(
                s.folder, s.label, quality,
                self._sync_ready,
                replace=replace, resync=resync,
                errored=errored, stop_evt=self._stop_evt, events=events,
                con_song=(s.con, s.song_id, s.artist, s.title) if s.con else None,
                mode=s.dl_mode,
                progress=lambda text, frac, s=s, i=i: self._queue.put(
                    ('song_progress', s, i, total, text, frac)))

            s.dl_mode = 'auto'
            fmt, s.auto_export = s.auto_export, ''    # only for this download
            prev_hit_network = result != 'skipped'
            if events:
                # got pushed back this song, slow right down for a while
                pace = min(pace * 2.0, 6.0)
                clean_streak = 0
            elif prev_hit_network:
                clean_streak += 1
                if clean_streak >= 8:
                    # going fine, ease off the brake a notch
                    pace = max(pace * 0.7, 1.0)   # never under the 30 s floor
                    clean_streak = 0

            if result == 'stop':
                self._abandon_pending()
                self._queue.put(('rate_limited', s, i, total))
                return
            if result == 'stopped':
                self._abandon_pending()
                self._queue.put(('stopped', i, total, done, skipped, errors))
                return

            if result == 'review':
                done += 1
                s.review = conpack.load_review(s.con, s.song_id)
                self._queue.put(('song_review', s, i, total, fmt))
            elif result == 'skipped':
                skipped += 1
                self._queue.put(('song_skipped', s, i, total))
            elif errored:
                errors += 1
                self._queue.put(('song_error', s, i, total, errored[-1]))
            else:
                done += 1
                # process_download already probed and stored the resolution in song.ini
                if s.con:
                    s.res = probe_video_height(s.video) or '?'
                elif not resync:
                    stored = get_stored_resolution(s.folder)
                    if stored:
                        s.res = stored
                self._queue.put(('song_done', s, i, total, result == 'unsynced'))

        self._queue.put(('finished', i + 1, done, skipped, errors))

    def _abandon_pending(self):
        with self._dl_lock:
            self._pending.clear()
            self._dl_alive = False

    @staticmethod
    def _run_summary(done, skipped, errors):
        parts = []
        if done:    parts.append(f'{done} downloaded')
        if skipped: parts.append(f'{skipped} already had video')
        if errors:  parts.append(f'{errors} failed (details in log)')
        return ', '.join(parts) if parts else 'nothing to do'

    def _stop(self):
        if not self._running:
            return
        self._stop_evt.set()
        # Reflect the press immediately so an impatient user isn't left guessing.
        self._stop_btn.configure(state='disabled')
        self._status_lbl.configure(text='Stopping after current song...')

    def _poll_queue(self):
        try:
            while True:
                self._handle_msg(self._queue.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _handle_msg(self, msg):
        kind = msg[0]

        if kind == 'song_start':
            _, s, i, total = msg
            s.status = '⟳  Downloading...'
            s.stag   = 'busy'
            self._update_row(s)
            self._progress.set(i / total)
            self._status_lbl.configure(
                text=f'{i + 1} / {total}   {s.label}')

        elif kind == 'song_wait':
            # countdown before the next song's YouTube requests
            _, s, i, total, secs = msg
            s.status = f'⏳  Waiting {secs}s'
            s.stag   = 'dim'
            self._update_row(s)
            self._status_lbl.configure(
                text=f'{i + 1} / {total}   Waiting {secs}s before the next download '
                     f'(so YouTube doesn\'t flag it as abuse)')

        elif kind == 'cache_size':
            self._cache_bytes = msg[1]
            self._update_buttons()

        elif kind == 'yt_title':
            _, vid, title = msg
            self._on_yt_title(vid, title)

        elif kind == 'song_review':
            _, s, i, total, fmt = msg
            synced = s.review and (s.review.get('matched') or s.review.get('autoedit'))
            s.status = _review_status(s.review) + ('' if synced else ' (not synced)')
            s.stag   = 'warn'
            if fmt and s.review and synced and not s.rendering:
                # Download & export: render now (one at a time, downloads go on)
                self._queue_render(s, fmt)
            self._update_row(s)
            self._refresh_cache_size()
            self._progress.set((i + 1) / total)

        elif kind == 'collage_ready':
            _, s, result, err = msg
            s.status = _review_status(s.review)
            s.stag   = _review_tag(s.review)
            self._update_row(s)
            if err:
                messagebox.showerror('Collage', f'Could not make the collage:\n\n{err}')
            else:
                CollageViewer(self, s.label, *result)

        elif kind == 'render_progress':
            _, s, text, frac = msg
            s.rendering = 'running'
            s.status = f'⟳  {text} {int(frac * 100)}%'
            s.stag   = 'busy'
            self._update_row(s)

        elif kind == 'render_done':
            _, s, dest, err = msg
            s.rendering = ''
            if err:
                s.status, s.stag = f'✗  Render failed: {err[:30]}', 'error'
            else:
                s.review    = conpack.load_review(s.con, s.song_id)   # original kept
                s.has_video = True
                s.ours      = True
                s.res       = probe_video_height(dest) or '?'
                s.status    = f'✔  Rendered {os.path.splitext(dest)[1][1:].upper()}'
                s.stag      = 'done'
            self._update_row(s)
            self._refresh_cache_size()
            self._maybe_shutdown()

        elif kind == 'song_progress':
            # step inside the current song: '3/5 Matching audio (video 2/3)'
            _, s, i, total, text, frac = msg
            s.status = f'⟳  {text}'
            s.stag   = 'busy'
            self._update_row(s)
            if frac is not None:
                self._progress.set((i + frac) / total)
            self._status_lbl.configure(text=f'{i + 1} / {total}   {s.label}   ·   {text}')

        elif kind == 'song_done':
            _, s, i, total, unsynced = msg
            # CON videos can't be re-timed later, so flag a guessed cut
            s.status    = '✔  Done (not synced, default cut)' if unsynced else '✔  Done'
            s.stag      = 'done'
            s.has_video = True
            if not s.con:       # folder video downloaded now (backstagehero_source)
                s.ours = True
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'song_skipped':
            _, s, i, total = msg
            s.status    = '✔  Already had video'
            s.stag      = 'dim'
            s.has_video = True
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'song_error':
            _, s, i, total, detail = msg
            short = (detail or '').strip().splitlines()[0] if detail else ''
            if len(short) > 40:
                short = short[:38] + '...'
            s.status = f'✗  {short}' if short else '✗  Error'
            s.stag   = 'error'
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'rate_limited':
            _, s, i, total = msg
            s.status = '✗  Rate limited'
            s.stag   = 'error'
            self._update_row(s)
            self._running = False
            self._update_buttons()
            self._status_lbl.configure(text='Rate limited, wait and try again')
            messagebox.showwarning(
                'YouTube rate limit',
                'YouTube is asking to confirm you\'re not a bot.\n\n'
                'Your IP is being rate-limited. Wait a little while and '
                'try again. Everything already downloaded is safe.')
            self._flush_pending_update()

        elif kind == 'stopped':
            _, i, total, done, skipped, errors = msg
            self._running = False
            self._update_buttons()
            self._status_lbl.configure(
                text='Stopped. ' + self._run_summary(done, skipped, errors))
            self._apply_filter()
            self._flush_pending_update()

        elif kind == 'finished':
            _, total, done, skipped, errors = msg
            self._running = False
            self._progress.set(1.0)
            self._status_lbl.configure(
                text='Done. ' + self._run_summary(done, skipped, errors))
            self._update_buttons()
            # Re-apply filter so has_video status reflects new downloads
            self._apply_filter()
            self._flush_pending_update()
            self._maybe_shutdown()     # Download & export WebM: renders may be done

        elif kind == 'res_update':
            _, s, status_text = msg
            self._update_row(s)
            if not self._running:
                self._status_lbl.configure(text=status_text)

        elif kind == 'app_update_available':
            _, latest, asset, sha = msg
            # don't restart in the middle of a run, wait until it's idle
            if self._running:
                self._pending_update = (latest, asset, sha)
            else:
                self._offer_update(latest, asset, sha)

        elif kind == 'scan_progress':
            info = msg[1]
            self._status_lbl.configure(
                text=f'Scanning your library... {info.get("songs", 0):,} songs')
            win = getattr(self, '_scan_win', None)
            if win is not None:
                win.update_info(info)

        elif kind == 'library_scanned':
            self._on_library_scanned(*msg[1:])

        elif kind == 'scan_failed':
            self._on_scan_failed(*msg[1:])

        elif kind == 'ytdlp_updated':
            _, ver = msg
            self._status_lbl.configure(
                text=f'Downloader updated ({ver}), active next launch')

    def destroy(self):
        self._stop_evt.set()
        super().destroy()


def run():
    """Called from VideoDownload.__main__ when running as the frozen exe."""
    # Tell Windows to group this process under its own taskbar identity,
    # not under python.exe - must be called before the window is created.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('BackstageHero.App')
    except Exception:
        pass

    updater._cleanup_old_exe()   # fast, no network, fine to call at startup

    app = App()
    app.update()                 # draw the window before dropping the splash
    try:
        import pyi_splash        # hand off from the bootloader splash to the window
        pyi_splash.close()
    except Exception:
        pass
    app.mainloop()


if __name__ == '__main__':
    run()
