# Two update channels: the app exe (from GitHub releases) and yt-dlp (from PyPI).
# yt-dlp updates separately because YouTube breaks it every few weeks, way more
# often than we cut releases. Both only run when frozen - source installs use pip.

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

log = logging.getLogger('backstagehero')

GITHUB_REPO = 'AARP41298/BackstageHero'
EXE_ASSET_NAME = 'BackstageHero.exe'

_API_LATEST = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
_PYPI_YTDLP = 'https://pypi.org/pypi/yt-dlp/json'
_UA = 'BackstageHero-Updater'

_YTDLP_CHECK_INTERVAL = 20 * 3600  # ~daily
_META_TIMEOUT = 5    # short timeout for metadata fetches so a bad connection doesn't hang
_DL_TIMEOUT   = 120  # socket timeout for binary downloads (exe / wheel)
_META_MAX     = 5 << 20    # metadata responses have no business being bigger than this
_DL_MAX       = 500 << 20  # hard ceiling on any single download


def _frozen():
    return getattr(sys, 'frozen', False)


def data_dir():
    """Per-user data folder for BackstageHero (cache, settings, etc)."""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    path = os.path.join(base, 'BackstageHero')
    os.makedirs(path, exist_ok=True)
    return path


def _ytdlp_dir():
    """Where the cached yt-dlp package lives."""
    return os.path.join(data_dir(), 'ytdlp')


def _ver_tuple(v):
    """Version string to comparable tuple. Strips leading non-digits so 'v.1.0.3' works."""
    return tuple(int(n) for n in re.findall(r'\d+', v or ''))


def _app_ver(v):
    """App version/tag to comparable key. A pre-release sorts before its release,
    so 'v2.2.1-rc1' < '2.2.1' < 'v2.2.2'."""
    v = v or ''
    m = re.search(r'\d+(?:\.\d+)*', v)
    if not m:
        return ((), 1, 0)
    base = tuple(int(n) for n in m.group().split('.'))
    pre = re.search(r'\d+', v[m.end():])
    return (base, 0, int(pre.group())) if pre else (base, 1, 0)


def _get_json(url, timeout=_META_TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': _UA,
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(_META_MAX).decode('utf-8', 'replace'))


def prefer_cached_ytdlp():
    """Call before importing yt_dlp. Puts the cached newer version on sys.path if one exists."""
    if not _frozen():
        return
    try:
        cache = _ytdlp_dir()
        staged = cache + '.new'
        # promote staged update before yt_dlp imports - can't swap a live package mid-run
        if os.path.isdir(os.path.join(staged, 'yt_dlp')):
            _swap_dir(staged, cache)
        if os.path.isfile(os.path.join(cache, 'yt_dlp', '__init__.py')):
            sys.path.insert(0, cache)
            try:
                import yt_dlp  # noqa: F401  - prove the cached copy loads here
            except Exception:
                # e.g. a newer yt-dlp that needs a newer Python than this exe
                # was built with. Drop it and fall back to the bundled copy.
                log.warning('cached yt-dlp failed to import; using bundled copy',
                            exc_info=True)
                sys.path.remove(cache)
                for name in [m for m in sys.modules
                             if m == 'yt_dlp' or m.startswith('yt_dlp.')]:
                    del sys.modules[name]
                shutil.rmtree(cache, ignore_errors=True)
    except Exception:
        pass


def _python_ok(spec):
    """True if this interpreter satisfies a PyPI requires_python spec like '>=3.11'.
    Only lower bounds matter here; anything we can't parse is treated as OK."""
    for m in re.finditer(r'(>=|>)\s*(\d+(?:\.\d+)*)', spec or ''):
        need = _ver_tuple(m.group(2))
        have = tuple(sys.version_info[:len(need)])
        if have < need or (m.group(1) == '>' and have == need):
            return False
    return True


