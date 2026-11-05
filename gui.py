# BackstageHero GUI.
# All download/sync logic lives in VideoDownload.py; this file owns the window.

import os
import sys
import glob
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
    probe_video_height,
)
import conpack
from concurrent.futures import ThreadPoolExecutor, as_completed
import updater
import resolver_client

log = logging.getLogger('backstagehero')

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('blue')


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
    'collage': ('▦',    'Show frame collage'),
    'search':  ('↻',    'Wrong video: search YouTube again'),
    'mp4':     ('MP4',  'Render to MP4 (fast) and approve this video'),
    'webm':    ('WebM', 'Render to WebM (smaller, slow) and approve this video'),
}
_ACTION_KEYS = tuple(_ACTIONS)
_ACTION_COLS = tuple('act_' + k for k in _ACTION_KEYS)
_ACTION_W = 46

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
    """Returns (ok: bool, message: str)."""
    if not path or not os.path.isdir(path):
        return False, 'That path does not exist or is not a folder.'

    # They picked an individual song folder (song.ini at the root level)
    if os.path.exists(os.path.join(path, 'song.ini')):
        return False, (
            'That looks like an individual song folder, not your Songs library.\n\n'
            'Please select the folder that contains all your song packs, '
            'not a folder inside one of them.')

    # Check for any song.ini anywhere inside, or a packed CON/LIVE file (YARG)
    found = next(
        glob.iglob(os.path.join(glob.escape(path), '**', 'song.ini'),
                   recursive=True), None)
    if not found and not conpack.find_con_files(path):
        return False, (
            'No songs found in that folder.\n\n'
            'Make sure you\'re selecting your Clone Hero / YARG Songs folder.\n'
            'It should contain song folders with a song.ini file inside, '
            'or Rock Band CON files.')

    return True, ''


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
    review   : object = None   # CON download waiting for review (conpack.load_review)
    rendering: str  = ''       # '' | 'queued' | 'running'

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


