"""
Build script -- produces BackstageHero.exe from VideoDownload.py.

Usage:
    python build.py

Requirements: Python 3.11+ (current yt-dlp needs it), internet connection
(first run only).
PyInstaller and Pillow are installed automatically if missing.
The finished exe appears in dist\\BackstageHero.exe.
"""

import subprocess
import sys
import os
import urllib.request
import zipfile


def pip_install(package):
    print(f'Installing {package}...')
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--quiet', package],
        check=True,
    )


if sys.version_info < (3, 11):
    print('WARNING: building with Python %d.%d. Current yt-dlp needs 3.11+, so this'
          % sys.version_info[:2])
    print('         exe will be stuck on its bundled yt-dlp and never auto-update it.')
    print('         Build with Python 3.11+ (e.g. "py -3.12 build.py") instead.\n')


# 1. Ensure PyInstaller is available

check = subprocess.run(
    [sys.executable, '-m', 'PyInstaller', '--version'],
    capture_output=True,
)
if check.returncode != 0:
    pip_install('pyinstaller')


# 2. Convert PNG icon to ICO (via subprocess so Pillow is always importable)

icon_png = os.path.join('assets', 'icon.png')
icon_ico = os.path.join('assets', 'icon.ico')
icon_flag = []

if os.path.exists(icon_png):
    convert = (
        'from PIL import Image; '
        f'img = Image.open({repr(icon_png)}); '
        f'img.save({repr(icon_ico)}, format="ICO", '
        'sizes=[(256,256),(128,128),(64,64),(32,32),(16,16)])'
    )
    result = subprocess.run([sys.executable, '-c', convert], capture_output=True)
    if result.returncode != 0:
        pip_install('pillow')
        subprocess.run([sys.executable, '-c', convert], check=True)
    print('Icon converted.')
    icon_flag = ['--icon', icon_ico]
else:
    print('Warning: assets/icon.png not found, building without custom icon.')


# 2b. Generate the brand art: header logo + the splash image shown by the
# PyInstaller bootloader while the onefile exe unpacks.

splash_png = os.path.join('assets', 'splash.png')
splash_flag = []
result = subprocess.run([sys.executable, 'make_brand.py'], capture_output=True)
if result.returncode != 0:
    pip_install('pillow')
    result = subprocess.run([sys.executable, 'make_brand.py'], capture_output=True)
if os.path.exists(splash_png):
    splash_flag = ['--splash', splash_png]
    print('Brand art (logo + splash) ready.')
else:
    print('Warning: could not build splash image; continuing without one.')


# 3. Bundle ffmpeg - download automatically if not already present.
# ffmpeg does audio sync and video remuxing. We bundle only ffmpeg.exe (the
# embedded mpv player handles preview playback, so ffplay isn't needed). The
# build is GPL, so its LICENSE text is shipped alongside the exe.
#
# For a reproducible build, pin a specific BtbN release by setting FFMPEG_URL,
# e.g. FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-YYYY-MM-DD-.../ffmpeg-...-win64-gpl.zip
FFMPEG_URL = os.environ.get(
    'FFMPEG_URL',
    'https://github.com/BtbN/ffmpeg-builds/releases/latest/download/'
    'ffmpeg-master-latest-win64-gpl.zip')
FFMPEG_LICENSE = 'ffmpeg-LICENSE.txt'

if not os.path.exists('ffmpeg.exe'):
    print('ffmpeg.exe not found - downloading (this takes a minute)...')
    tmp_zip = 'ffmpeg_tmp.zip'
    try:
        def _progress(count, block, total):
            if total > 0:
                print(f'\r  {min(100, count * block * 100 // total)}%', end='', flush=True)
        urllib.request.urlretrieve(FFMPEG_URL, tmp_zip, reporthook=_progress)
        print()
        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            member = next((n for n in names if os.path.basename(n) == 'ffmpeg.exe'), None)
            if member:
                with zf.open(member) as src, open('ffmpeg.exe', 'wb') as dst:
                    dst.write(src.read())
            # Ship the GPL licence text from the build.
            lic = next((n for n in names if os.path.basename(n).upper() == 'LICENSE.TXT'), None)
            if lic:
                with zf.open(lic) as src, open(FFMPEG_LICENSE, 'wb') as dst:
                    dst.write(src.read())
            if not os.path.exists('ffmpeg.exe'):
                raise RuntimeError('ffmpeg.exe not found inside downloaded zip')
        print('ffmpeg.exe ready.')
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

ffmpeg_flag = []
if os.path.exists('ffmpeg.exe'):
    ffmpeg_flag += ['--add-binary', 'ffmpeg.exe' + os.pathsep + '.']
    print('Bundling ffmpeg.exe.')
