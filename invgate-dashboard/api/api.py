#!/usr/bin/env python3
"""
API minimalista que lee SQLite y expone /api/metrics para el dashboard.
"""

import os
import sqlite3
import json
from datetime import datetime, timezone
from collections import defaultdict
from flask import Flask, jsonify

DB_PATH = os.environ.get("DB_PATH", "/data/invgate.db")
app     = Flask(__name__)


def get_con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


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


def calc_resolution_hours(created_at, solved_at, closed_at):
    t1 = solved_at or closed_at
    if not created_at or not t1:
        return None
    hours = (t1 - created_at) / 3600
    return round(hours, 2) if hours >= 0 else None


@app.route("/api/metrics")
def metrics():
    if not os.path.exists(DB_PATH):
        return jsonify({"error": "DB no disponible aun, espera la carga inicial"}), 503

    con = get_con()
    lkp = load_lookups(con)

    # -- Status snapshot (conteo real desde API, no solo lo que esta en DB) --
    row = con.execute("SELECT value FROM meta WHERE key='status_snapshot'").fetchone()
    by_status = json.loads(row["value"]) if row else {}

    # -- Leer todos los tickets --
    tickets = con.execute("""
        SELECT id, status_id, priority_id, type_id, category_id,
               assigned_id, assigned_group_id,
               created_at, solved_at, closed_at, last_update,
               sla_resolution, sla_first_reply, rating,
               sentiment_initial, sentiment_current
        FROM tickets
    """).fetchall()

    # -- Agregar --
    by_priority   = defaultdict(int)
    by_type       = defaultdict(int)
    by_category   = defaultdict(int)
    by_helpdesk   = defaultdict(int)
    by_agent      = defaultdict(int)
    monthly       = defaultdict(int)
    res_buckets   = {"<1h": 0, "1-4h": 0, "4-8h": 0, "8-24h": 0, "24-72h": 0, ">72h": 0}
    res_times     = []
    sla_ok = sla_breach = sla_unknown = 0
    sentiment_dist = {"positive": 0, "neutral": 0, "negative": 0}

    for t in tickets:
        by_priority[lkp["priority"].get(str(t["priority_id"]), "Sin prioridad")] += 1
        by_type    [lkp["type"]    .get(str(t["type_id"]),     "Sin tipo")]      += 1
        by_category[lkp["category"].get(str(t["category_id"]), "Sin categoria")] += 1
        by_helpdesk[lkp["helpdesk"].get(str(t["assigned_group_id"]), "Sin asignar")] += 1
        by_agent   [lkp["agent"]   .get(str(t["assigned_id"]),  "Sin asignar")]  += 1

        mk = to_month(t["created_at"])
        if mk:
            monthly[mk] += 1

        h = calc_resolution_hours(t["created_at"], t["solved_at"], t["closed_at"])
        if h is not None and h < 720:
            res_times.append(h)
            if   h < 1:  res_buckets["<1h"]    += 1
            elif h < 4:  res_buckets["1-4h"]   += 1
            elif h < 8:  res_buckets["4-8h"]   += 1
            elif h < 24: res_buckets["8-24h"]  += 1
            elif h < 72: res_buckets["24-72h"] += 1
            else:        res_buckets[">72h"]   += 1

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

    avg_res = round(sum(res_times) / len(res_times), 1) if res_times else 0

    # Metadata
    meta_row = con.execute("SELECT value FROM meta WHERE key='last_run_ts'").fetchone()
    last_run = None
    if meta_row:
        try:
            last_run = datetime.fromtimestamp(float(meta_row["value"]),
                                              tz=timezone.utc).isoformat()
        except Exception:
            pass

    con.close()

    return jsonify({
        "generated_at":          datetime.now(tz=timezone.utc).isoformat(),
        "last_extraction":        last_run,
        "total_tickets_in_db":   len(tickets),
        "metrics": {
            "by_status":           by_status,
            "by_priority":         dict(sorted(by_priority.items(), key=lambda x: -x[1])),
            "by_type":             dict(sorted(by_type    .items(), key=lambda x: -x[1])),
            "by_category":         dict(sorted(by_category.items(), key=lambda x: -x[1])[:15]),
            "by_helpdesk":         dict(sorted(by_helpdesk.items(), key=lambda x: -x[1])[:15]),
            "by_agent":            dict(sorted(by_agent   .items(), key=lambda x: -x[1])[:20]),
            "resolution_buckets":  res_buckets,
            "avg_resolution_hours": avg_res,
            "monthly_trend":       dict(sorted(monthly.items())),
            "sla": {"ok": sla_ok, "breach": sla_breach, "unknown": sla_unknown},
            "sentiment":           sentiment_dist,
        },
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
