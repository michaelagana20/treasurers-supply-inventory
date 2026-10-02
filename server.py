import argparse
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape, quoteattr

APP_VERSION = "1.0.0"
GITHUB_REPOSITORY = os.environ.get("TSI_GITHUB_REPOSITORY", "michaelagana20/treasurers-supply-inventory")
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
DATA_DIR = Path(os.environ.get("TSI_DATA_DIR", APP_DIR / "data"))
DB_PATH = DATA_DIR / "inventory.db"
BACKUP_DIR = APP_DIR / "backups"
TRAY_CONFIG_PATH = DATA_DIR / "tray-settings.json"
STARTUP_VALUE_NAME = "TreasurersSupplyInventory"


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
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER,
 summary TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
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


def inventory_as_of(con, end=""):
    if not end:
        return inventory(con)
    data = rows(con.execute("""
        SELECT s.id supply_id,s.name supply_name,v.id variant_id,v.name variant_name,v.detail_name,v.is_direct,
          COALESCE((SELECT SUM(quantity) FROM stock_additions a WHERE a.variant_id=v.id AND date(a.created_at)<=?),0) added,
          COALESCE((SELECT SUM(released_qty) FROM request_items i JOIN requests r ON r.id=i.request_id
                    WHERE i.variant_id=v.id AND r.status='RELEASED' AND date(r.released_at)<=?),0) released,
          COALESCE((SELECT SUM(c.quantity) FROM corrections c JOIN request_items i ON i.id=c.request_item_id
                    WHERE i.variant_id=v.id AND date(c.created_at)<=?),0) corrected
        FROM supplies s JOIN variants v ON v.supply_id=s.id
        WHERE s.archived=0 AND v.archived=0 AND date(s.created_at)<=? AND date(v.created_at)<=?
        ORDER BY s.name,v.is_direct DESC,v.name""", (end,end,end,end,end)))
    for item in data:
        item["used"] = item["released"] - item["corrected"]
        item["stock"] = item["added"] - item["used"]
    return data


def add_event(con, event_type, entity_type, entity_id, summary, details):
    con.execute("INSERT INTO audit_events(event_type,entity_type,entity_id,summary,details,created_at) VALUES(?,?,?,?,?,?)",
                (event_type, entity_type, entity_id, summary, json.dumps(details, ensure_ascii=False), now()))


def recent_activity(con):
    events = rows(con.execute("SELECT id,event_type,entity_type,entity_id,summary,details,created_at FROM audit_events ORDER BY id DESC"))
    for e in events:
        try: e["details"] = json.loads(e["details"])
        except Exception: e["details"] = {}
    for a in rows(con.execute("""SELECT a.id,a.quantity,a.remarks,a.created_at,s.name supply_name,v.name variant_name,v.is_direct
            FROM stock_additions a JOIN variants v ON v.id=a.variant_id JOIN supplies s ON s.id=v.supply_id ORDER BY a.id DESC""")):
        events.append({"id":a["id"],"event_type":"STOCK_ADDED","entity_type":"stock_addition","entity_id":a["id"],
            "summary":f"Added {a['quantity']} to {a['supply_name'] if a['is_direct'] else a['supply_name']+' — '+a['variant_name']}",
            "details":{"Supply":a["supply_name"],"Variant":"Direct stock" if a["is_direct"] else a["variant_name"],"Quantity added":a["quantity"],"Remarks":a["remarks"] or "No remarks"},"created_at":a["created_at"]})
    for r in rows(con.execute("""SELECT r.id,r.status,r.created_at,r.released_at,q.name requestor,
            (SELECT COUNT(*) FROM request_items i WHERE i.request_id=r.id) item_count FROM requests r JOIN requestors q ON q.id=r.requestor_id ORDER BY r.id DESC""")):
        when = r["released_at"] if r["status"] == "RELEASED" else r["created_at"]
        events.append({"id":r["id"],"event_type":r["status"],"entity_type":"request","entity_id":r["id"],
            "summary":f"Request #{r['id']} {'released to' if r['status']=='RELEASED' else 'submitted by'} {r['requestor']}",
            "details":{"Request number":r["id"],"Requestor":r["requestor"],"Status":r["status"].title(),"Line items":r["item_count"]},"created_at":when})
    for c in rows(con.execute("""SELECT c.id,c.quantity,c.remarks,c.created_at,r.id request_id,s.name supply_name,v.name variant_name,v.is_direct,q.name requestor
            FROM corrections c JOIN request_items i ON i.id=c.request_item_id JOIN requests r ON r.id=i.request_id JOIN requestors q ON q.id=r.requestor_id
            JOIN variants v ON v.id=i.variant_id JOIN supplies s ON s.id=v.supply_id ORDER BY c.id DESC""")):
        events.append({"id":c["id"],"event_type":"CORRECTION","entity_type":"correction","entity_id":c["id"],
            "summary":f"Returned {c['quantity']} to {c['supply_name'] if c['is_direct'] else c['supply_name']+' — '+c['variant_name']}",
            "details":{"Request number":c["request_id"],"Requestor":c["requestor"],"Quantity returned":c["quantity"],"Remarks":c["remarks"]},"created_at":c["created_at"]})
    return sorted(events, key=lambda x:x["created_at"], reverse=True)


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


