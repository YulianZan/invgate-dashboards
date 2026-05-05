#!/usr/bin/env python3
"""
InvGate -> SQLite extractor (incremental).
Lee INVGATE_URL, INVGATE_USER, INVGATE_PASS, DB_PATH del entorno.
"""

import os
import sqlite3
import requests
import json
import sys
import time
import random
import threading
import email.utils
from datetime import datetime, timezone
from requests.auth import HTTPBasicAuth
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config desde entorno ──────────────────────────────────────────────────────
INVGATE_URL = os.environ["INVGATE_URL"]
USERNAME    = os.environ["INVGATE_USER"]
PASSWORD    = os.environ["INVGATE_PASS"]
DB_PATH     = os.environ.get("DB_PATH", "/data/invgate.db")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 3))
HTTP_MAX_RETRIES = int(os.environ.get("HTTP_MAX_RETRIES", 6))
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", 0.25))
RATE_LIMIT_BACKOFF_SECONDS = float(os.environ.get("RATE_LIMIT_BACKOFF_SECONDS", 30))
FORCE_FULL  = "--full" in sys.argv

BASE    = INVGATE_URL.rstrip("/").removesuffix("/api/v1").removesuffix("/api")
SESSION = requests.Session()
SESSION.auth   = HTTPBasicAuth(USERNAME, PASSWORD)
SESSION.verify = True

REQUEST_LOCK = threading.Lock()
NEXT_REQUEST_AT = 0.0


# ── HTTP ──────────────────────────────────────────────────────────────────────

def parse_retry_after(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - datetime.now(tz=timezone.utc).timestamp())
        except Exception:
            return None


def wait_for_turn():
    global NEXT_REQUEST_AT
    if REQUEST_DELAY_SECONDS <= 0:
        return
    with REQUEST_LOCK:
        now = time.monotonic()
        wait = max(0.0, NEXT_REQUEST_AT - now)
        NEXT_REQUEST_AT = max(now, NEXT_REQUEST_AT) + REQUEST_DELAY_SECONDS
    if wait > 0:
        time.sleep(wait)


