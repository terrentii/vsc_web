"""
REST API Blueprint
-----------------
GET  /api/rooms                   — список открытых комнат
GET  /api/room/<room_id>/messages — сообщения комнаты (?after=N для пагинации)
POST /api/room/<room_id>/message  — отправить сообщение (JSON body: {"text": "..."})
"""
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from extensions import db
from models import Room, Message, RoomMember

api_bp = Blueprint('api', __name__)


def _api_can_access(room):
    if room.is_open:
        return True
    if session.get('user_type') != 'registered':
        return False
    return RoomMember.query.filter_by(
        room_id=room.room_id, login=session.get('login')
    ).first() is not None


@api_bp.route('/rooms')
def list_rooms():
    """Список открытых комнат (последние 50)."""
    rooms = (
        Room.query
        .filter_by(is_open=True)
        .order_by(Room.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([
        {
            'room_id': r.room_id,
            'name': r.name or r.room_id,
            'created_at': r.created_at.isoformat(),
        }
        for r in rooms
    ])


@api_bp.route('/room/<room_id>/messages')
def get_messages(room_id):
    """
    Список сообщений комнаты.
    Query param: after=N (0-based offset, по умолчанию 0).
    """
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    if not _api_can_access(room):
        return jsonify({'error': 'Access denied'}), 403

    after = max(0, request.args.get('after', 0, type=int))
    messages = (
        Message.query
        .filter_by(room_id=room_id)
        .order_by(Message.id)
        .offset(after)
        .limit(200)
        .all()
    )

    return jsonify([
        {
            'id': m.id,
            'author': m.author,
            'text': m.text,
            'timestamp': m.timestamp.isoformat(),
            'reply_to': m.reply_to,
            'media': m.media,
        }
        for m in messages
    ])


@api_bp.route('/room/<room_id>/message', methods=['POST'])
def post_message(room_id):
    """
    Отправить сообщение. Тело запроса: JSON {"text": "..."}.
    Требует активной сессии (зарегистрированный или анонимный пользователь).
    """
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    if not _api_can_access(room):
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()[:4000]
    if not text:
        return jsonify({'error': 'text is required'}), 400

    if session.get('user_type') == 'registered':
        author = session['login']
    else:
        author = session.get('anon_id', 'Anon')

    msg = Message(
        room_id=room_id,
        author=author,
        text=text,
        timestamp=datetime.utcnow(),
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({'ok': True, 'id': msg.id, 'author': author, 'text': text}), 201