def report_rows(con, report_type, start="", end=""):
    date_clause, params = "", []
    if start:
        date_clause += " AND date({date_field})>=?"
        params.append(start)
    if end:
        date_clause += " AND date({date_field})<=?"
        params.append(end)
    if report_type == "inventory":
        data = inventory_as_of(con, end)
        return {
            "name": "Inventory Status",
            "headers": ["Main Supply", "Stock Type", "Variant", "Detailed Name", "Total Added", "Released", "Returned", "Net Used", "Remaining", "% Remaining", "Stock Status"],
            "rows": [[x["supply_name"], "Direct stock" if x["is_direct"] else "Variant", "" if x["is_direct"] else x["variant_name"], x["detail_name"] or "", x["added"], x["released"], x["corrected"], x["used"], x["stock"], (x["stock"] / x["added"]) if x["added"] else 0, "Out of stock" if x["stock"] <= 0 else "Low" if x["added"] and x["stock"] / x["added"] <= .2 else "Available"] for x in data],
            "percent_cols": {9}
        }
    if report_type == "stock":
        sql = """SELECT a.created_at,s.name supply_name,v.name variant_name,v.detail_name,v.is_direct,a.quantity,a.remarks
            FROM stock_additions a JOIN variants v ON v.id=a.variant_id JOIN supplies s ON s.id=v.supply_id
            WHERE 1=1 {filters} ORDER BY a.created_at DESC,a.id DESC""".format(filters=date_clause.format(date_field="a.created_at"))
        data = rows(con.execute(sql, params))
        return {"name":"Stock Additions","headers":["Date Added","Main Supply","Stock Type","Variant","Detailed Name","Quantity Added","Remarks"],
            "rows":[[x["created_at"],x["supply_name"],"Direct stock" if x["is_direct"] else "Variant","" if x["is_direct"] else x["variant_name"],x["detail_name"] or "",x["quantity"],x["remarks"] or ""] for x in data], "date_cols":{0}}
    if report_type == "requests":
        sql = """SELECT r.id,r.status,r.created_at,r.released_at,q.name requestor,s.name supply_name,v.name variant_name,v.detail_name,v.is_direct,
            i.requested_qty,COALESCE(i.released_qty,0) released_qty,COALESCE((SELECT SUM(c.quantity) FROM corrections c WHERE c.request_item_id=i.id),0) returned_qty
            FROM requests r JOIN requestors q ON q.id=r.requestor_id JOIN request_items i ON i.request_id=r.id
            JOIN variants v ON v.id=i.variant_id JOIN supplies s ON s.id=v.supply_id
            WHERE 1=1 {filters} ORDER BY r.created_at DESC,r.id DESC,i.id""".format(filters=date_clause.format(date_field="COALESCE(r.released_at,r.created_at)"))
        data = rows(con.execute(sql, params))
        return {"name":"Requests and Releases","headers":["Request No.","Requestor","Status","Requested Date","Released Date","Main Supply","Variant","Detailed Name","Requested Qty","Released Qty","Returned Qty","Net Used"],
            "rows":[[x["id"],x["requestor"],x["status"].title(),x["created_at"],x["released_at"] or "",x["supply_name"],"Direct stock" if x["is_direct"] else x["variant_name"],x["detail_name"] or "",x["requested_qty"],x["released_qty"],x["returned_qty"],x["released_qty"]-x["returned_qty"]] for x in data], "date_cols":{3,4}}
    if report_type == "corrections":
        sql = """SELECT c.created_at,r.id request_id,q.name requestor,s.name supply_name,v.name variant_name,v.detail_name,v.is_direct,
            i.released_qty,c.quantity,c.remarks
            FROM corrections c JOIN request_items i ON i.id=c.request_item_id JOIN requests r ON r.id=i.request_id
            JOIN requestors q ON q.id=r.requestor_id JOIN variants v ON v.id=i.variant_id JOIN supplies s ON s.id=v.supply_id
            WHERE 1=1 {filters} ORDER BY c.created_at DESC,c.id DESC""".format(filters=date_clause.format(date_field="c.created_at"))
        data = rows(con.execute(sql, params))
        return {"name":"Corrections","headers":["Correction Date","Request No.","Requestor","Main Supply","Variant","Detailed Name","Original Released Qty","Quantity Returned","Remarks"],
            "rows":[[x["created_at"],x["request_id"],x["requestor"],x["supply_name"],"Direct stock" if x["is_direct"] else x["variant_name"],x["detail_name"] or "",x["released_qty"],x["quantity"],x["remarks"]] for x in data], "date_cols":{0}}
    raise APIError("Unknown report type.")