def get(endpoint, params=None):
    url     = f"{BASE}/api/v1/{endpoint}"
    encoded = []
    for k, v in (params or {}).items():
        if isinstance(v, list):
            for item in v:
                encoded.append((f"{k}[]", item))
        else:
            encoded.append((k, v))

    for attempt in range(HTTP_MAX_RETRIES + 1):
        try:
            wait_for_turn()
            r = SESSION.get(url, params=encoded or None, timeout=30)
            if r.status_code == 429:
                retry_after = parse_retry_after(r.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else RATE_LIMIT_BACKOFF_SECONDS
                wait = wait + random.uniform(0, 1.5)
                if attempt < HTTP_MAX_RETRIES:
                    print(f"  [WARN] {endpoint} -> HTTP 429. Reintento {attempt + 1}/{HTTP_MAX_RETRIES} en {wait:.1f}s")
                    time.sleep(wait)
                    continue
            if 500 <= r.status_code < 600 and attempt < HTTP_MAX_RETRIES:
                wait = min(60, (2 ** attempt) + random.uniform(0, 1))
                print(f"  [WARN] {endpoint} -> HTTP {r.status_code}. Reintento {attempt + 1}/{HTTP_MAX_RETRIES} en {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            print(f"  [ERROR] {endpoint} -> HTTP {e.response.status_code}: {e.response.text[:150]}")
            return None
        except requests.exceptions.RequestException as e:
            if attempt < HTTP_MAX_RETRIES:
                wait = min(60, (2 ** attempt) + random.uniform(0, 1))
                print(f"  [WARN] {endpoint} -> {e}. Reintento {attempt + 1}/{HTTP_MAX_RETRIES} en {wait:.1f}s")
                time.sleep(wait)
                continue
            print(f"  [ERROR] {endpoint} -> {e}")
            return None
        except Exception as e:
            print(f"  [ERROR] {endpoint} -> {e}")
            return None

    print(f"  [ERROR] {endpoint} -> agotados {HTTP_MAX_RETRIES} reintentos")
    return None


def get_ticket(tid):
    return get("incident", {"id": tid})


# ── SQLite schema ─────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tickets (
    id                  INTEGER PRIMARY KEY,
    title               TEXT,
    status_id           INTEGER,
    priority_id         INTEGER,
    type_id             INTEGER,
    category_id         INTEGER,
    assigned_id         INTEGER,
    assigned_group_id   INTEGER,
    user_id             INTEGER,
    creator_id          INTEGER,
    source_id           INTEGER,
    created_at          INTEGER,   -- epoch
    solved_at           INTEGER,
    closed_at           INTEGER,
    last_update         INTEGER,
    sla_resolution      TEXT,
    sla_first_reply     TEXT,
    rating              INTEGER,
    sentiment_initial   TEXT,
    sentiment_current   TEXT,
    raw_json            TEXT       -- ticket completo para no perder nada
);

CREATE TABLE IF NOT EXISTS lookups (
    entity  TEXT NOT NULL,   -- 'status','priority','type','category','helpdesk','agent'
    id      TEXT NOT NULL,
    name    TEXT NOT NULL,
    PRIMARY KEY (entity, id)
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_status    ON tickets(status_id);
CREATE INDEX IF NOT EXISTS idx_tickets_created   ON tickets(created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_updated   ON tickets(last_update);
CREATE INDEX IF NOT EXISTS idx_tickets_agent     ON tickets(assigned_id);
CREATE INDEX IF NOT EXISTS idx_tickets_helpdesk  ON tickets(assigned_group_id);
CREATE INDEX IF NOT EXISTS idx_tickets_category  ON tickets(category_id);
"""


def init_db(con):
    con.executescript(SCHEMA)
    con.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def to_epoch(val):
    if not val:
        return None
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def build_lookup(data, id_field="id", name_field="name"):
    if not data:
        return {}
    return {
        str(item[id_field]): item.get(name_field, f"ID {item[id_field]}")
        for item in data if id_field in item
    }


def upsert_ticket(con, t):
    con.execute("""
        INSERT INTO tickets
            (id, title, status_id, priority_id, type_id, category_id,
             assigned_id, assigned_group_id, user_id, creator_id, source_id,
             created_at, solved_at, closed_at, last_update,
             sla_resolution, sla_first_reply, rating,
             sentiment_initial, sentiment_current, raw_json)
        VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            title               = excluded.title,
            status_id           = excluded.status_id,
            priority_id         = excluded.priority_id,
            type_id             = excluded.type_id,
            category_id         = excluded.category_id,
            assigned_id         = excluded.assigned_id,
            assigned_group_id   = excluded.assigned_group_id,
            user_id             = excluded.user_id,
            last_update         = excluded.last_update,
            solved_at           = excluded.solved_at,
            closed_at           = excluded.closed_at,
            sla_resolution      = excluded.sla_resolution,
            sla_first_reply     = excluded.sla_first_reply,
            rating              = excluded.rating,
            sentiment_initial   = excluded.sentiment_initial,
            sentiment_current   = excluded.sentiment_current,
            raw_json            = excluded.raw_json
    """, (
        t.get("id"),
        t.get("title"),
        t.get("status_id"),
        t.get("priority_id"),
        t.get("type_id"),
        t.get("category_id"),
        t.get("assigned_id"),
        t.get("assigned_group_id"),
        t.get("user_id"),
        t.get("creator_id"),
        t.get("source_id"),
        to_epoch(t.get("created_at")),
        to_epoch(t.get("solved_at")),
        to_epoch(t.get("closed_at")),
        to_epoch(t.get("last_update")),
        t.get("sla_incident_resolution"),
        t.get("sla_incident_first_reply"),
        t.get("rating"),
        t.get("request_customer_sentiment_initial"),
        t.get("request_customer_sentiment_current"),
        json.dumps(t, ensure_ascii=False),
    ))


def upsert_lookups(con, entity, lookup_dict):
    con.executemany(
        "INSERT OR REPLACE INTO lookups (entity, id, name) VALUES (?,?,?)",
        [(entity, k, v) for k, v in lookup_dict.items()]
    )


def get_last_run(con):
    row = con.execute("SELECT value FROM meta WHERE key='last_run_ts'").fetchone()
    return float(row[0]) if row else None


def set_last_run(con, ts):
    con.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('last_run_ts',?)", (str(ts),))


def get_all_ids_by_status(status_lookup):
    print("  Consultando IDs por estado...")
    result = {}
    for sid, sname in status_lookup.items():
        all_ids = []
        offset  = 0
        while True:
            r = get("incidents.by.status", {"status_id": int(sid), "limit": 1000, "offset": offset})
            if not r or r.get("status") != "OK":
                break
            batch  = r.get("requestIds", [])
            total  = r.get("total", 0)
            all_ids.extend(batch)
            offset += len(batch)
            if offset >= total or not batch:
                break
        result[sid] = {"name": sname, "count": len(all_ids), "ids": all_ids}
        print(f"    '{sname}': {len(all_ids)} tickets")
    return result


def find_delta(con, all_ids, last_run_ts, force_full):
    if force_full or last_run_ts is None:
        print(f"  Modo COMPLETO: {len(all_ids)} tickets")
        return set(all_ids)

    # IDs que ya tenemos en DB con su last_update
    rows = con.execute("SELECT id, last_update FROM tickets").fetchall()
    cached = {row[0]: row[1] for row in rows}

    new_ids     = {i for i in all_ids if i not in cached}
    updated_ids = {i for i in all_ids if i in cached and
                   (cached[i] is None or cached[i] > last_run_ts)}

    delta = new_ids | updated_ids
    print(f"  Modo INCREMENTAL:")
    print(f"    En sistema : {len(all_ids)} | En DB: {len(cached)}")
    print(f"    Nuevos     : {len(new_ids)} | Modificados: {len(updated_ids)}")
    print(f"    A descargar: {len(delta)}")
    return delta


def fetch_parallel(ids):
    if not ids:
        print("  Nada que descargar.")
        return []
    ids   = list(ids)
    total = len(ids)
    done  = errors = 0
    result = []
    print(f"  Descargando {total} tickets ({MAX_WORKERS} hilos)...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_ticket, tid): tid for tid in ids}
        for future in as_completed(futures):
            done += 1
            t = future.result()
            if t and isinstance(t, dict) and "id" in t:
                result.append(t)
            else:
                errors += 1
            if done % 50 == 0 or done == total:
                print(f"    {done}/{total} | ok: {len(result)} | err: {errors}")
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    run_ts = datetime.now(tz=timezone.utc).timestamp()
    print("=" * 60)
    print(f"InvGate Extractor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Instancia: {BASE}")
    print(f"DB: {DB_PATH}")
    print("=" * 60)

    con = sqlite3.connect(DB_PATH)
    init_db(con)

    # 1. Lookups
    print("\n[1/4] Cargando taxonomia...")
    statuses   = get("incident.attributes.status")     or []
    priorities = get("incident.attributes.priority")   or []
    types      = get("incident.attributes.type")       or []
    categories = get("categories", {"page_size": 500}) or []
    helpdesks  = get("helpdesks")                      or []
    users_raw  = get("users")                          or []

    status_lkp   = build_lookup(statuses)
    priority_lkp = build_lookup(priorities)
    type_lkp     = build_lookup(types)
    category_lkp = build_lookup(categories)
    helpdesk_lkp = build_lookup(helpdesks)
    agents       = [u for u in users_raw if u.get("user_type") == 1]
    agent_lkp    = {
        str(u["id"]): f"{u.get('name','').strip()} {u.get('lastname','').strip()}".strip()
        for u in agents
    }

    upsert_lookups(con, "status",   status_lkp)
    upsert_lookups(con, "priority", priority_lkp)
    upsert_lookups(con, "type",     type_lkp)
    upsert_lookups(con, "category", category_lkp)
    upsert_lookups(con, "helpdesk", helpdesk_lkp)
    upsert_lookups(con, "agent",    agent_lkp)
    con.commit()
    print(f"  Estados: {len(statuses)} | Prioridades: {len(priorities)} | Tipos: {len(types)}")
    print(f"  Categorias: {len(categories)} | Helpdesks: {len(helpdesks)} | Agentes: {len(agents)}")

    # 2. IDs actuales
    print("\n[2/4] Obteniendo IDs actuales...")
    ids_by_status   = get_all_ids_by_status(status_lkp)
    all_current_ids = list({i for s in ids_by_status.values() for i in s["ids"]})
    print(f"  Total IDs: {len(all_current_ids)}")

    # 3. Delta y descarga
    print("\n[3/4] Calculando delta y descargando...")
    last_run_ts = get_last_run(con)
    if last_run_ts:
        print(f"  Ultima corrida: {datetime.fromtimestamp(last_run_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    to_download = find_delta(con, all_current_ids, last_run_ts, FORCE_FULL)
    new_tickets = fetch_parallel(to_download)

    # 4. Persistir
    print("\n[4/4] Guardando en SQLite...")
    for t in new_tickets:
        upsert_ticket(con, t)
    set_last_run(con, run_ts)
    con.commit()

    # Guardar snapshot de status para el dashboard
    status_snapshot = json.dumps(
        {s["name"]: s["count"] for s in ids_by_status.values() if s["count"] > 0},
        ensure_ascii=False
    )
    con.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('status_snapshot',?)",
                (status_snapshot,))
    con.commit()
    con.close()

    elapsed = round(datetime.now(tz=timezone.utc).timestamp() - run_ts, 1)
    total_in_db = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    print(f"\nTickets en DB    : {total_in_db}")
    print(f"Descargados ahora: {len(new_tickets)}")
    print(f"Tiempo total     : {elapsed}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
