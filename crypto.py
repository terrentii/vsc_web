"""Шифрование содержимого сообщений at-rest (в БД).

Ключ отдельный от app.secret_key (который подписывает сессии/CSRF) —
компрометация одного не должна автоматически раскрывать другой.
Как и .secret_key в app.py: берётся из env, иначе один раз генерируется
и сохраняется в файл рядом с проектом.

Формат хранения — с префиксом ENC_PREFIX перед токеном Fernet. Значения без
префикса считаются старым/незашифрованным текстом и возвращаются как есть —
это то, что позволяет накатить эту версию на сервер с уже существующими
сообщениями без миграции: старые записи остаются читаемыми, новые пишутся
уже зашифрованными.
"""
import os

ENC_PREFIX = 'enc1:'

_KEY_FILE = os.path.join(os.path.dirname(__file__), '.message_key')
_fernet = None


def _load_key() -> bytes:
    env_key = os.environ.get('MESSAGE_ENCRYPTION_KEY', '').strip()
    if env_key:
        return env_key.encode('ascii')

    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, 'rb') as f:
            key = f.read().strip()
        if key:
            return key

    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    with open(_KEY_FILE, 'wb') as f:
        f.write(key)
    os.chmod(_KEY_FILE, 0o600)
    return key


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt_text(value):
    if not value:
        return value
    token = _get_fernet().encrypt(value.encode('utf-8')).decode('ascii')
    return ENC_PREFIX + token


def decrypt_text(value):
    if not value or not value.startswith(ENC_PREFIX):
        return value
    from cryptography.fernet import InvalidToken
    token = value[len(ENC_PREFIX):].encode('ascii')
    try:
        return _get_fernet().decrypt(token).decode('utf-8')
    except InvalidToken:
        # Не наш ключ (например, ротация) — отдаём как есть, а не роняем комнату.
        return value
