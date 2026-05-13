from datetime import datetime, timezone


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
from flask_login import UserMixin
from extensions import db


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    def get_id(self):
        return self.login


class Room(db.Model):
    __tablename__ = 'rooms'

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(10), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), default='', nullable=False)
    is_open = db.Column(db.Boolean, default=True, nullable=False)
    tg_visible = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    creator_login = db.Column(db.String(64), nullable=False)
    personal_login = db.Column(db.String(64), unique=True, nullable=True, index=True)

    messages = db.relationship('Message', backref='room', lazy='dynamic',
                               cascade='all, delete-orphan',
                               foreign_keys='Message.room_id',
                               primaryjoin='Room.room_id == Message.room_id')
    members = db.relationship('RoomMember', backref='room', lazy='dynamic',
                              cascade='all, delete-orphan',
                              foreign_keys='RoomMember.room_id',
                              primaryjoin='Room.room_id == RoomMember.room_id')


class Message(db.Model):
    __tablename__ = 'messages'

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(10), db.ForeignKey('rooms.room_id'), nullable=False, index=True)
    author = db.Column(db.String(64), nullable=False)
    text = db.Column(db.Text, default='', nullable=False)
    timestamp = db.Column(db.DateTime, default=_utcnow, nullable=False)
    reply_to = db.Column(db.Integer, nullable=True)
    media = db.Column(db.String(256), nullable=True)


class RoomMember(db.Model):
    __tablename__ = 'room_members'

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(10), db.ForeignKey('rooms.room_id'), nullable=False, index=True)
    login = db.Column(db.String(64), nullable=False, index=True)
    joined_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    role = db.Column(db.String(20), default='member', nullable=False)

    __table_args__ = (db.UniqueConstraint('room_id', 'login', name='uq_room_member'),)


class AnonIdentity(db.Model):
    __tablename__ = 'anon_identities'

    id          = db.Column(db.Integer, primary_key=True)  # и есть номер анона
    fingerprint = db.Column(db.String(64), unique=True, nullable=False, index=True)
    first_seen  = db.Column(db.DateTime, default=_utcnow, nullable=False)


class ApiKey(db.Model):
    __tablename__ = 'api_keys'

    id         = db.Column(db.Integer, primary_key=True)
    login      = db.Column(db.String(64), db.ForeignKey('users.login'), nullable=False, index=True)
    key_hash   = db.Column(db.String(64), unique=True, nullable=False, index=True)
    label      = db.Column(db.String(64), default='', nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