def _wheel_url_and_version():
    """(download_url, version, sha256) for the latest yt-dlp wheel on PyPI, or None.
    PyPI publishes the digest in the same response, so downloads get verified."""
    info = _get_json(_PYPI_YTDLP)
    version = info['info']['version']
    if not _python_ok(info['info'].get('requires_python')):
        log.info('yt-dlp %s needs Python %s; this build is %d.%d - not updating',
                 version, info['info'].get('requires_python'), *sys.version_info[:2])
        return None
    for entry in info.get('urls', []):
        if entry.get('packagetype') == 'bdist_wheel':
            if not _python_ok(entry.get('requires_python')):
                continue
            sha = (entry.get('digests') or {}).get('sha256') or ''
            return entry['url'], version, sha.lower()
    return None


def _swap_dir(staging, target):
    """Atomically replace directory `target` with `staging` (same volume)."""
    backup = target + '.bak'
    if os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    if os.path.exists(target):
        os.rename(target, backup)
    try:
        os.rename(staging, target)
    except Exception:
        if os.path.exists(backup):       # restore on failure
            os.rename(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def maybe_update_ytdlp(current_version):
    """Check PyPI for a newer yt-dlp and stage it for the next launch. At most once a day.
    Returns the new version string if something was staged, else None."""
    if not _frozen():
        return None
    try:
        stamp = os.path.join(data_dir(), 'ytdlp_last_check')
        now = time.time()
        if os.path.exists(stamp) and now - os.path.getmtime(stamp) < _YTDLP_CHECK_INTERVAL:
            return None
        # write the stamp first so a failed check still waits a day before retrying
        with open(stamp, 'w') as f:
            f.write(str(int(now)))

        found = _wheel_url_and_version()
        if not found:
            return None
        url, latest, sha = found
        if _ver_tuple(latest) <= _ver_tuple(current_version):
            return None
        if not sha:
            log.warning('PyPI gave no digest for the yt-dlp wheel; skipping update')
            return None

        work = tempfile.mkdtemp(prefix='bh_ytdlp_')
        try:
            wheel = os.path.join(work, 'ytdlp.whl')
            _download(url, wheel)
            if _sha256_of(wheel) != sha:
                log.warning('yt-dlp wheel failed its digest check; skipping update')
                return None
            staging = os.path.join(work, 'extract')
            os.makedirs(staging)
            with zipfile.ZipFile(wheel) as zf:
                for member in zf.namelist():
                    if member.startswith('yt_dlp/'):
                        zf.extract(member, staging)
            if not os.path.isdir(os.path.join(staging, 'yt_dlp')):
                return None
            # stage to .new - gets promoted on next launch before yt_dlp is imported
            _swap_dir(staging, _ytdlp_dir() + '.new')
            return latest
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Exception:
        log.warning('yt-dlp update check failed', exc_info=True)
        return None


def _download(url, dest, progress_cb=None):
    """Download a URL to a file. progress_cb(got, total) is called as bytes arrive."""
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=_DL_TIMEOUT) as resp, open(dest, 'wb') as out:
        total = int(resp.headers.get('Content-Length') or 0)
        got = 0
        if progress_cb:
            progress_cb(0, total)
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if got > _DL_MAX:
                raise IOError('download exceeded the size limit')
            if progress_cb:
                progress_cb(got, total)


def _find_release_asset(release, name):
    for asset in release.get('assets', []):
        if asset.get('name') == name:
            return asset.get('browser_download_url')
    return None


def _expected_sha256(release):
    """Get the expected SHA-256 from a .sha256 sidecar or the release body. Returns None if not found."""
    try:
        side = _find_release_asset(release, EXE_ASSET_NAME + '.sha256')
        if side:
            req = urllib.request.Request(side, headers={'User-Agent': _UA})
            with urllib.request.urlopen(req, timeout=_META_TIMEOUT) as resp:
                text = resp.read().decode('utf-8', 'replace')
            m = re.search(r'[0-9a-fA-F]{64}', text)
            if m:
                return m.group(0).lower()
        m = re.search(r'[0-9a-fA-F]{64}', release.get('body') or '')
        if m:
            return m.group(0).lower()
    except Exception:
        pass
    return None


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _prompt_yes(message, timeout=5.0, default=True):
    """Prompt with a timeout. Returns default if the user doesn't respond in time."""
    print(message, end='', flush=True)
    box = [None]

    def _read():
        try:
            box[0] = sys.stdin.readline()
        except Exception:
            box[0] = ''

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    ans = box[0]
    if ans is None:          # timed out
        print()
        return default
    ans = ans.strip().lower()
    if ans == '':
        return default
    return ans in ('y', 'yes')


