import os
import shutil
from collections import namedtuple

import media_cleanup

Usage = namedtuple('Usage', 'total used free')


def _room_with_files(tmp_path, *specs):
    """specs: (имя, mtime, размер). Возвращает путь rooms/."""
    rooms = tmp_path / 'rooms'
    media = rooms / '1234567890' / 'media'
    media.mkdir(parents=True)
    for name, mtime, size in specs:
        f = media / name
        f.write_bytes(b'x' * size)
        os.utime(f, (mtime, mtime))
    return str(rooms)


def _fake_disk(monkeypatch, used_ratio):
    total = 1000
    monkeypatch.setattr(media_cleanup.shutil, 'disk_usage',
                        lambda _p: Usage(total, int(total * used_ratio), 0))


def test_below_threshold_nothing_is_deleted(tmp_path, monkeypatch):
    rooms = _room_with_files(tmp_path, ('a.zip', 1, 10))
    _fake_disk(monkeypatch, 0.5)
    assert media_cleanup.cleanup(rooms) == []
    assert os.path.isfile(os.path.join(rooms, '1234567890', 'media', 'a.zip'))


def test_non_photos_go_first_even_if_newer(tmp_path, monkeypatch):
    rooms = _room_with_files(tmp_path,
                             ('old.jpg', 1, 100),     # самое старое, но фото
                             ('new.zip', 9999, 100))  # свежее, но не фото
    _fake_disk(monkeypatch, 0.75)  # лишних 50 байт из 1000
    removed = media_cleanup.cleanup(rooms)
    assert [os.path.basename(p) for p in removed] == ['new.zip']


def test_oldest_first_inside_group(tmp_path, monkeypatch):
    rooms = _room_with_files(tmp_path,
                             ('new.zip', 9999, 40),
                             ('old.zip', 1, 40),
                             ('mid.zip', 500, 40))
    _fake_disk(monkeypatch, 0.78)  # лишних 80 байт — ровно два файла по 40
    removed = media_cleanup.cleanup(rooms)
    assert [os.path.basename(p) for p in removed] == ['old.zip', 'mid.zip']


def test_photos_are_deleted_when_non_photos_are_not_enough(tmp_path, monkeypatch):
    rooms = _room_with_files(tmp_path, ('a.zip', 1, 10), ('b.png', 2, 100))
    _fake_disk(monkeypatch, 0.9)
    removed = media_cleanup.cleanup(rooms)
    assert [os.path.basename(p) for p in removed] == ['a.zip', 'b.png']


def test_dry_run_keeps_files(tmp_path, monkeypatch):
    rooms = _room_with_files(tmp_path, ('a.zip', 1, 100))
    _fake_disk(monkeypatch, 0.9)
    removed = media_cleanup.cleanup(rooms, dry_run=True)
    assert len(removed) == 1
    assert os.path.isfile(os.path.join(rooms, '1234567890', 'media', 'a.zip'))
