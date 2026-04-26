from flask import Flask, request, jsonify
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os
import requests
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///reminders.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

API_KEY = os.getenv('API_KEY', '')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
TZ_NAME = os.getenv('TZ', 'America/Los_Angeles')
WEBHOOK_TIMEOUT_SECONDS = 10


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


class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time_utc = db.Column(DateTime(timezone=True), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    webhook_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sent = db.Column(db.Boolean, default=False)
    sent_at = db.Column(DateTime(timezone=True), nullable=True)
    failure_reason = db.Column(db.String(500), nullable=True)


def _migrate_schema():
    """Idempotent migrations for the reminder table.

    Combines the legacy timestamptz conversion with the webhook-mode columns.
    SQLite is a no-op (uses string storage); Postgres runs each ALTER and
    swallows errors so re-runs are safe.
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
        "ALTER TABLE reminder ADD COLUMN webhook_url VARCHAR(500)",
        "ALTER TABLE reminder ADD COLUMN failure_reason VARCHAR(500)",
        "ALTER TABLE reminder ALTER COLUMN chat_id DROP NOT NULL",
        "ALTER TABLE reminder DROP COLUMN chat_id",
    ]
    with bind.begin() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                print(f"Migration skipped ({e.__class__.__name__}): {stmt[:60]}...")


with app.app_context():
    db.create_all()
    _migrate_schema()


@app.route('/')
def index():
    return (
        "Reminder Bot is running.\n\n"
        "BREAKING CHANGE (2026-04-25): No longer sends Telegram messages directly.\n"
        "On fire, POSTs a JSON webhook to the URL stored with each reminder.\n"
        "POST /add_reminder now requires `webhook_url` (and no longer accepts `chat_id`).\n"
    ), 200, {'Content-Type': 'text/plain; charset=utf-8'}


@app.route('/add_reminder', methods=['POST'])
@require_api_key
def add_reminder():
    """
    添加提醒（本地时间 + webhook URL）

    Request body:
        {
            "time": "2026-04-26T08:00:00",          # local time, TZ env decides zone if no offset
            "message": "早报",
            "webhook_url": "http://hermes-agent.railway.internal:9876/brief-trigger"
        }

    到点时 reminder bot 会向 webhook_url POST 一个 JSON（见 /check）。
    """
    data = request.json
    if not data or 'time' not in data or 'message' not in data or 'webhook_url' not in data:
        return jsonify({"error": "Missing time, message, or webhook_url"}), 400

    try:
        utc_time = to_utc(data['time'])
    except Exception as e:
        return jsonify({"error": f"Invalid time format: {e}"}), 400

    reminder = Reminder(
        time_utc=utc_time,
        message=data['message'],
        webhook_url=data['webhook_url'],
    )
    db.session.add(reminder)
    db.session.commit()

    local_time = utc_time.astimezone(local_tz())
    return jsonify({
        "status": "ok",
        "id": reminder.id,
        "time_local": local_time.isoformat(),
        "time_utc": utc_time.isoformat(),
        "tz": TZ_NAME,
        "message": data['message'],
        "webhook_url": data['webhook_url'],
    })


def _fire_webhook(reminder, now):
    """POST the reminder payload to its webhook URL.

    Returns (ok, failure_reason). 10s timeout, no retries — caller marks the
    reminder as sent regardless so the next cron tick won't fire it again.
    """
    if not reminder.webhook_url:
        return False, "missing webhook_url"
    payload = {
        "reminder_id": reminder.id,
        "message": reminder.message,
        "scheduled_time": as_utc_aware(reminder.time_utc).isoformat(),
        "triggered_at": now.isoformat(),
    }
    headers = {}
    if WEBHOOK_SECRET:
        headers['X-Webhook-Secret'] = WEBHOOK_SECRET
    try:
        r = requests.post(
            reminder.webhook_url,
            json=payload,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
        if 200 <= r.status_code < 300:
            return True, None
        body_snippet = (r.text or '')[:200]
        return False, f"HTTP {r.status_code}: {body_snippet}"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"


@app.route('/check', methods=['POST'])
@require_api_key
def check_reminders():
    """Cron 每5分钟调用。每条到期 reminder 触发一次 webhook，无论成败都标 sent。"""
    now = datetime.now(timezone.utc)
    reminders = Reminder.query.filter(
        Reminder.sent == False,
        Reminder.time_utc <= now,
    ).all()

    sent_count = 0
    failed_count = 0
    for r in reminders:
        ok, failure_reason = _fire_webhook(r, now)
        r.sent = True
        r.sent_at = now
        if ok:
            sent_count += 1
        else:
            failed_count += 1
            r.failure_reason = failure_reason
            print(f"Webhook failed for reminder {r.id}: {failure_reason}")

    db.session.commit()
    return jsonify({
        "checked_at": now.isoformat(),
        "found": len(reminders),
        "sent": sent_count,
        "failed": failed_count,
    })


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
        "webhook_url": r.webhook_url,
        "sent": r.sent,
        "failure_reason": r.failure_reason,
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
