from flask import Flask, request, jsonify
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os
import requests
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime

app = Flask(__name__)

# 数据库配置
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///reminders.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# 从环境变量读取
BOT_TOKEN = os.getenv('BOT_TOKEN')
DEFAULT_CHAT_ID = os.getenv('CHAT_ID')
API_KEY = os.getenv('API_KEY', '')
TZ_NAME = os.getenv('TZ', 'America/Los_Angeles')


def local_tz():
    return ZoneInfo(TZ_NAME)


def to_utc(local_iso_string):
    """本地时间转 UTC"""
    dt = datetime.fromisoformat(local_iso_string)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(timezone.utc)


def as_utc_aware(dt):
    """Normalize a possibly-naive datetime from the DB to a tz-aware UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def require_api_key(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return f(*args, **kwargs)
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if key != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def send_telegram_message(chat_id, text):
    if not BOT_TOKEN:
        return False, "No BOT_TOKEN"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }, timeout=10)
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time_utc = db.Column(DateTime(timezone=True), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    chat_id = db.Column(db.String(50), nullable=False)
    created_at = db.Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sent = db.Column(db.Boolean, default=False)
    sent_at = db.Column(DateTime(timezone=True), nullable=True)


def _migrate_to_timestamptz():
    """Idempotent migration: convert naive timestamp columns to timestamptz.

    Existing rows are assumed to already hold UTC values (that is what the
    previous code attempted to write). On Postgres this is safe to re-run.
    SQLite keeps datetimes as strings so the conversion is a no-op.
    """
    bind = db.session.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    from sqlalchemy import text
    stmts = [
        "ALTER TABLE reminder ALTER COLUMN time_utc TYPE timestamp with time zone "
        "USING time_utc AT TIME ZONE 'UTC'",
        "ALTER TABLE reminder ALTER COLUMN created_at TYPE timestamp with time zone "
        "USING created_at AT TIME ZONE 'UTC'",
        "ALTER TABLE reminder ALTER COLUMN sent_at TYPE timestamp with time zone "
        "USING sent_at AT TIME ZONE 'UTC'",
    ]
    with bind.begin() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                print(f"Migration skipped ({e.__class__.__name__}): {stmt[:60]}...")


with app.app_context():
    db.create_all()
    _migrate_to_timestamptz()


@app.route('/')
def index():
    return "Reminder Bot is running!"


@app.route('/add_reminder', methods=['POST'])
@require_api_key
def add_reminder():
    """
    添加提醒（本地时间）
    {
        "time": "2026-04-12T15:00",
        "message": "倒垃圾",
        "chat_id": "8509139631"  // 可选
    }
    """
    data = request.json
    if not data or 'time' not in data or 'message' not in data:
        return jsonify({"error": "Missing time or message"}), 400

    try:
        utc_time = to_utc(data['time'])
    except Exception as e:
        return jsonify({"error": f"Invalid time format: {e}"}), 400

    chat_id = data.get('chat_id', DEFAULT_CHAT_ID)
    reminder = Reminder(time_utc=utc_time, message=data['message'], chat_id=chat_id)
    db.session.add(reminder)
    db.session.commit()

    local_time = utc_time.astimezone(local_tz())
    return jsonify({
        "status": "ok",
        "id": reminder.id,
        "time_local": local_time.isoformat(),
        "time_utc": utc_time.isoformat(),
        "tz": TZ_NAME,
        "message": data['message']
    })


@app.route('/check', methods=['POST'])
@require_api_key
def check_reminders():
    """Cron 每5分钟调用"""
    now = datetime.now(timezone.utc)
    reminders = Reminder.query.filter(
        Reminder.sent == False,
        Reminder.time_utc <= now
    ).all()

    sent_count = 0
    for r in reminders:
        success, info = send_telegram_message(r.chat_id, f"⏰ {r.message}")
        if success:
            r.sent = True
            r.sent_at = now
            sent_count += 1
        else:
            print(f"Failed to send reminder {r.id}: {info}")

    db.session.commit()
    return jsonify({"checked_at": now.isoformat(), "found": len(reminders), "sent": sent_count})


@app.route('/reminders')
@require_api_key
def list_reminders():
    reminders = Reminder.query.order_by(Reminder.time_utc.desc()).limit(20).all()
    tz = local_tz()
    return jsonify([{
        "id": r.id,
        "time_local": as_utc_aware(r.time_utc).astimezone(tz).isoformat(),
        "time_utc": as_utc_aware(r.time_utc).isoformat(),
        "message": r.message,
        "sent": r.sent,
    } for r in reminders])


@app.route('/delete/<int:id>', methods=['DELETE'])
@require_api_key
def delete_reminder(id):
    r = Reminder.query.get_or_404(id)
    db.session.delete(r)
    db.session.commit()
    return jsonify({"status": "deleted", "id": id})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
