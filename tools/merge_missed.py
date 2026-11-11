"""
Merge missed_entries.json files sent by users into the database (mvdb/).

    python tools/merge_missed.py missed_entries.json [more.json ...]
    python tools/merge_missed.py --dry-run "S:\\Downloads\\missed_entries.json"

For each entry it prints what happened:

    +  new        video added (to a new or an existing song)
    ~  filled     the database had this video; filled in missing fields
                  (offset, auto-edit, crop, title...)
    =  same       already in the database
    !  conflict   the database has this video with another type or offset:
                  kept as is, fix it by hand if the submission is right
    x  invalid    no YouTube ID, or no title / con_id

Only the touched sources are written. Review `git diff mvdb/` before
committing: merging trusts the file. See MISSED_ENTRIES.md.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mvdb  # noqa: E402

MARK = {'new': '+', 'filled': '~', 'same': '=', 'conflict': '!', 'invalid': 'x'}


def _describe(e):
    song, video = e.get('song') or {}, e.get('video') or {}
    name = f"{song.get('artist', '?')} - {song.get('title', song.get('con_id', '?'))}"
    extra = []
    if 'offset_ms' in video:
        extra.append(f"offset {video['offset_ms']} ms")
    if video.get('autoedit'):
        extra.append('auto-edit')
    return (f"{name}  [{video.get('type', 'official')}] {video.get('youtube', '')}"
            + (f"  ({', '.join(extra)})" if extra else ''))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Merge missed_entries.json files into mvdb/.')
    ap.add_argument('files', nargs='+', help='missed_entries.json files')
    ap.add_argument('-n', '--dry-run', action='store_true', help="show, don't write")
    ap.add_argument('-q', '--quiet', action='store_true', help='only show new, filled, conflicts')
    args = ap.parse_args(argv)

    db = mvdb.load()
    touched = set()
    totals = dict.fromkeys(MARK, 0)
    for path in args.files:
        report = []
        try:
            mvdb.merge_submission(db, path, report)
        except Exception as ex:
            print(f'{path}: {ex}')
            continue
        print(f'{path}: {len(report)} entr{"y" if len(report) == 1 else "ies"}')
        for status, key, e in report:
            totals[status] += 1
            if status in ('new', 'filled'):
                touched.add(key)
            if args.quiet and status in ('same', 'invalid'):
                continue
            print(f'  {MARK[status]} {status:8s} {key or "?":10s} {_describe(e)}')

    print(', '.join(f'{n} {k}' for k, n in totals.items()))
    if args.dry_run:
        print('dry run: nothing written')
    elif touched:
        mvdb.save({k: db[k] for k in sorted(touched)})
        print('wrote: ' + ', '.join(sorted(touched)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
