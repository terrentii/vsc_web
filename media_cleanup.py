#!/usr/bin/env python3
"""Очистка медиа, когда диск забит.

Порог — 70% занятого места. Удаляем самые старые файлы, но сначала НЕ фото:
архив/видео/документ занимает больше и восстановим его проще, чем картинку
из переписки. Останавливаемся, как только опустились ниже порога.

Запускается сам после каждой загрузки (rooms.upload_media) и вручную/по cron:
    python3 media_cleanup.py --dry-run
    python3 media_cleanup.py --threshold 0.8
"""

import argparse
import os
import shutil

ROOMS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rooms')
THRESHOLD = 0.70

PHOTO_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif', 'heic', 'avif'}


def _is_photo(path: str) -> bool:
    return path.rsplit('.', 1)[-1].lower() in PHOTO_EXTS if '.' in path else False


def _media_files(rooms_dir: str):
    for room in os.scandir(rooms_dir) if os.path.isdir(rooms_dir) else ():
        media_dir = os.path.join(room.path, 'media')
        if not os.path.isdir(media_dir):
            continue
        for entry in os.scandir(media_dir):
            try:
                stat = entry.stat()
            except OSError:
                continue
            if entry.is_file():
                yield entry.path, stat.st_mtime, stat.st_size


def cleanup(rooms_dir=ROOMS_DIR, threshold=THRESHOLD, dry_run=False):
    """Удаляет медиа, пока занято больше threshold диска. Возвращает удалённые пути."""
    if not os.path.isdir(rooms_dir):
        return []

    usage = shutil.disk_usage(rooms_dir)
    excess = usage.used - int(usage.total * threshold)
    if excess <= 0:
        return []

    # сначала не-фото, внутри группы — самые старые
    files = sorted(_media_files(rooms_dir), key=lambda f: (_is_photo(f[0]), f[1]))

    removed = []
    for path, _mtime, size in files:
        if excess <= 0:
            break
        if not dry_run:
            try:
                os.remove(path)
            except OSError:
                continue
        removed.append(path)
        excess -= size
    return removed


def main():
    p = argparse.ArgumentParser(description='Очистка медиа при заполнении диска')
    p.add_argument('--threshold', type=float, default=THRESHOLD,
                   help='доля занятого диска, выше которой чистим (по умолчанию 0.70)')
    p.add_argument('--dry-run', action='store_true', help='только показать, что удалилось бы')
    p.add_argument('--rooms-dir', default=ROOMS_DIR)
    args = p.parse_args()

    usage = shutil.disk_usage(args.rooms_dir if os.path.isdir(args.rooms_dir) else '.')
    print(f'Диск занят на {usage.used / usage.total:.1%} (порог {args.threshold:.0%})')

    removed = cleanup(args.rooms_dir, args.threshold, args.dry_run)
    if not removed:
        print('Чистить нечего.')
        return
    freed = 'бы освободилось' if args.dry_run else 'освобождено'
    print(f'{"Удалилось бы" if args.dry_run else "Удалено"} файлов: {len(removed)}')
    for path in removed:
        print('  ', os.path.relpath(path, os.path.dirname(args.rooms_dir)))
    after = shutil.disk_usage(args.rooms_dir)
    print(f'Теперь занято {after.used / after.total:.1%} ({freed})')


if __name__ == '__main__':
    main()