def _cleanup_old_exe():
    """Delete the previous exe left behind by an earlier self-update."""
    try:
        old = sys.executable + '.old'
        if os.path.exists(old):
            os.remove(old)
    except Exception:
        pass


def _apply_app_update(asset_url, expected_sha, status_cb=None):
    """Download the new exe, verify the checksum, swap it in and relaunch.
    Returns False if anything fails - the current exe is left alone.

    status_cb(stage, **info) reports progress for a UI. Stages:
      'download' (got, total) | 'verify' | 'install' | 'restart' | 'error' (msg)
    """
    def emit(stage, **info):
        if status_cb:
            try:
                status_cb(stage, **info)
            except Exception:
                pass

    cur = sys.executable
    work = tempfile.mkdtemp(prefix='bh_app_')
    new = os.path.join(work, EXE_ASSET_NAME)
    try:
        # no checksum, no install. every release ships a .sha256 sidecar, so
        # if it's missing the release page is broken or someone is messing
        # with it - either way not something to run.
        if not expected_sha:
            emit('error', msg='No checksum published for this update.')
            print('  Update has no checksum to verify against - keeping the current version.')
            return False

        emit('download', got=0, total=0)
        _download(asset_url, new,
                  progress_cb=lambda got, total: emit('download', got=got, total=total))
        emit('verify')
        if _sha256_of(new) != expected_sha:
            emit('error', msg='Downloaded file failed its integrity check.')
            print('  Update checksum did not match - keeping the current version.')
            return False

        emit('install')
        old = cur + '.old'
        if os.path.exists(old):
            try:
                os.remove(old)
            except Exception:
                pass
        os.rename(cur, old)
        try:
            shutil.move(new, cur)
        except Exception:
            os.rename(old, cur)  # put it back if the move fails
            raise
    except Exception as e:
        emit('error', msg=str(e))
        print(f'  Could not install the update ({e}). Continuing with the current version.')
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)

    emit('restart')
    print('  Update installed. Restarting...')
    try:
        subprocess.Popen([cur], cwd=os.getcwd(), close_fds=True)
    except Exception:
        pass
    return True  # caller is responsible for exiting the current process


def check_app_update(current_version):
    """Check GitHub for a newer release. Returns (latest_tag, asset_url, sha256) if one
    exists, or None if already current / offline / any error."""
    if not _frozen():
        return None
    try:
        release = _get_json(_API_LATEST)
        tag = release.get('tag_name') or ''
        if _app_ver(tag) <= _app_ver(current_version):
            return None
        asset = _find_release_asset(release, EXE_ASSET_NAME)
        if not asset:
            return None
        sha = _expected_sha256(release)
        if not sha:
            # nothing to verify the download against - don't even offer it
            log.warning('Release %s has no checksum; not offering the update', tag)
            return None
        latest = re.sub(r'^\D*', '', tag) or tag
        return latest, asset, sha
    except Exception:
        log.warning('App update check failed', exc_info=True)
        return None


def apply_app_update(asset_url, expected_sha, status_cb=None):
    """Download the new exe, verify it, swap it in place and relaunch.
    Returns False on any failure (current exe untouched). status_cb reports
    progress stages, see _apply_app_update."""
    return _apply_app_update(asset_url, expected_sha, status_cb=status_cb)


def run_startup_updates(app_version, ytdlp_version):
    """Legacy entry-point kept for the CLI path. GUI uses check_app_update instead."""
    _cleanup_old_exe()
    info = check_app_update(app_version)
    if info:
        latest, asset, sha = info
        if _prompt_yes(
                f'\nUpdate available: v{app_version} -> v{latest}. '
                'Install now? [Y/n] '):
            _apply_app_update(asset, sha)
        else:
            print('  Skipping update for now.')
    staged = maybe_update_ytdlp(ytdlp_version)
    if staged:
        print(f'  Updated downloader (yt-dlp {staged}) - active next launch.')
