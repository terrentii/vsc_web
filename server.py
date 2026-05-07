"""
WSGI entry point.
Запуск production-сервером:
    gunicorn server:application
или через Procfile:
    gunicorn app:app
"""
from app import app as application

if __name__ == '__main__':
    application.run()
