#!/usr/bin/env python3
"""
API minimalista que lee SQLite y expone /api/metrics para el dashboard.
"""

import os
import sqlite3
import json
import re
from datetime import datetime, time, timedelta, timezone
from collections import defaultdict
from flask import Flask, jsonify, request

DB_PATH = os.environ.get("DB_PATH", "/data/invgate.db")
AREA_CUSTOM_FIELD_ID = os.environ.get("AREA_CUSTOM_FIELD_ID", "72")
app     = Flask(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_con():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_app_db(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS dashboard_users (
            email       TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dashboard_views (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email  TEXT NOT NULL,
            name        TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE(user_email, name),
            FOREIGN KEY(user_email) REFERENCES dashboard_users(email) ON DELETE CASCADE
        );
    """)
    con.commit()


def normalize_email(value):
    email = (value or "").strip().lower()
    if not EMAIL_RE.match(email):
        return None
    return email


def ensure_user(con, email):
    now = datetime.now(tz=timezone.utc).isoformat()
    con.execute("""
        INSERT INTO dashboard_users (email, created_at, last_seen_at)
        VALUES (?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET last_seen_at = excluded.last_seen_at
    """, (email, now, now))


def unavailable(message):
    return jsonify({"error": message}), 503


def load_lookups(con):
    rows = con.execute("SELECT entity, id, name FROM lookups").fetchall()
    lkp  = defaultdict(dict)
    for r in rows:
        lkp[r["entity"]][r["id"]] = r["name"]
    return lkp


def to_month(epoch):
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m")
    except Exception:
        return None


def to_day(epoch):
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


def to_hour(epoch):
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:00")
    except Exception:
        return None


def calc_resolution_hours(created_at, solved_at, closed_at):
    t1 = solved_at or closed_at
    if not created_at or not t1:
        return None
    hours = (t1 - created_at) / 3600
    return round(hours, 2) if hours >= 0 else None


def period_bounds(period, now=None):
    now = now or datetime.now(tz=timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)

    if period == "daily":
        start = today
        previous_start = start - timedelta(days=1)
        previous_end = start
    elif period == "weekly":
        start = today - timedelta(days=today.weekday())
        previous_start = start - timedelta(days=7)
        previous_end = start
    elif period == "monthly":
        start = today.replace(day=1)
        previous_end = start
        previous_month_day = start - timedelta(days=1)
        previous_start = previous_month_day.replace(day=1)
    elif period == "prev_month":
        today_first = today.replace(day=1)
        start = (today_first - timedelta(days=1)).replace(day=1)
        end = today_first
        previous_end = start
        previous_start = (start - timedelta(days=1)).replace(day=1)
        return int(start.timestamp()), int(end.timestamp()), int(previous_start.timestamp()), int(previous_end.timestamp())
    elif period == "historical":
        return None, None, None, None
    else:
        raise ValueError("period invalido")

    return int(start.timestamp()), int(now.timestamp()), int(previous_start.timestamp()), int(previous_end.timestamp())


def in_range(value, start, end):
    if value is None:
        return False
    if start is None and end is None:
        return True
    return value >= start and value < end


def extract_area(raw_json):
    try:
        data = json.loads(raw_json or "{}")
    except Exception:
        return None, "Sin area"

    field = (data.get("custom_fields") or {}).get(AREA_CUSTOM_FIELD_ID)
    if isinstance(field, dict) and field:
        area_id, area_name = next(iter(field.items()))
        return str(area_id), str(area_name)
    if isinstance(field, list) and field:
        return str(field[0]), str(field[0])
    if field:
        return str(field), str(field)
    return None, "Sin area"


def ticket_area_matches(ticket, area_id):
    if not area_id:
        return True
    return ticket["area_id"] == area_id


def percent_delta(current, previous):
    if previous == 0:
        return None if current == 0 else 100
    return round((current - previous) / previous * 100, 1)


def trend_key(epoch, period):
    if period == "daily":
        return to_hour(epoch)
    if period in ("weekly", "monthly", "prev_month", "custom"):
        return to_day(epoch)
    return to_month(epoch)


def sort_dict(counter, limit=None):
    items = sorted(counter.items(), key=lambda x: -x[1])
    if limit:
        items = items[:limit]
    return dict(items)


def summarize(tickets, period_tickets, start, end):
    closed_in_period = sum(1 for t in tickets if in_range(t["closed_at"], start, end))
    solved_in_period = sum(1 for t in tickets if in_range(t["solved_at"], start, end))
    updated_in_period = sum(1 for t in tickets if in_range(t["last_update"], start, end))
    open_now = sum(1 for t in tickets if not t["closed_at"])

    sla_ok = sla_breach = sla_unknown = 0
    res_times = []
    for t in period_tickets:
        h = calc_resolution_hours(t["created_at"], t["solved_at"], t["closed_at"])
        if h is not None and h < 720:
            res_times.append(h)

        sla = (t["sla_resolution"] or "").lower()
        if not sla:
            sla_unknown += 1
        elif "breach" in sla or "vencido" in sla:
            sla_breach += 1
        else:
            sla_ok += 1

    return {
        "created_in_period": len(period_tickets),
        "closed_in_period": closed_in_period,
        "solved_in_period": solved_in_period,
        "updated_in_period": updated_in_period,
        "open_now": open_now,
        "sla_ok": sla_ok,
        "sla_breach": sla_breach,
        "sla_unknown": sla_unknown,
        "avg_resolution_hours": round(sum(res_times) / len(res_times), 1) if res_times else 0,
    }


def build_metrics(tickets, period_tickets, lkp, period):
    by_status = defaultdict(int)
    by_priority = defaultdict(int)
    by_type = defaultdict(int)
    by_category = defaultdict(int)
    by_helpdesk = defaultdict(int)
    by_area = defaultdict(int)
    by_agent = defaultdict(int)
    trend = defaultdict(int)
    res_buckets = {"<1h": 0, "1-4h": 0, "4-8h": 0, "8-24h": 0, "24-72h": 0, ">72h": 0}
    sentiment_dist = {"positive": 0, "neutral": 0, "negative": 0}
    sla_ok = sla_breach = sla_unknown = 0
    res_times = []

    for t in period_tickets:
        by_status[lkp["status"].get(str(t["status_id"]), "Sin estado")] += 1
        by_priority[lkp["priority"].get(str(t["priority_id"]), "Sin prioridad")] += 1
        by_type[lkp["type"].get(str(t["type_id"]), "Sin tipo")] += 1
        by_category[lkp["category"].get(str(t["category_id"]), "Sin categoria")] += 1
        by_helpdesk[lkp["helpdesk"].get(str(t["assigned_group_id"]), "Sin asignar")] += 1
        by_area[t["area_name"]] += 1
        by_agent[lkp["agent"].get(str(t["assigned_id"]), "Sin asignar")] += 1

        key = trend_key(t["created_at"], period)
        if key:
            trend[key] += 1

        h = calc_resolution_hours(t["created_at"], t["solved_at"], t["closed_at"])
        if h is not None and h < 720:
            res_times.append(h)
            if h < 1:
                res_buckets["<1h"] += 1
            elif h < 4:
                res_buckets["1-4h"] += 1
            elif h < 8:
                res_buckets["4-8h"] += 1
            elif h < 24:
                res_buckets["8-24h"] += 1
            elif h < 72:
                res_buckets["24-72h"] += 1
            else:
                res_buckets[">72h"] += 1

        sla = (t["sla_resolution"] or "").lower()
        if not sla:
            sla_unknown += 1
        elif "breach" in sla or "vencido" in sla:
            sla_breach += 1
        else:
            sla_ok += 1

        sent = (t["sentiment_current"] or "").lower()
        if sent in sentiment_dist:
            sentiment_dist[sent] += 1

    return {
        "by_status": sort_dict(by_status),
        "by_priority": sort_dict(by_priority),
        "by_type": sort_dict(by_type),
        "by_category": sort_dict(by_category, 15),
        "by_helpdesk": sort_dict(by_helpdesk, 15),
        "by_area": sort_dict(by_area, 20),
        "by_agent": sort_dict(by_agent, 20),
        "resolution_buckets": res_buckets,
        "avg_resolution_hours": round(sum(res_times) / len(res_times), 1) if res_times else 0,
        "monthly_trend": dict(sorted(trend.items())),
        "trend": dict(sorted(trend.items())),
        "sla": {"ok": sla_ok, "breach": sla_breach, "unknown": sla_unknown},
        "sentiment": sentiment_dist,
    }


@app.route("/api/metrics")
def metrics():
    if not os.path.exists(DB_PATH):
        return unavailable("DB no disponible aun, espera la carga inicial")

    period = request.args.get("period", "historical").lower()
    area_id = request.args.get("area_id")

    if period == "custom":
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        if not date_from or not date_to:
            return jsonify({"error": "date_from y date_to son requeridos para period=custom"}), 400
        try:
            start = int(datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            end   = int((datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp())
        except ValueError:
            return jsonify({"error": "Formato de fecha invalido, usar YYYY-MM-DD"}), 400
        previous_start = previous_end = None
    else:
        try:
            start, end, previous_start, previous_end = period_bounds(period)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    con = None
    try:
        con = get_con()
        lkp = load_lookups(con)

        # -- Leer todos los tickets --
        rows = con.execute("""
            SELECT id, status_id, priority_id, type_id, category_id,
                   assigned_id, assigned_group_id,
                   created_at, solved_at, closed_at, last_update,
                   sla_resolution, sla_first_reply, rating,
                   sentiment_initial, sentiment_current, raw_json
            FROM tickets
        """).fetchall()

        tickets = []
        for row in rows:
            item = dict(row)
            item["area_id"], item["area_name"] = extract_area(item.get("raw_json"))
            tickets.append(item)

        meta_row = con.execute("SELECT value FROM meta WHERE key='last_run_ts'").fetchone()
        last_run = None
        if meta_row:
            try:
                last_run = datetime.fromtimestamp(float(meta_row["value"]),
                                                  tz=timezone.utc).isoformat()
            except Exception:
                pass
    except sqlite3.OperationalError as e:
        app.logger.warning("SQLite no disponible: %s", e)
        return unavailable("DB no disponible temporalmente, reintenta en unos segundos")
    finally:
        if con:
            con.close()

    area_tickets = [t for t in tickets if ticket_area_matches(t, area_id)]
    period_tickets = [t for t in area_tickets if in_range(t["created_at"], start, end)]
    previous_tickets = [t for t in area_tickets if in_range(t["created_at"], previous_start, previous_end)]

    summary = summarize(area_tickets, period_tickets, start, end)
    previous_summary = summarize(area_tickets, previous_tickets, previous_start, previous_end) if previous_start else None
    comparison = None
    if previous_summary:
        comparison = {
            "created_delta_pct": percent_delta(summary["created_in_period"], previous_summary["created_in_period"]),
            "closed_delta_pct": percent_delta(summary["closed_in_period"], previous_summary["closed_in_period"]),
            "solved_delta_pct": percent_delta(summary["solved_in_period"], previous_summary["solved_in_period"]),
            "sla_breach_delta_pct": percent_delta(summary["sla_breach"], previous_summary["sla_breach"]),
            "avg_resolution_delta_pct": percent_delta(summary["avg_resolution_hours"], previous_summary["avg_resolution_hours"]),
            "previous": previous_summary,
        }

    areas = {}
    for t in tickets:
        areas[t["area_id"] or ""] = t["area_name"]
    available_areas = [
        {"id": area_key, "name": name}
        for area_key, name in sorted(areas.items(), key=lambda item: item[1].lower())
        if area_key
    ]

    selected_area_name = None
    if area_id:
        selected_area_name = next((a["name"] for a in available_areas if a["id"] == area_id), None)

    return jsonify({
        "generated_at":          datetime.now(tz=timezone.utc).isoformat(),
        "last_extraction":        last_run,
        "total_tickets_in_db":    len(tickets),
        "available_areas":        available_areas,
        "filters": {
            "period": period,
            "period_start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat() if start else None,
            "period_end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat() if end else None,
            "area_id": area_id,
            "area_name": selected_area_name,
            "area_custom_field_id": AREA_CUSTOM_FIELD_ID,
        },
        "summary":               summary,
        "comparison":            comparison,
        "metrics":               build_metrics(area_tickets, period_tickets, lkp, period),
    })


@app.route("/api/health")
def health():
    exists = os.path.exists(DB_PATH)
    count  = 0
    if exists:
        try:
            count = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        except Exception:
            pass
    return jsonify({"status": "ok", "db_exists": exists, "tickets": count})


@app.route("/api/users/<path:email>/views", methods=["GET"])
def list_views(email):
    email = normalize_email(email)
    if not email:
        return jsonify({"error": "correo invalido"}), 400

    con = None
    try:
        con = get_con()
        init_app_db(con)
        ensure_user(con, email)
        con.commit()
        rows = con.execute("""
            SELECT name, config_json, updated_at
            FROM dashboard_views
            WHERE user_email = ?
            ORDER BY lower(name)
        """, (email,)).fetchall()
        views = []
        for row in rows:
            try:
                config = json.loads(row["config_json"])
            except Exception:
                config = {}
            views.append({"name": row["name"], "config": config, "updated_at": row["updated_at"]})
        return jsonify({"email": email, "views": views})
    finally:
        if con:
            con.close()


@app.route("/api/users/<path:email>/views", methods=["POST"])
def save_view(email):
    email = normalize_email(email)
    if not email:
        return jsonify({"error": "correo invalido"}), 400

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    config = payload.get("config") or {}
    if not name:
        return jsonify({"error": "name es requerido"}), 400
    if not isinstance(config, dict):
        return jsonify({"error": "config debe ser un objeto"}), 400

    con = None
    try:
        con = get_con()
        init_app_db(con)
        ensure_user(con, email)
        now = datetime.now(tz=timezone.utc).isoformat()
        con.execute("""
            INSERT INTO dashboard_views (user_email, name, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_email, name) DO UPDATE SET
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
        """, (email, name, json.dumps(config, ensure_ascii=False), now, now))
        con.commit()
        return jsonify({"email": email, "view": {"name": name, "config": config, "updated_at": now}})
    finally:
        if con:
            con.close()


@app.route("/api/users/<path:email>/views/<path:name>", methods=["DELETE"])
def delete_view(email, name):
    email = normalize_email(email)
    if not email:
        return jsonify({"error": "correo invalido"}), 400

    con = None
    try:
        con = get_con()
        init_app_db(con)
        con.execute("DELETE FROM dashboard_views WHERE user_email = ? AND name = ?", (email, name))
        con.commit()
        return jsonify({"email": email, "deleted": name})
    finally:
        if con:
            con.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