def _scan_library(songs_folder, progress=None):
    """Return a list[Song] for the folder, sorted alphabetically.
    progress(count) is called periodically so the UI can show a live tally."""
    songs = []
    for ini in glob.iglob(
            os.path.join(glob.escape(songs_folder), '**', 'song.ini'),
            recursive=True):
        folder = os.path.dirname(ini)
        artist, title, stored = scan_song(folder)
        label = build_query(artist, title) or os.path.basename(folder)
        has_vid = os.path.exists(os.path.join(folder, 'video.mp4'))
        res = '-'
        if has_vid:
            res = stored if stored else '...'   # '...' = needs probing
        songs.append(Song(
            filename=ini, folder=folder,
            label=label, key=label.lower(),
            has_video=has_vid, res=res))
        if progress and len(songs) % 50 == 0:
            progress(len(songs))

    # packed Rock Band CONs: one row per song inside, listed through Nautilus
    cons = conpack.find_con_files(songs_folder)
    con_errors = {}
    if cons and conpack.nautilus_ready():
        listing, con_errors = conpack.list_songs(
            cons, progress=(lambda d, t: progress(f'reading CON files {d}/{t}'))
            if progress else None)
        for con, entries in listing.items():
            pack = os.path.basename(con)
            for e in entries:
                label = (build_query(e['artist'], e['name']) or e['id']) + f'   [{pack}]'
                has_vid = conpack.existing_video(con, e['id']) is not None
                review = conpack.load_review(con, e['id'])
                songs.append(Song(
                    filename=con, folder=os.path.dirname(con),
                    label=label, key=label.lower(),
                    has_video=has_vid, res='...' if has_vid else '-',
                    con=con, song_id=e['id'], artist=e['artist'], title=e['name'],
                    review=review,
                    status='◆  Review download' if review else '',
                    stag='warn' if review else ''))
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
                     font=ctk.CTkFont(size=14, weight='bold'),
                     text_color=_TEXT, anchor='w').pack(
            side='left', padx=18, fill='y')
        res = self._song.res if self._song.res not in ('-', '...', '') else '?'
        ctk.CTkLabel(hdr, text=res,
                     font=ctk.CTkFont(size=11),
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
            font=ctk.CTkFont(size=30, weight='bold'), text_color=_BLUE)
        self._ms_lbl.pack(side='left')
        # live indicator, only when ffplay is around
        if ffplayPath:
            self._live_lbl = ctk.CTkLabel(
                top_row, text='',
                font=ctk.CTkFont(size=10), text_color=_SUBTEXT)
            self._live_lbl.pack(side='right', padx=4, pady=(8, 0))
        else:
            self._live_lbl = None
        self._desc_lbl = ctk.CTkLabel(
            card, text='',
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT)
        self._desc_lbl.pack(pady=(2, 12))

        # Slider
        sf = ctk.CTkFrame(self, fg_color='transparent')
        sf.pack(fill='x', padx=20, pady=(14, 0))
        ctk.CTkLabel(sf, text='-30s', font=ctk.CTkFont(size=10),
                     text_color=_SUBTEXT).pack(side='left')
        ctk.CTkLabel(sf, text='+90s', font=ctk.CTkFont(size=10),
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
                  text_color=_TEXT, font=ctk.CTkFont(size=10))
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
                text_color=_TEXT, font=ctk.CTkFont(size=11),
                command=self._toggle_play)
            self._play_btn.pack(side='left')
            ctk.CTkButton(
                prev_row, text='↻  Restart', width=100, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=ctk.CTkFont(size=11),
                command=self._restart_at_song).pack(side='left', padx=(8, 0))
            ctk.CTkLabel(prev_row,
                         text='Drag the slider, sync updates live as it plays.',
                         font=ctk.CTkFont(size=10),
                         text_color=_SUBTEXT).pack(side='right')
        elif ffplayPath:
            ctk.CTkButton(
                prev_row, text='▶  Preview', width=110, height=28,
                fg_color='#2a2a3e', hover_color='#383858',
                text_color=_TEXT, font=ctk.CTkFont(size=11),
                command=self._launch_preview).pack(side='left')
        else:
            ctk.CTkLabel(prev_row, text='Player not bundled. Adjust the offset and Save.',
                         font=ctk.CTkFont(size=10),
                         text_color=_SUBTEXT).pack(side='left')

        # Divider
        ctk.CTkFrame(self, fg_color=_BORDER, height=1).pack(
            fill='x', padx=20, pady=14)

        # Share
        shr = ctk.CTkFrame(self, fg_color='transparent')
        shr.pack(fill='x', padx=20)
        ctk.CTkCheckBox(shr, text='Share with community',
                        variable=self._share,
                        font=ctk.CTkFont(size=12), text_color=_TEXT,
                        checkbox_width=18, checkbox_height=18,
                        checkmark_color=_BG, fg_color=_BLUE,
                        hover_color='#7aaef8').pack(anchor='w')
        ctk.CTkLabel(shr,
                     text='Once enough users confirm the same offset it becomes\n'
                          'the default for this chart, no fingerprinting needed.',
                     font=ctk.CTkFont(size=10), text_color=_SUBTEXT,
                     justify='left', anchor='w').pack(anchor='w', pady=(4, 0))

        # Cancel / Save
        foot = ctk.CTkFrame(self, fg_color='transparent')
        foot.pack(fill='x', padx=20, pady=(14, 20))
        ctk.CTkButton(foot, text='Cancel', width=110,
                      fg_color='transparent', border_width=1,
                      border_color=_BORDER, hover_color='#30304a',
                      text_color=_SUBTEXT, font=ctk.CTkFont(size=12),
                      command=self._close).pack(side='left')
        ctk.CTkButton(foot, text='Save', width=110,
                      fg_color=_BLUE, hover_color='#7aaef8',
                      text_color='#11111b',
                      font=ctk.CTkFont(size=13, weight='bold'),
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
                     font=ctk.CTkFont(size=15, weight='bold'),
                     text_color=_TEXT).pack(padx=24, pady=(22, 4), anchor='w')
        self._stat = ctk.CTkLabel(self, text='Preparing...',
                                  font=ctk.CTkFont(size=12), text_color=_SUBTEXT)
        self._stat.pack(padx=24, anchor='w')
        self._bar = ctk.CTkProgressBar(self, width=372, height=14,
                                       progress_color=_BLUE)
        self._bar.set(0)
        self._bar.pack(padx=24, pady=(12, 0))
        self._bar.configure(mode='indeterminate')
        self._bar.start()
        self._detail = ctk.CTkLabel(self, text='',
                                    font=ctk.CTkFont(size=10), text_color=_SUBTEXT)
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
                         font=ctk.CTkFont(size=17, weight='bold'),
                         text_color=_TEXT).grid(row=0, column=0, padx=(16, 6), pady=14)
        ctk.CTkLabel(hdr,
                     text=f'v{__version__}',
                     font=ctk.CTkFont(size=11),
                     text_color=_SUBTEXT).grid(row=0, column=1, pady=14,
                                               padx=(0, 16), sticky='w')

        folder_row = ctk.CTkFrame(hdr, fg_color='transparent')
        folder_row.grid(row=0, column=2, padx=(0, 12), sticky='e')
        folder_row.grid_columnconfigure(0, weight=1)
        self._folder_lbl = ctk.CTkLabel(
            folder_row, text='No folder selected',
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT, anchor='e')
        self._folder_lbl.grid(row=0, column=0, padx=(0, 10), sticky='e')
        ctk.CTkButton(folder_row, text='Change folder',
                      width=115, height=28, font=ctk.CTkFont(size=11),
                      command=self._pick_folder).grid(row=0, column=1)

        # Filter / search bar
        fbar = ctk.CTkFrame(self, fg_color=_SURFACE, corner_radius=0, height=50)
        fbar.grid(row=1, column=0, sticky='ew', pady=(1, 0))
        fbar.grid_propagate(False)
        # Column 3 is a spacer that absorbs leftover space;
        # column 4 is the search box pinned to the right.
        fbar.grid_columnconfigure(3, weight=1)

        _btn = dict(height=30, width=130, font=ctk.CTkFont(size=12),
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
                       fieldbackground=_BG, rowheight=30,
                       font=('Segoe UI', 11), borderwidth=0, relief='flat')
        sty.configure('BH.Treeview.Heading',
                       background='#181825', foreground=_SUBTEXT,
                       font=('Segoe UI', 10, 'bold'),
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
            columns=('check', 'label', 'res', 'status') + _ACTION_COLS,
            show='headings', selectmode='none')

        self._tree.heading('check',  text='',
                           anchor='center', command=self._toggle_all)
        self._tree.heading('label',  text='Song / Artist  ↕',
                           anchor='w',      command=lambda: self._sort('label'))
        self._tree.heading('res',    text='Resolution  ↕',
                           anchor='center', command=lambda: self._sort('res'))
        self._tree.heading('status', text='Status', anchor='w')

        self._tree.column('check',  width=36,  minwidth=36,  anchor='center', stretch=False)
        self._tree.column('label',  width=520, minwidth=200, anchor='w',      stretch=False)
        self._tree.column('res',    width=95,  minwidth=70,  anchor='center', stretch=False)
        self._tree.column('status', width=170, minwidth=100, anchor='w',      stretch=False)
        for k, col in enumerate(_ACTION_COLS):
            self._tree.heading(col, text='Actions' if k == 0 else '', anchor='w')
            self._tree.column(col, width=_ACTION_W, minwidth=_ACTION_W,
                              anchor='center', stretch=False)
        # Song 60% / Resolution 20% / Status 20% of the width left after the
        # checkbox and the action icons
        self._tree.bind('<Configure>', self._size_columns, add='+')
        self._tree.bind('<Motion>', self._on_tree_motion, add='+')

        self._tree.tag_configure('done',  foreground=_GREEN)
        self._tree.tag_configure('error', foreground=_RED)
        self._tree.tag_configure('busy',  foreground=_BLUE)
        self._tree.tag_configure('warn',  foreground=_YELLOW)
        self._tree.tag_configure('dim',   foreground=_SUBTEXT)
        # Slightly more contrast between rows so a long list is easy to scan
        self._tree.tag_configure('row_even', background='#272740')
        self._tree.tag_configure('row_odd',  background='#1e1e2e')

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
        _font  = ctk.CTkFont(size=12)
        _font_bold = ctk.CTkFont(size=13, weight='bold')

        self._sel_btn = ctk.CTkButton(
            foot, text='Select all', width=105, font=_font,
            fg_color=_BORDER, hover_color='#44445a', text_color=_TEXT,
            command=self._select_all_visible, **_fbtn)
        self._sel_btn.grid(row=0, column=0, padx=(12, 4), pady=15)

        self._clr_btn = ctk.CTkButton(
            foot, text='Clear all', width=90, font=_font,
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
            font=_font).grid(row=0, column=2, padx=4, pady=15)

        self._start_btn = ctk.CTkButton(
            foot, text='▶  Search & Download', width=175, font=_font_bold,
            command=self._start_download, **_fbtn)
        self._start_btn.grid(row=0, column=3, padx=4, pady=15)

        self._resync_btn = ctk.CTkButton(
            foot, text='↺  Auto-sync', width=120, font=_font,
            fg_color='#313244', hover_color='#414160', text_color=_TEXT,
            command=self._start_resync, **_fbtn)
        self._resync_btn.grid(row=0, column=4, padx=4, pady=15)

        self._stop_btn = ctk.CTkButton(
            foot, text='■  Stop', width=90, font=_font,
            fg_color='#4a1a2a', hover_color='#6a2a3e', text_color=_RED,
            state='disabled', command=self._stop, **_fbtn)
        self._stop_btn.grid(row=0, column=5, padx=4, pady=15)

        # Opt-in/out of contributing confirmed matches back to the community pool.
        self._share_var = tk.BooleanVar(
            value=self._settings.get('share_matches', True))
        share_cb = ctk.CTkCheckBox(
            foot, text='Share matches', variable=self._share_var,
            font=ctk.CTkFont(size=11), text_color=_SUBTEXT,
            checkbox_width=18, checkbox_height=18,
            command=self._on_share_toggle)
        share_cb.grid(row=0, column=6, padx=(12, 4), pady=15)

        # Progress + status (right side of footer)
        prog_frame = ctk.CTkFrame(foot, fg_color='transparent')
        prog_frame.grid(row=0, column=7, padx=(8, 16), pady=15, sticky='e')

        self._progress = ctk.CTkProgressBar(prog_frame, width=190, height=8,
                                             corner_radius=4)
        self._progress.set(0)
        self._progress.pack(pady=(2, 4))

        self._status_lbl = ctk.CTkLabel(
            prog_frame, text='Ready',
            font=ctk.CTkFont(size=10), text_color=_SUBTEXT,
            anchor='e', width=190)
        self._status_lbl.pack()

        self._update_buttons()

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
                _save_songs_path(path)
                self._load_library(path)
                return

            messagebox.showerror('Invalid Songs folder', msg +
                                 '\n\nPlease try again.')

    def _load_library(self, path):
        self._songs_folder = path
        display = path if len(path) < 58 else '...' + path[-55:]
        self._folder_lbl.configure(text=f'Songs:  {display}')
        self._status_lbl.configure(text='Scanning your library...')

        # Make sure the queue is being polled before the scan thread posts to it.
        if not self._polling:
            self._polling = True
            self.after(200, self._poll_queue)

        # Scan off the UI thread so the window stays responsive on big libraries.
        def _worker():
            songs, cons, con_errors = _scan_library(
                path, progress=lambda c: self._queue.put(('scan_progress', c)))
            self._queue.put(('library_scanned', songs, cons, con_errors))
        threading.Thread(target=_worker, daemon=True).start()

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

    def _on_library_scanned(self, songs, cons=(), con_errors=None):
        if cons and not conpack.nautilus_ready() and not self._nautilus_asked:
            self._nautilus_asked = True
            if self._ask_nautilus(len(cons)):
                self._load_library(self._songs_folder)
                return
        self._songs = songs
        self._apply_filter()
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
            futures = {(pool.submit(probe_video_height, s.video) if s.con
                        else pool.submit(probe_resolution, s.folder)): s
                       for s in songs}
            for fut in as_completed(futures):
                if self._stop_evt.is_set():
                    break
                s = futures[fut]
                try:
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
        self._apply_filter()

    def _size_columns(self, event):
        avail = event.width - 36 - 2 - _ACTION_W * len(_ACTION_COLS)
        if avail < 300:
            return
        res = status = int(avail * 0.2)
        self._tree.column('label',  width=avail - res - status)
        self._tree.column('res',    width=res)
        self._tree.column('status', width=status)

    # ---- actions column ------------------------------------------------------

    def _actions_of(self, s):
        """Which action icons are enabled for a song."""
        if s.rendering:                      # rendering: everything locked
            return set()
        if s.review:                         # downloaded, waiting for review
            return {'play', 'collage', 'search', 'mp4', 'webm'}
        if s.has_video:                      # finished video
            return {'play', 'search'}
        return set()

    def _row_values(self, s):
        chk         = '  ☑' if s.checked else '  ☐'
        status_text = s.status or ('✔' if s.has_video else '✗')
        enabled = self._actions_of(s)
        icons = tuple(_ACTIONS[a][0] if a in enabled else '' for a in _ACTION_KEYS)
        return (chk, s.label, s.res or '-', status_text) + icons

    def _row_tags(self, s, row_tag):
        stag = s.stag or ('dim' if s.has_video else 'error')
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
        return s, key

    def _on_tree_motion(self, event):
        s, key = self._action_at(event)
        hint = ''
        if s and key and key in self._actions_of(s):
            hint = _ACTIONS[key][1]
            if key == 'play' and s.review:
                hint = 'Play the downloaded video (not cut yet)'
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
            if key:
                if key in self._actions_of(s):
                    self._do_action(s, key)
                return
            s.checked = not s.checked
            self._update_row(s)
            self._update_buttons()

    def _do_action(self, s, key):
        if key == 'play':
            path = s.review['raw'] if s.review else s.video
            if os.path.exists(path):
                _open_in_file_manager(path)     # opens files in the default player
        elif key == 'collage':
            self._show_collage(s)
        elif key == 'search':
            self._search_again(s)
        elif key in ('mp4', 'webm'):
            self._queue_render(s, key)

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
        if s.review:
            if not messagebox.askyesno(
                    'Search again?',
                    f'{s.label}\n\nMark this video as wrong and search YouTube again?\n'
                    '(it will be skipped in future searches)'):
                return
            conpack.mark_bad(s.con, s.song_id, s.review['video_id'])
            conpack.discard_work(s.con, s.song_id)
            s.review = None
            self._enqueue_downloads([s], replace=True)
            return
        # finished video: same checks as Re-download (protects hand-made videos)
        if not self._confirm_redownload([s]):
            return
        tag = conpack.read_tag(s.video) or ''
        m = re.search(r'source=(\S+)', tag)
        if m:
            conpack.mark_bad(s.con, s.song_id, m.group(1))
        self._enqueue_downloads([s], replace=True)

    def _queue_render(self, s, fmt):
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

        self._start_btn.configure(
            text=f'▶  Search & Download ({n})' if n else '▶  Search & Download',
            state='normal' if can_start else 'disabled')
        self._resync_btn.configure(
            state='normal' if can_resync else 'disabled')
        self._stop_btn.configure(
            state='normal' if self._running else 'disabled')

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
                progress=lambda text, frac, s=s, i=i: self._queue.put(
                    ('song_progress', s, i, total, text, frac)))

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
                self._queue.put(('song_review', s, i, total))
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

        elif kind == 'song_review':
            _, s, i, total = msg
            synced = s.review and s.review.get('matched')
            s.status = '◆  Review download' + ('' if synced else ' (not synced)')
            s.stag   = 'warn'
            self._update_row(s)
            self._progress.set((i + 1) / total)

        elif kind == 'collage_ready':
            _, s, result, err = msg
            s.status = '◆  Review download' if s.review else ''
            s.stag   = 'warn' if s.review else ''
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
                s.review    = None
                s.has_video = True
                s.res       = probe_video_height(dest) or '?'
                s.status    = f'✔  Rendered {os.path.splitext(dest)[1][1:].upper()}'
                s.stag      = 'done'
            self._update_row(s)

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
            what = msg[1] if isinstance(msg[1], str) else f'{msg[1]} songs'
            self._status_lbl.configure(text=f'Scanning your library... {what}')

        elif kind == 'library_scanned':
            self._on_library_scanned(*msg[1:])

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
