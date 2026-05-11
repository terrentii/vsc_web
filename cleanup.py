#!/usr/bin/env python3
"""
Скрипт полной очистки данных МЫС Web.
Удаляет всех пользователей, сообщения, комнаты и медиафайлы.
Структура БД и приложение остаются нетронутыми.
"""

import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ROOMS_DIR        = os.path.join(BASE_DIR, 'rooms')
USERS_DIR        = os.path.join(BASE_DIR, 'users')
SESSION_DIR      = os.path.join(BASE_DIR, 'flask_session')


def confirm(prompt: str) -> bool:
    answer = input(f'{prompt} [да/нет]: ').strip().lower()
    return answer in ('да', 'д', 'yes', 'y')


def clear_table(model, label: str):
    count = model.query.count()
    model.query.delete()
    print(f'  ✓ {label}: удалено {count} записей')


def clear_directory(path: str, label: str, recreate: bool = True):
    if not os.path.isdir(path):
        print(f'  — {label}: папка не найдена, пропуск')
        return
    count = sum(len(files) for _, _, files in os.walk(path))
    shutil.rmtree(path)
    if recreate:
        os.makedirs(path, exist_ok=True)
    print(f'  ✓ {label}: удалено {count} файлов')


def main():
    print('=' * 52)
    print('   МЫС Web — Скрипт полной очистки данных')
    print('=' * 52)
    print()
    print('Будет удалено:')
    print('  • Все зарегистрированные пользователи')
    print('  • Все сообщения')
    print('  • Все комнаты и медиафайлы')
    print('  • Все анонимные идентификаторы')
    print('  • Все активные сессии')
    print()
    print('Сохранится:')
    print('  • Структура базы данных')
    print('  • Код приложения')
    print('  • Настройки')
    print()

    if not confirm('Вы уверены? Это действие необратимо'):
        print('Отменено.')
        sys.exit(0)

    print()
    if not confirm('Подтвердите ещё раз — все данные будут уничтожены'):
        print('Отменено.')
        sys.exit(0)

    print()
    print('Очистка...')

    # Импортируем приложение и модели
    from app import app
    from extensions import db
    from models import User, Room, Message, RoomMember, AnonIdentity

    with app.app_context():
        # 1. БД — удаляем все строки во всех таблицах
        print()
        print('[1/3] База данных:')
        clear_table(Message,      'Сообщения')
        clear_table(RoomMember,   'Участники комнат')
        clear_table(Room,         'Комнаты')
        clear_table(User,         'Пользователи')
        clear_table(AnonIdentity, 'Анонимные идентификаторы')
        db.session.commit()

        # 2. Файлы комнат
        print()
        print('[2/3] Файловая система:')
        clear_directory(ROOMS_DIR,   'Папки комнат с медиафайлами')
        clear_directory(USERS_DIR,   'Данные пользователей')
        clear_directory(SESSION_DIR, 'Сессии')

        # 3. Пересоздаём структуру БД (на случай если что-то слетело)
        print()
        print('[3/3] Проверка структуры БД:')
        db.create_all()
        print('  ✓ Все таблицы на месте')

    print()
    print('=' * 52)
    print('   Очистка завершена. Приложение готово к работе.')
    print('=' * 52)


if __name__ == '__main__':
    main()
