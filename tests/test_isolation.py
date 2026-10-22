"""Тесты не должны трогать настоящий каталог rooms/ и запускать очистку медиа.

Очистка по заполненности диска (media_cleanup) удаляет файлы из ROOMS_DIR;
на почти полном диске прогон тестов стёр бы реальные медиа.
"""
import os
import tempfile

import media_cleanup
import rooms


def test_rooms_dir_is_temporary():
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rooms')
    assert os.path.realpath(rooms.ROOMS_DIR) != os.path.realpath(real)
    assert os.path.realpath(rooms.ROOMS_DIR).startswith(os.path.realpath(tempfile.gettempdir()))
    assert media_cleanup.ROOMS_DIR == rooms.ROOMS_DIR


def test_cleanup_disabled_in_tests():
    assert media_cleanup.THRESHOLD >= 1.0
    assert media_cleanup.cleanup() == []
