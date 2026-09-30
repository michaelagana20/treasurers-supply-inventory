import argparse
import json
import os
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_VERSION = "1.0.0"
GITHUB_REPOSITORY = os.environ.get("TSI_GITHUB_REPOSITORY", "")
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TSI_DATA_DIR", Path(os.environ.get("LOCALAPPDATA", ROOT)) / "TreasurersSupplyInventory"))
DB_PATH = DATA_DIR / "inventory.db"
BACKUP_DIR = DATA_DIR / "backups"


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS supplies (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, created_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS variants (
 id INTEGER PRIMARY KEY, supply_id INTEGER NOT NULL REFERENCES supplies(id), name TEXT NOT NULL COLLATE NOCASE,
 detail_name TEXT, is_direct INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(supply_id, name)
);
CREATE TABLE IF NOT EXISTS stock_additions (
 id INTEGER PRIMARY KEY, variant_id INTEGER NOT NULL REFERENCES variants(id), quantity INTEGER NOT NULL CHECK(quantity > 0),
 remarks TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requestors (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY, requestor_id INTEGER NOT NULL REFERENCES requestors(id), status TEXT NOT NULL DEFAULT 'REQUESTED',
 created_at TEXT NOT NULL, released_at TEXT
);
CREATE TABLE IF NOT EXISTS request_items (
 id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id), variant_id INTEGER NOT NULL REFERENCES variants(id),
 requested_qty INTEGER NOT NULL CHECK(requested_qty > 0), released_qty INTEGER CHECK(released_qty >= 0)
);
CREATE TABLE IF NOT EXISTS corrections (
 id INTEGER PRIMARY KEY, request_item_id INTEGER NOT NULL REFERENCES request_items(id), quantity INTEGER NOT NULL CHECK(quantity > 0),
 remarks TEXT NOT NULL CHECK(length(trim(remarks)) > 0), created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_add_variant ON stock_additions(variant_id);
CREATE INDEX IF NOT EXISTS idx_item_variant ON request_items(variant_id);
CREATE INDEX IF NOT EXISTS idx_correction_item ON corrections(request_item_id);
"""


def init_db():
    with connect() as con:
        con.executescript(SCHEMA)
        columns = {row[1] for row in con.execute("PRAGMA table_info(variants)")}
        if "archived" not in columns:
            con.execute("ALTER TABLE variants ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def rows(cur):
    return [dict(x) for x in cur.fetchall()]


def inventory_sql(where=""):
    return f"""
    SELECT s.id supply_id,s.name supply_name,v.id variant_id,v.name variant_name,v.detail_name,v.is_direct,
      COALESCE((SELECT SUM(quantity) FROM stock_additions a WHERE a.variant_id=v.id),0) added,
      COALESCE((SELECT SUM(released_qty) FROM request_items i JOIN requests r ON r.id=i.request_id
                WHERE i.variant_id=v.id AND r.status='RELEASED'),0) released,
      COALESCE((SELECT SUM(c.quantity) FROM corrections c JOIN request_items i ON i.id=c.request_item_id
                WHERE i.variant_id=v.id),0) corrected
    FROM supplies s JOIN variants v ON v.supply_id=s.id WHERE s.archived=0 AND v.archived=0 {where}
    ORDER BY s.name, v.is_direct DESC, v.name"""


def inventory(con):
    data = rows(con.execute(inventory_sql()))
    for x in data:
        x["used"] = x["released"] - x["corrected"]
        x["stock"] = x["added"] - x["used"]
    return data


def request_data(con, status):
    reqs = rows(con.execute("""SELECT r.id,r.status,r.created_at,r.released_at,q.name requestor
        FROM requests r JOIN requestors q ON q.id=r.requestor_id WHERE r.status=? ORDER BY r.id DESC""", (status,)))
    for r in reqs:
        r["items"] = rows(con.execute("""SELECT i.id,i.variant_id,i.requested_qty,i.released_qty,s.name supply_name,
            v.name variant_name,v.detail_name,v.is_direct,
            COALESCE((SELECT SUM(quantity) FROM corrections c WHERE c.request_item_id=i.id),0) corrected
            FROM request_items i JOIN variants v ON v.id=i.variant_id JOIN supplies s ON s.id=v.supply_id
            WHERE i.request_id=? ORDER BY i.id""", (r["id"],)))
    return reqs


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            raise APIError("Invalid request data.")

    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return super().do_GET()
        try:
            with connect() as con:
                if path == "/api/state":
                    inv = inventory(con)
                    supplies = []
                    for v in inv:
                        match = next((s for s in supplies if s["id"] == v["supply_id"]), None)
                        if not match:
                            match = {"id": v["supply_id"], "name": v["supply_name"], "variants": []}
                            supplies.append(match)
                        match["variants"].append(v)
                    result = {
                        "version": APP_VERSION, "repository": GITHUB_REPOSITORY, "supplies": supplies,
                        "requestors": rows(con.execute("SELECT name FROM requestors ORDER BY name")),
                        "requested": request_data(con, "REQUESTED"), "released": request_data(con, "RELEASED"),
                        "recent_additions": rows(con.execute("""SELECT a.id,a.quantity,a.remarks,a.created_at,s.name supply_name,v.name variant_name,v.is_direct
                            FROM stock_additions a JOIN variants v ON v.id=a.variant_id JOIN supplies s ON s.id=v.supply_id ORDER BY a.id DESC LIMIT 20""")),
                        "corrections": rows(con.execute("""SELECT c.id,c.quantity,c.remarks,c.created_at,i.released_qty,r.id request_id,
                            s.name supply_name,v.name variant_name,v.is_direct,q.name requestor
                            FROM corrections c JOIN request_items i ON i.id=c.request_item_id JOIN requests r ON r.id=i.request_id
                            JOIN requestors q ON q.id=r.requestor_id JOIN variants v ON v.id=i.variant_id JOIN supplies s ON s.id=v.supply_id
                            ORDER BY c.id DESC LIMIT 30""")),
                        "backups": [p.name for p in sorted(BACKUP_DIR.glob("*.db"), reverse=True)] if BACKUP_DIR.exists() else []
                    }
                    return self.send_json(result)
                if path == "/api/update":
                    if not GITHUB_REPOSITORY:
                        return self.send_json({"configured": False, "message": "Set TSI_GITHUB_REPOSITORY to owner/repository to enable update checks."})
                    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest", headers={"User-Agent": "TreasurersSupplyInventory"})
                    try:
                        with urllib.request.urlopen(req, timeout=4) as res:
                            d = json.load(res)
                        latest = str(d.get("tag_name", "")).lstrip("v")
                        return self.send_json({"configured": True, "latest": latest, "current": APP_VERSION,
                            "available": tuple(map(int, latest.split("."))) > tuple(map(int, APP_VERSION.split("."))), "url": d.get("html_url")})
                    except Exception:
                        return self.send_json({"configured": True, "offline": True, "message": "Could not check for updates. The app remains fully usable offline."})
                raise APIError("Not found.", 404)
        except APIError as e:
            self.send_json({"error": e.message}, e.status)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        path, data = urlparse(self.path).path, self.body()
        try:
            if path == "/api/restore":
                return self.restore(data)
            with connect() as con:
                if path == "/api/supplies":
                    name = str(data.get("name", "")).strip()
                    variants = data.get("variants") or []
                    qty = int(data.get("quantity") or 0)
                    if not name: raise APIError("Supply name is required.")
                    if qty < 0: raise APIError("Starting quantity cannot be negative.")
                    try:
                        cur = con.execute("INSERT INTO supplies(name,created_at) VALUES(?,?)", (name, now()))
                    except sqlite3.IntegrityError: raise APIError("A supply with that name already exists.")
                    sid = cur.lastrowid
                    if variants:
                        for item in variants:
                            vn, detail, vqty = str(item.get("name", "")).strip(), str(item.get("detail_name", "")).strip(), int(item.get("quantity") or 0)
                            if not vn: raise APIError("Every variant needs a name.")
                            if vqty < 0: raise APIError("Variant quantity cannot be negative.")
                            vid = con.execute("INSERT INTO variants(supply_id,name,detail_name,is_direct,created_at) VALUES(?,?,?,?,?)", (sid,vn,detail,0,now())).lastrowid
                            if vqty: con.execute("INSERT INTO stock_additions(variant_id,quantity,remarks,created_at) VALUES(?,?,?,?)", (vid,vqty,"Starting stock",now()))
                    else:
                        vid = con.execute("INSERT INTO variants(supply_id,name,is_direct,created_at) VALUES(?,?,1,?)", (sid,"__DIRECT__",now())).lastrowid
                        if qty: con.execute("INSERT INTO stock_additions(variant_id,quantity,remarks,created_at) VALUES(?,?,?,?)", (vid,qty,"Starting stock",now()))
                elif path == "/api/variants":
                    sid, name = int(data.get("supply_id")), str(data.get("name", "")).strip()
                    if not name: raise APIError("Variant name is required.")
                    direct = con.execute(inventory_sql("AND s.id=? AND v.is_direct=1"), (sid,)).fetchone()
                    if direct:
                        used = direct["released"] - direct["corrected"]
                        balance = direct["added"] - used
                        if balance != 0 or direct["added"] != 0 or direct["released"] != 0:
                            raise APIError("This supply already has direct stock history. Create a new supply with variants; conversion is blocked to prevent loss or duplication.")
                        con.execute("DELETE FROM variants WHERE id=?", (direct["variant_id"],))
                    con.execute("INSERT INTO variants(supply_id,name,detail_name,is_direct,created_at) VALUES(?,?,?,?,?)",
                                (sid,name,str(data.get("detail_name", "")).strip(),0,now()))
                elif path == "/api/stock":
                    vid, qty = int(data.get("variant_id")), int(data.get("quantity") or 0)
                    if qty <= 0: raise APIError("Stock addition must be a positive whole number.")
                    con.execute("INSERT INTO stock_additions(variant_id,quantity,remarks,created_at) VALUES(?,?,?,?)", (vid,qty,str(data.get("remarks", "")).strip(),now()))
                elif path == "/api/requests":
                    person, items = str(data.get("requestor", "")).strip(), data.get("items") or []
                    if not person: raise APIError("Requestor name is required.")
                    if not items: raise APIError("Add at least one requested item.")
                    con.execute("INSERT OR IGNORE INTO requestors(name,created_at) VALUES(?,?)", (person,now()))
                    qid = con.execute("SELECT id FROM requestors WHERE name=? COLLATE NOCASE", (person,)).fetchone()[0]
                    rid = con.execute("INSERT INTO requests(requestor_id,created_at) VALUES(?,?)", (qid,now())).lastrowid
                    seen = set()
                    for item in items:
                        vid, qty = int(item.get("variant_id")), int(item.get("quantity") or 0)
                        if qty <= 0: raise APIError("Every requested quantity must be positive.")
                        if vid in seen: raise APIError("The same supply item cannot appear twice in one request.")
                        seen.add(vid); con.execute("INSERT INTO request_items(request_id,variant_id,requested_qty) VALUES(?,?,?)", (rid,vid,qty))
                elif path.startswith("/api/releases/"):
                    rid = int(path.rsplit("/",1)[1]); release = data.get("items") or []
                    req = con.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
                    if not req or req[0] != "REQUESTED": raise APIError("This request is no longer awaiting release.")
                    expected = rows(con.execute("SELECT id,variant_id FROM request_items WHERE request_id=?", (rid,)))
                    entered = {int(i["id"]): int(i.get("quantity") or 0) for i in release}
                    if set(entered) != {i["id"] for i in expected}: raise APIError("Provide an actual released quantity for every line.")
                    inv = {i["variant_id"]: i["stock"] for i in inventory(con)}
                    aggregate = {}
                    for item in expected:
                        qty = entered[item["id"]]
                        if qty < 0: raise APIError("Released quantity cannot be negative.")
                        aggregate[item["variant_id"]] = aggregate.get(item["variant_id"],0) + qty
                    for vid, qty in aggregate.items():
                        if qty > inv.get(vid,0): raise APIError("A released quantity exceeds available stock.")
                    for iid, qty in entered.items(): con.execute("UPDATE request_items SET released_qty=? WHERE id=?", (qty,iid))
                    con.execute("UPDATE requests SET status='RELEASED',released_at=? WHERE id=?", (now(),rid))
                elif path == "/api/corrections":
                    iid, qty, remarks = int(data.get("item_id")), int(data.get("quantity") or 0), str(data.get("remarks", "")).strip()
                    if qty <= 0: raise APIError("Return quantity must be a positive whole number.")
                    if not remarks: raise APIError("Remarks are required for a correction.")
                    item = con.execute("""SELECT i.released_qty,COALESCE(SUM(c.quantity),0) corrected,r.status FROM request_items i
                        JOIN requests r ON r.id=i.request_id LEFT JOIN corrections c ON c.request_item_id=i.id WHERE i.id=? GROUP BY i.id""", (iid,)).fetchone()
                    if not item or item["status"] != "RELEASED": raise APIError("Only a released line can be corrected.")
                    if qty + item["corrected"] > item["released_qty"]: raise APIError("Total returns cannot exceed the recorded released quantity.")
                    con.execute("INSERT INTO corrections(request_item_id,quantity,remarks,created_at) VALUES(?,?,?,?)", (iid,qty,remarks,now()))
                elif path == "/api/archive/supply":
                    sid = int(data.get("supply_id"))
                    pending = con.execute("""SELECT COUNT(*) FROM request_items i JOIN requests r ON r.id=i.request_id
                        JOIN variants v ON v.id=i.variant_id WHERE v.supply_id=? AND r.status='REQUESTED'""", (sid,)).fetchone()[0]
                    if pending: raise APIError("This supply is used by a pending request. Release that request before archiving it.")
                    if not con.execute("SELECT 1 FROM supplies WHERE id=? AND archived=0", (sid,)).fetchone(): raise APIError("Supply not found.")
                    con.execute("UPDATE supplies SET archived=1 WHERE id=?", (sid,))
                elif path == "/api/archive/variant":
                    vid = int(data.get("variant_id"))
                    pending = con.execute("""SELECT COUNT(*) FROM request_items i JOIN requests r ON r.id=i.request_id
                        WHERE i.variant_id=? AND r.status='REQUESTED'""", (vid,)).fetchone()[0]
                    if pending: raise APIError("This variant is used by a pending request. Release that request before archiving it.")
                    variant = con.execute("SELECT is_direct FROM variants WHERE id=? AND archived=0", (vid,)).fetchone()
                    if not variant: raise APIError("Variant not found.")
                    if variant["is_direct"]: raise APIError("Archive the main supply instead of its direct stock record.")
                    con.execute("UPDATE variants SET archived=1 WHERE id=?", (vid,))
                elif path == "/api/backup":
                    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    dest = BACKUP_DIR / (datetime.now().strftime("inventory-%Y%m%d-%H%M%S") + ".db")
                    out = sqlite3.connect(dest); con.backup(out); out.close()
                    return self.send_json({"ok": True, "file": dest.name})
                else: raise APIError("Not found.", 404)
            self.send_json({"ok": True})
        except APIError as e: self.send_json({"error": e.message}, e.status)
        except (ValueError, TypeError): self.send_json({"error": "Use valid whole-number quantities."}, 400)
        except sqlite3.IntegrityError as e: self.send_json({"error": "That entry conflicts with existing data: " + str(e)}, 400)
        except Exception as e: self.send_json({"error": str(e)}, 500)

    def restore(self, data):
        name = Path(str(data.get("file", ""))).name
        if data.get("confirm") != "RESTORE": raise APIError("Type RESTORE to confirm.")
        source = BACKUP_DIR / name
        if not source.exists() or source.suffix != ".db": raise APIError("Backup not found.")
        test = sqlite3.connect(source)
        ok = test.execute("PRAGMA integrity_check").fetchone()[0]; tables = {r[0] for r in test.execute("SELECT name FROM sqlite_master WHERE type='table'")}; test.close()
        if ok != "ok" or not {"supplies","requests","stock_additions"}.issubset(tables): raise APIError("Backup is not a valid inventory database.")
        safety = DATA_DIR / (datetime.now().strftime("before-restore-%Y%m%d-%H%M%S") + ".db")
        with connect() as con:
            out = sqlite3.connect(safety); con.backup(out); out.close()
        source_con = sqlite3.connect(source)
        target_con = sqlite3.connect(DB_PATH)
        source_con.backup(target_con)
        target_con.close(); source_con.close()
        self.send_json({"ok": True, "safety_copy": safety.name})


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8765); parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(); init_db()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Treasurer's Supply Inventory v{APP_VERSION} running at {url}\nData: {DB_PATH}\nPress Ctrl+C to stop.")
    if not args.no_browser: threading.Timer(.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