def excel_column(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xml_text(value):
    value = "" if value is None else str(value)
    return escape("".join(ch for ch in value if ch in "\t\n\r" or ord(ch) >= 32))


def excel_datetime(value):
    parsed = datetime.fromisoformat(str(value))
    parsed = parsed.replace(tzinfo=None)
    origin = datetime(1899, 12, 30)
    return (parsed - origin).total_seconds() / 86400


def friendly_date(value):
    if not value:
        return ""
    return datetime.strptime(value, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")


def build_xlsx(sheets):
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    created_label = datetime.now().astimezone().strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    sheet_xml, rels, workbook_sheets, content_sheets = [], [], [], []
    for sheet_index, spec in enumerate(sheets, 1):
        headers, data = spec["headers"], spec["rows"]
        columns = max(1, len(headers)); last_col = excel_column(columns); last_row = 4 + len(data)
        widths = []
        for col in range(columns):
            sample = [headers[col]] + [row[col] if col < len(row) else "" for row in data[:250]]
            widths.append(min(45, max(11, max(len(str(v or "")) for v in sample) + 2)))
        rows_xml = [f'<row r="1" ht="24" customHeight="1"><c r="A1" s="1" t="inlineStr"><is><t>{xml_text(spec["name"])}</t></is></c></row>',
                    f'<row r="2"><c r="A2" s="3" t="inlineStr"><is><t>Generated {xml_text(created_label)} · Treasurer\'s Office Supply Inventory</t></is></c></row>']
        header_cells = "".join(f'<c r="{excel_column(i+1)}4" s="2" t="inlineStr"><is><t>{xml_text(value)}</t></is></c>' for i, value in enumerate(headers))
        rows_xml.append(f'<row r="4" ht="22" customHeight="1">{header_cells}</row>')
        percent_cols = spec.get("percent_cols", set())
        date_cols = spec.get("date_cols", set())
        for row_number, values in enumerate(data, 5):
            cells = []
            for col, value in enumerate(values):
                ref = f"{excel_column(col+1)}{row_number}"
                if col in date_cols and value:
                    cells.append(f'<c r="{ref}" s="5"><v>{excel_datetime(value)}</v></c>')
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}" s="{4 if col in percent_cols else 0}"><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{xml_text(value)}</t></is></c>')
            rows_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        cols_xml = "".join(f'<col min="{i+1}" max="{i+1}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths))
        merge = f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>' if columns > 1 else ""
        auto_filter = f'<autoFilter ref="A4:{last_col}{last_row}"/>' if data else ""
        sheet_xml.append(f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>{cols_xml}</cols><sheetData>{"".join(rows_xml)}</sheetData>{merge}{auto_filter}<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>''')
        workbook_sheets.append(f'<sheet name={quoteattr(spec["name"][:31])} sheetId="{sheet_index}" r:id="rId{sheet_index}"/>')
        rels.append(f'<Relationship Id="rId{sheet_index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_index}.xml"/>')
        content_sheets.append(f'<Override PartName="/xl/worksheets/sheet{sheet_index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="mmm d, yyyy h:mm AM/PM"/></numFmts><fonts count="4"><font><sz val="10"/><name val="Aptos"/></font><font><b/><sz val="16"/><color rgb="FF063F74"/><name val="Aptos"/></font><font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font><font><i/><sz val="9"/><color rgb="FF617789"/><name val="Aptos"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF075CA8"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border/><border><bottom style="thin"><color rgb="FFD8E4ED"/></bottom></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="10" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/><xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>{''.join(content_sheets)}</Types>''')
        book.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>''')
        book.writestr("docProps/core.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>Treasurer's Office Supply Inventory Reports</dc:title><dc:creator>Samboy</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{xml_text(created)}</dcterms:created></cp:coreProperties>''')
        book.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{''.join(workbook_sheets)}</sheets></workbook>''')
        book.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{''.join(rels)}<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        book.writestr("xl/styles.xml", styles)
        for index, content in enumerate(sheet_xml, 1):
            book.writestr(f"xl/worksheets/sheet{index}.xml", content)
    return stream.getvalue()


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RESOURCE_DIR / "web"), **kwargs)

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

    def send_xlsx(self, content, filename):
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def body(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            raise APIError("Invalid request data.")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
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
                    archived_variants = rows(con.execute("""SELECT v.id variant_id,v.supply_id,v.name variant_name,v.detail_name,s.name supply_name,
                        COALESCE((SELECT SUM(quantity) FROM stock_additions a WHERE a.variant_id=v.id),0) added,
                        COALESCE((SELECT SUM(released_qty) FROM request_items i JOIN requests r ON r.id=i.request_id WHERE i.variant_id=v.id AND r.status='RELEASED'),0) released,
                        COALESCE((SELECT SUM(c.quantity) FROM corrections c JOIN request_items i ON i.id=c.request_item_id WHERE i.variant_id=v.id),0) corrected
                        FROM variants v JOIN supplies s ON s.id=v.supply_id WHERE v.archived=1 AND s.archived=0 ORDER BY s.name,v.name"""))
                    for v in archived_variants:
                        v["used"] = v["released"] - v["corrected"]
                        v["stock"] = v["added"] - v["used"]
                    for s in supplies:
                        s["archived_variants"] = [v for v in archived_variants if v["supply_id"] == s["id"]]
                    result = {
                        "version": APP_VERSION, "repository": GITHUB_REPOSITORY,
                        "local_url": f"http://127.0.0.1:{self.server.server_port}", "supplies": supplies,
                        "requestors": rows(con.execute("SELECT name FROM requestors ORDER BY name")),
                        "requested": request_data(con, "REQUESTED"), "released": request_data(con, "RELEASED"),
                        "recent_additions": rows(con.execute("""SELECT a.id,a.quantity,a.remarks,a.created_at,s.name supply_name,v.name variant_name,v.is_direct
                            FROM stock_additions a JOIN variants v ON v.id=a.variant_id JOIN supplies s ON s.id=v.supply_id ORDER BY a.id DESC LIMIT 20""")),
                        "corrections": rows(con.execute("""SELECT c.id,c.quantity,c.remarks,c.created_at,i.released_qty,r.id request_id,
                            s.name supply_name,v.name variant_name,v.is_direct,q.name requestor
                            FROM corrections c JOIN request_items i ON i.id=c.request_item_id JOIN requests r ON r.id=i.request_id
                            JOIN requestors q ON q.id=r.requestor_id JOIN variants v ON v.id=i.variant_id JOIN supplies s ON s.id=v.supply_id
                            ORDER BY c.id DESC LIMIT 30""")),
                        "activities": recent_activity(con), "backup_dir": str(BACKUP_DIR),
                        "backups": [p.name for p in sorted(BACKUP_DIR.glob("*.db"), reverse=True)] if BACKUP_DIR.exists() else []
                    }
                    return self.send_json(result)
                if path == "/api/reports/excel":
                    query = parse_qs(parsed.query)
                    report_type = query.get("type", ["complete"])[0]
                    start, end = query.get("start", [""])[0], query.get("end", [""])[0]
                    for value in (start, end):
                        if value:
                            try: datetime.strptime(value, "%Y-%m-%d")
                            except ValueError: raise APIError("Use valid report dates.")
                    if start and end and start > end: raise APIError("Start date cannot be after end date.")
                    report_types = ["inventory", "stock", "requests", "corrections"] if report_type == "complete" else [report_type]
                    sheets = []
                    if report_type == "complete":
                        inv = inventory_as_of(con, end)
                        if end:
                            pending = con.execute("SELECT COUNT(*) FROM requests WHERE date(created_at)<=? AND (released_at IS NULL OR date(released_at)>?)", (end,end)).fetchone()[0]
                            released = con.execute("SELECT COUNT(*) FROM requests WHERE released_at IS NOT NULL AND date(released_at)<=?", (end,)).fetchone()[0]
                        else:
                            pending = con.execute("SELECT COUNT(*) FROM requests WHERE status='REQUESTED'").fetchone()[0]
                            released = con.execute("SELECT COUNT(*) FROM requests WHERE status='RELEASED'").fetchone()[0]
                        sheets.append({"name":"Report Summary","headers":["Metric","Value"],"rows":[
                            ["Active main supplies",len({x["supply_id"] for x in inv})],
                            ["Tracked stock items",len(inv)],["Current units remaining",sum(x["stock"] for x in inv)],
                            ["Pending requests",pending],["Completed releases",released],
                            ["Inventory snapshot",f"As of {friendly_date(end)}" if end else "Current"],
                            ["Transaction report period",f"{friendly_date(start) if start else 'All dates'} to {friendly_date(end) if end else 'Present'}"]]})
                    sheets.extend(report_rows(con, kind, start, end) for kind in report_types)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    filename = f"supply-{report_type}-report-{stamp}.xlsx"
                    return self.send_xlsx(build_xlsx(sheets), filename)
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
                elif path == "/api/supplies/bulk":
                    entries = data.get("entries") or []
                    if not entries: raise APIError("Add at least one bulk-entry row.")
                    grouped = {}
                    for number, item in enumerate(entries, 1):
                        supply = str(item.get("supply", "")).strip()
                        variant = str(item.get("variant", "")).strip()
                        detail = str(item.get("detail_name", "")).strip()
                        qty = int(item.get("quantity") or 0)
                        if not supply: raise APIError(f"Row {number}: supply name is required.")
                        if qty < 0: raise APIError(f"Row {number}: quantity cannot be negative.")
                        key = supply.casefold()
                        grouped.setdefault(key, {"name":supply,"rows":[]})["rows"].append({"variant":variant,"detail":detail,"quantity":qty,"row":number})
                    for group in grouped.values():
                        if con.execute("SELECT 1 FROM supplies WHERE name=? COLLATE NOCASE", (group["name"],)).fetchone():
                            raise APIError(f"Supply already exists: {group['name']}")
                        has_variants = any(x["variant"] for x in group["rows"])
                        if has_variants and any(not x["variant"] for x in group["rows"]):
                            raise APIError(f"{group['name']}: do not mix blank and named variants.")
                        if not has_variants and len(group["rows"]) > 1:
                            raise APIError(f"{group['name']}: direct supplies may appear only once.")
                        names = [x["variant"].casefold() for x in group["rows"] if x["variant"]]
                        if len(names) != len(set(names)): raise APIError(f"{group['name']}: duplicate variant names.")
                    created = 0
                    for group in grouped.values():
                        sid = con.execute("INSERT INTO supplies(name,created_at) VALUES(?,?)", (group["name"],now())).lastrowid
                        for item in group["rows"]:
                            direct = not item["variant"]
                            vid = con.execute("INSERT INTO variants(supply_id,name,detail_name,is_direct,created_at) VALUES(?,?,?,?,?)",
                                (sid,"__DIRECT__" if direct else item["variant"],item["detail"],1 if direct else 0,now())).lastrowid
                            if item["quantity"]:
                                con.execute("INSERT INTO stock_additions(variant_id,quantity,remarks,created_at) VALUES(?,?,?,?)", (vid,item["quantity"],"Bulk starting stock",now()))
                        add_event(con,"SUPPLY_CREATED","supply",sid,f"Bulk-created supply {group['name']}",{"Supply":group["name"],"Items":len(group["rows"])})
                        created += 1
                    return self.send_json({"ok":True,"created":created})
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
                    supply = con.execute("SELECT name FROM supplies WHERE id=? AND archived=0", (sid,)).fetchone()
                    if not supply: raise APIError("Supply not found.")
                    balances = rows(con.execute(inventory_sql("AND s.id=?"), (sid,)))
                    remaining = sum(x["added"] - (x["released"] - x["corrected"]) for x in balances)
                    if remaining != 0: raise APIError(f"This supply still has {remaining} remaining. It can only be archived when fully used.")
                    con.execute("UPDATE supplies SET archived=1 WHERE id=?", (sid,))
                    add_event(con,"SUPPLY_ARCHIVED","supply",sid,f"Archived supply {supply['name']}",{"Supply":supply["name"],"Remaining stock":remaining})
                elif path == "/api/archive/variant":
                    vid = int(data.get("variant_id"))
                    pending = con.execute("""SELECT COUNT(*) FROM request_items i JOIN requests r ON r.id=i.request_id
                        WHERE i.variant_id=? AND r.status='REQUESTED'""", (vid,)).fetchone()[0]
                    if pending: raise APIError("This variant is used by a pending request. Release that request before archiving it.")
                    variant = con.execute("SELECT v.is_direct,v.name,s.name supply_name FROM variants v JOIN supplies s ON s.id=v.supply_id WHERE v.id=? AND v.archived=0", (vid,)).fetchone()
                    if not variant: raise APIError("Variant not found.")
                    if variant["is_direct"]: raise APIError("Archive the main supply instead of its direct stock record.")
                    balance = con.execute(inventory_sql("AND v.id=?"), (vid,)).fetchone()
                    remaining = balance["added"] - (balance["released"] - balance["corrected"])
                    if remaining != 0: raise APIError(f"This variant still has {remaining} remaining. It can only be archived when fully used.")
                    con.execute("UPDATE variants SET archived=1 WHERE id=?", (vid,))
                    add_event(con,"VARIANT_ARCHIVED","variant",vid,f"Archived variant {variant['supply_name']} — {variant['name']}",
                              {"Supply":variant["supply_name"],"Variant":variant["name"],"Remaining stock":remaining})
                elif path == "/api/restore/variant":
                    vid = int(data.get("variant_id"))
                    variant = con.execute("""SELECT v.name,s.name supply_name,s.archived supply_archived FROM variants v
                        JOIN supplies s ON s.id=v.supply_id WHERE v.id=? AND v.archived=1""", (vid,)).fetchone()
                    if not variant: raise APIError("Archived variant not found.")
                    if variant["supply_archived"]: raise APIError("Restore the main supply before restoring this variant.")
                    con.execute("UPDATE variants SET archived=0 WHERE id=?", (vid,))
                    add_event(con,"VARIANT_RESTORED","variant",vid,f"Restored variant {variant['supply_name']} — {variant['name']}",
                              {"Supply":variant["supply_name"],"Variant":variant["name"]})
                elif path == "/api/backup":
                    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    dest = BACKUP_DIR / (datetime.now().strftime("inventory-%Y%m%d-%H%M%S") + ".db")
                    out = sqlite3.connect(dest); con.backup(out); out.close()
                    return self.send_json({"ok": True, "file": dest.name})
                elif path == "/api/reset":
                    if data.get("confirm") != "RESET DATABASE": raise APIError("Type RESET DATABASE to confirm.")
                    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    dest = BACKUP_DIR / (datetime.now().strftime("before-reset-%Y%m%d-%H%M%S") + ".db")
                    out = sqlite3.connect(dest); con.backup(out); out.close()
                    con.execute("BEGIN IMMEDIATE")
                    for table in ("corrections","request_items","requests","requestors","stock_additions","variants","supplies","audit_events"):
                        con.execute(f"DELETE FROM {table}")
                    con.commit()
                    return self.send_json({"ok": True, "safety_backup": dest.name})
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


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def load_tray_config():
    defaults = {"open_behavior": "new_tab"}
    try:
        saved = json.loads(TRAY_CONFIG_PATH.read_text(encoding="utf-8"))
        if saved.get("open_behavior") in {"new_tab", "new_window", "minimized"}:
            defaults.update(saved)
    except (OSError, ValueError, TypeError):
        pass
    return defaults


def save_tray_config(config):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRAY_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def startup_enabled():
    if os.name != "nt" or not FROZEN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
        return Path(value.strip().strip('"')).resolve() == Path(sys.executable).resolve()
    except (OSError, ValueError):
        return False


def set_startup(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, STARTUP_VALUE_NAME)
            except FileNotFoundError:
                pass


def open_browser_url(url, behavior="new_tab"):
    if behavior == "new_window":
        webbrowser.open_new(url)
    elif behavior != "minimized":
        webbrowser.open_new_tab(url)


def tray_image():
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((3, 3, 61, 61), fill="#075ca8", outline="#ffd43b", width=4)
    draw.polygon(((14, 26), (32, 13), (50, 26)), fill="#ffd43b")
    draw.rectangle((16, 27, 48, 32), fill="#ffffff")
    for left in (19, 29, 39):
        draw.rectangle((left, 32, left + 6, 47), fill="#ffffff")
    draw.rectangle((14, 47, 50, 52), fill="#ffd43b")
    return image


def run_with_tray(server, url, open_browser=True):
    import pystray
    config = load_tray_config()

    def open_app(icon=None, item=None):
        webbrowser.open_new_tab(url)

    def open_data(icon=None, item=None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(DATA_DIR)

    def exit_app(icon, item=None):
        icon.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    def toggle_startup(icon, item=None):
        set_startup(not startup_enabled())
        icon.update_menu()

    def choose_behavior(behavior):
        def choose(icon, item=None):
            config["open_behavior"] = behavior
            save_tray_config(config)
            icon.update_menu()
        return choose

    behavior_menu = pystray.Menu(
        pystray.MenuItem("Open a new tab", choose_behavior("new_tab"), checked=lambda item: config["open_behavior"] == "new_tab", radio=True),
        pystray.MenuItem("Open a new browser window", choose_behavior("new_window"), checked=lambda item: config["open_behavior"] == "new_window", radio=True),
        pystray.MenuItem("Start without opening browser", choose_behavior("minimized"), checked=lambda item: config["open_behavior"] == "minimized", radio=True),
    )

    menu = pystray.Menu(
        pystray.MenuItem("Open Supply Inventory", open_app, default=True),
        pystray.MenuItem("Run when Windows starts", toggle_startup, checked=lambda item: startup_enabled()),
        pystray.MenuItem("When app starts", behavior_menu),
        pystray.MenuItem("Open Data Folder", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_app),
    )
    icon = pystray.Icon("TreasurersSupplyInventory", tray_image(), "Treasurer's Supply Inventory", menu)
    thread = threading.Thread(target=server.serve_forever, name="inventory-server", daemon=True)
    thread.start()
    if open_browser:
        threading.Timer(.8, lambda: open_browser_url(url, config["open_behavior"])).start()
    try:
        icon.run()
    finally:
        server.shutdown()
        server.server_close()


def create_local_server(requested_port=None):
    if requested_port is not None:
        server = LocalHTTPServer(("127.0.0.1", requested_port), Handler)
        return server, server.server_port
    last_error = None
    for port in range(8765, 8865):
        try:
            server = LocalHTTPServer(("127.0.0.1", port), Handler)
            return server, server.server_port
        except OSError as error:
            last_error = error
    try:
        server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        return server, server.server_port
    except OSError:
        raise last_error


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int); parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(); init_db()
    try:
        server, selected_port = create_local_server(args.port)
    except OSError as error:
        print(f"Could not start the local server: {error}")
        return
    url = f"http://localhost:{selected_port}"
    if args.port is None and selected_port != 8765:
        print(f"Port 8765 is already in use. Automatically selected port {selected_port}.")
    print(f"Treasurer's Supply Inventory v{APP_VERSION} running at {url}\nData: {DB_PATH}\nPress Ctrl+C to stop.")
    if FROZEN:
        return run_with_tray(server, url, not args.no_browser)
    if not args.no_browser: threading.Timer(.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