if os.path.exists(FFMPEG_LICENSE):
    ffmpeg_flag += ['--add-data', FFMPEG_LICENSE + os.pathsep + '.']


# 3b. Bundle libmpv (embedded preview in the sync editor, via python-mpv).
# Optional: if the DLL can't be obtained the app still builds and the sync
# editor falls back to launching ffplay.

LIBMPV = 'libmpv-2.dll'
if not os.path.exists(LIBMPV):
    print(f'{LIBMPV} not found - fetching a libmpv dev build...')
    try:
        import json
        req = urllib.request.Request(
            'https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest',
            headers={'User-Agent': 'BackstageHero-build'})
        rel = json.loads(urllib.request.urlopen(req, timeout=30).read())
        asset = next(a for a in rel['assets']
                     if a['name'].startswith('mpv-dev-x86_64-')
                     and a['name'].endswith('.7z'))
        tmp7z = 'mpv_dev_tmp.7z'
        urllib.request.urlretrieve(asset['browser_download_url'], tmp7z)
        # zhongfly archives use the BCJ2 filter, which py7zr can't decode, so
        # shell out to whichever 7z/WinRAR CLI is installed.
        for tool, args in (
                (r'C:\Program Files\7-Zip\7z.exe', ['e', '-y']),
                (r'C:\Program Files\WinRAR\WinRAR.exe', ['x', '-y', '-ibck'])):
            if os.path.exists(tool):
                subprocess.run([tool, *args, tmp7z, LIBMPV, '.' + os.sep],
                               check=False)
                if os.path.exists(LIBMPV):
                    break
        if os.path.exists(tmp7z):
            os.remove(tmp7z)
        if not os.path.exists(LIBMPV):
            print(f'  Could not extract {LIBMPV} (need 7-Zip or WinRAR).')
            print(f'  Drop {LIBMPV} into this folder and rebuild to enable it.')
    except Exception as e:
        print(f'  libmpv fetch failed ({e}); building without the embedded preview.')

mpv_flag = []
if os.path.exists(LIBMPV):
    mpv_flag = ['--add-binary', LIBMPV + os.pathsep + '.']
    print(f'Bundling {LIBMPV}.')
else:
    print('Building WITHOUT libmpv - sync editor will fall back to ffplay.')


# 4. Run PyInstaller

print('\nBuilding BackstageHero.exe...\n')
subprocess.run(
    [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',                      # single portable exe
        '--noconfirm',                    # overwrite previous build without asking
        '--noconsole',                    # GUI app - no terminal window
        # collect-submodules gets yt-dlp's dynamically imported extractors
        # without dragging in package data the way collect-all does. numpy
        # needs nothing extra - PyInstaller's own hook handles its binaries,
        # and collect-all was stuffing headers and test machinery into the exe
        # that got unpacked on every launch.
        '--collect-submodules', 'yt_dlp',
        '--collect-all', 'customtkinter', # themes/assets are real runtime data
        '--hidden-import', 'gui',         # GUI module (conditional import)
        '--hidden-import', 'audiosync',   # fingerprint sync module
        '--hidden-import', 'mpv',         # python-mpv (loaded dynamically in gui)
        '--add-data', 'assets' + os.pathsep + 'assets',  # icon + other assets
        '--name', 'BackstageHero',
        *ffmpeg_flag,
        *mpv_flag,
        *icon_flag,
        *splash_flag,
        'VideoDownload.py',
    ],
    check=True,
)


# 5. Write a SHA-256 sidecar
# The in-app updater verifies a downloaded exe against this hash when it is
# published next to the release asset. Upload dist\BackstageHero.exe.sha256
# alongside the exe and auto-update gains a tamper check on top of HTTPS.

import hashlib

exe_path = os.path.join('dist', 'BackstageHero.exe')
sha_path = exe_path + '.sha256'
if os.path.exists(exe_path):
    h = hashlib.sha256()
    with open(exe_path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    with open(sha_path, 'w') as f:
        f.write(h.hexdigest() + '  BackstageHero.exe\n')
    print('Wrote checksum -->  ' + sha_path)


# 6. Done

print('\n' + '-' * 60)
print('Build complete!  -->  dist\\BackstageHero.exe')
print('-' * 60)
print('To run:')
print('  Launch dist\\BackstageHero.exe from anywhere. It asks for your Clone')
print('  Hero Songs folder on first run. ffmpeg is bundled - nothing to install.')
print('\nTo release (so auto-update reaches users):')
print('  Tag the GitHub release with a version newer than the previous tag, then')
print('  upload BOTH dist\\BackstageHero.exe and dist\\BackstageHero.exe.sha256.')
print('-' * 60)
