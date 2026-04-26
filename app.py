from flask import Flask, request, jsonify
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import requests
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime
from croniter import croniter, CroniterBadCronError

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///reminders.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

API_KEY = os.getenv('API_KEY', '')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
TZ_NAME = os.getenv('TZ', 'America/Los_Angeles')
WEBHOOK_TIMEOUT_SECONDS = 10


def resolve_tz(tz_name):
    """Return ZoneInfo for the given tz name, falling back to env default."""
    return ZoneInfo(tz_name or TZ_NAME)


def to_utc(local_iso_string, tz_name=None):
    """Parse a local ISO 8601 string to UTC. Naive inputs use tz_name (or env default)."""
    dt = datetime.fromisoformat(local_iso_string)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=resolve_tz(tz_name))
    return dt.astimezone(timezone.utc)


def as_utc_aware(dt):
    """Normalize a possibly-naive datetime from the DB to a tz-aware UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _next_cron_fire(cron_expr, tz_name, after_utc):
    """Compute the next cron occurrence (UTC) strictly after `after_utc`,
    interpreting the expression in `tz_name`."""
    tz = resolve_tz(tz_name)
    base_local = after_utc.astimezone(tz)
    it = croniter(cron_expr, base_local)
    next_local = it.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc)


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
    cron = db.Column(db.String(100), nullable=True)
    tz = db.Column(db.String(64), nullable=True)
    created_at = db.Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sent = db.Column(db.Boolean, default=False)
    sent_at = db.Column(DateTime(timezone=True), nullable=True)
    failure_reason = db.Column(db.String(500), nullable=True)


def _migrate_schema():
    """Idempotent migrations for the reminder table.

    Combines the legacy timestamptz conversion with the webhook-mode columns
    and recurring-reminder columns. SQLite is a no-op (string storage); on
    Postgres each ALTER runs and errors are swallowed so re-runs are safe.
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
        "ALTER TABLE reminder ADD COLUMN cron VARCHAR(100)",
        "ALTER TABLE reminder ADD COLUMN tz VARCHAR(64)",
    ]
    # Each ALTER must run in its own transaction. Postgres aborts the entire
    # transaction on the first error, so a single "column already exists" turns
    # every subsequent statement into InternalError ("current transaction is
    # aborted") and the migration silently does nothing.
    for stmt in stmts:
        try:
            with bind.begin() as conn:
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
        "On fire, POSTs a JSON webhook to each reminder's webhook_url.\n"
        "POST /add_reminder requires `webhook_url`. Optional `cron` for recurring.\n"
    ), 200, {'Content-Type': 'text/plain; charset=utf-8'}


@app.route('/add_reminder', methods=['POST'])
@require_api_key
def add_reminder():
    """
    添加提醒。

    一次性：
        { "time": "2026-04-26T08:00:00", "message": "...", "webhook_url": "..." }

    周期性（5 段 cron 表达式）：
        {
            "message": "简报",
            "webhook_url": "...",
            "cron": "0 8,12,18,22 * * *",
            "tz": "America/Los_Angeles"   // 可选；不传则用环境变量 TZ
        }

    同时给 time + cron 时，首次按 time 触发，之后按 cron 周期重排。
    """
    data = request.json or {}
    if 'message' not in data or 'webhook_url' not in data:
        return jsonify({"error": "Missing message or webhook_url"}), 400

    time_str = data.get('time')
    cron_expr = data.get('cron')
    tz_name = data.get('tz')

    if not time_str and not cron_expr:
        return jsonify({"error": "Must provide `time` (one-shot) or `cron` (recurring)"}), 400

    if tz_name:
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return jsonify({"error": f"Unknown tz: {tz_name}"}), 400

    if cron_expr:
        try:
            croniter(cron_expr, datetime.now(timezone.utc))
        except (CroniterBadCronError, ValueError) as e:
            return jsonify({"error": f"Invalid cron expression: {e}"}), 400

    try:
        if time_str:
            first_fire_utc = to_utc(time_str, tz_name)
        else:
            first_fire_utc = _next_cron_fire(cron_expr, tz_name, datetime.now(timezone.utc))
    except Exception as e:
        return jsonify({"error": f"Invalid time format: {e}"}), 400

    reminder = Reminder(
        time_utc=first_fire_utc,
        message=data['message'],
        webhook_url=data['webhook_url'],
        cron=cron_expr,
        tz=tz_name,
    )
    db.session.add(reminder)
    db.session.commit()

    display_tz = resolve_tz(tz_name)
    return jsonify({
        "status": "ok",
        "id": reminder.id,
        "next_fire_local": first_fire_utc.astimezone(display_tz).isoformat(),
        "next_fire_utc": first_fire_utc.isoformat(),
        "tz": tz_name or TZ_NAME,
        "message": data['message'],
        "webhook_url": data['webhook_url'],
        "cron": cron_expr,
        "recurring": bool(cron_expr),
    })


def _fire_webhook(reminder, now):
    """POST the reminder payload to its webhook URL.

    Returns (ok, failure_reason). 10s timeout, no retries — the caller marks
    the reminder so the next cron tick won't fire it again (one-shot) or
    advances time_utc to the next cron occurrence (recurring).
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
    """Cron 每5分钟调用。一次性 reminder 标 sent；周期性 reminder 重排到下次 cron 发生时间。"""
    now = datetime.now(timezone.utc)
    reminders = Reminder.query.filter(
        Reminder.sent == False,
        Reminder.time_utc <= now,
    ).all()

    sent_count = 0
    failed_count = 0
    rescheduled_count = 0
    for r in reminders:
        ok, failure_reason = _fire_webhook(r, now)
        r.sent_at = now
        r.failure_reason = failure_reason

        if r.cron:
            # Recurring: compute next from `now`, not from time_utc, so a long
            # downtime doesn't cause spam catch-up. sent stays False.
            try:
                r.time_utc = _next_cron_fire(r.cron, r.tz, now)
                rescheduled_count += 1
            except Exception as e:
                # Cron broke somehow — stop the loop on this row instead of
                # entering an infinite-retry on every /check.
                r.sent = True
                suffix = f"reschedule failed: {e}"
                r.failure_reason = (
                    f"{failure_reason} | {suffix}" if failure_reason else suffix
                )
        else:
            r.sent = True

        if ok:
            sent_count += 1
        else:
            failed_count += 1
            print(f"Webhook failed for reminder {r.id}: {failure_reason}")

    db.session.commit()
    return jsonify({
        "checked_at": now.isoformat(),
        "found": len(reminders),
        "sent": sent_count,
        "failed": failed_count,
        "rescheduled": rescheduled_count,
    })


@app.route('/reminders')
@require_api_key
def list_reminders():
    reminders = Reminder.query.order_by(Reminder.time_utc.desc()).limit(20).all()
    return jsonify([{
        "id": r.id,
        "time_local": as_utc_aware(r.time_utc).astimezone(resolve_tz(r.tz)).isoformat(),
        "time_utc": as_utc_aware(r.time_utc).isoformat(),
        "tz": r.tz or TZ_NAME,
        "message": r.message,
        "webhook_url": r.webhook_url,
        "cron": r.cron,
        "recurring": bool(r.cron),
        "sent": r.sent,
        "sent_at": as_utc_aware(r.sent_at).isoformat() if r.sent_at else None,
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
