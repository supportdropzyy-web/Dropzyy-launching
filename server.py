#!/usr/bin/env python3
"""
Dropzyy server: serves the launch page and saves restaurant / rider
sign-ups into a small SQLite file (dropzyy.db). No extra packages needed.

Run:
    ADMIN_PASSWORD="choose-a-strong-password" python3 server.py
Then open:
    http://localhost:8000          (website)
    http://localhost:8000/admin    (see sign-ups, login: admin / your password)

Optional environment variables:
    PORT, HOST, DB_PATH, ADMIN_USER, ADMIN_PASSWORD, TRUST_PROXY=1
"""
import base64
import csv
import hmac
import html
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Use Indian time for all dates saved and shown (change TZ env var to override)
os.environ.setdefault("TZ", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()

BASE = Path(__file__).resolve().parent
PAGE = BASE / "dropzyy-launch.html"
DB_PATH = os.environ.get("DB_PATH", str(BASE / "dropzyy.db"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"

VILLAGES = {"Savda", "Faizpur"}
VEHICLES = {"Bike", "Scooter", "Cycle"}
YES_NO = {"Yes", "No"}
MAX_BODY = 10_000
RATE_LIMIT = 10          # submissions allowed ...
RATE_WINDOW = 3600       # ... per IP per hour

_hits = {}
_hits_lock = threading.Lock()


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS restaurants (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                restaurant_name TEXT NOT NULL,
                owner_name      TEXT NOT NULL,
                phone           TEXT NOT NULL UNIQUE,
                village         TEXT NOT NULL,
                address         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS riders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                name        TEXT NOT NULL,
                phone       TEXT NOT NULL UNIQUE,
                village     TEXT NOT NULL,
                vehicle     TEXT NOT NULL,
                has_license TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS visitors (
                id         TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                last_seen  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                views      INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS visit_days (
                day        TEXT NOT NULL,
                visitor_id TEXT NOT NULL,
                PRIMARY KEY (day, visitor_id)
            );
            """
        )


# -------------------------------------------------------------- validation
class Invalid(Exception):
    pass


def text(data, key, label, lo=2, hi=100):
    v = data.get(key, "")
    if not isinstance(v, str):
        raise Invalid(f"Enter a valid {label}.")
    v = re.sub(r"\s+", " ", v).strip()
    if any(ord(c) < 32 for c in v):
        raise Invalid(f"Enter a valid {label}.")
    if len(v) < lo:
        raise Invalid(f"Enter your {label}.")
    if len(v) > hi:
        raise Invalid(f"{label.capitalize()} is too long (max {hi} characters).")
    return v


def choice(data, key, label, allowed):
    v = data.get(key, "")
    if v not in allowed:
        raise Invalid(f"Choose a {label}.")
    return v


def phone(data):
    digits = re.sub(r"\D", "", str(data.get("phone", "")))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise Invalid("Enter a valid 10-digit mobile number.")
    return digits


def rate_ok(ip):
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_LIMIT:
            _hits[ip] = recent
            return False
        recent.append(now)
        _hits[ip] = recent
        return True


def save_restaurant(d):
    row = (
        text(d, "restaurant_name", "restaurant name"),
        text(d, "owner_name", "owner name"),
        phone(d),
        choice(d, "village", "village", VILLAGES),
        text(d, "address", "area or address", 3, 200),
    )
    with db() as con:
        con.execute(
            "INSERT INTO restaurants (restaurant_name, owner_name, phone, village, address) "
            "VALUES (?,?,?,?,?)",
            row,
        )


def save_rider(d):
    row = (
        text(d, "name", "name"),
        phone(d),
        choice(d, "village", "village", VILLAGES),
        choice(d, "vehicle", "vehicle", VEHICLES),
        choice(d, "has_license", "licence option", YES_NO),
    )
    with db() as con:
        con.execute(
            "INSERT INTO riders (name, phone, village, vehicle, has_license) VALUES (?,?,?,?,?)",
            row,
        )


# ----------------------------------------------------------- visitor count
BOT_WORDS = ("bot", "crawl", "spider", "slurp", "render/", "uptime", "monitor",
             "curl", "wget", "python-requests", "headless", "preview")
VID_RE = re.compile(r"[0-9a-f]{32}")


def is_bot(ua):
    ua = (ua or "").lower()
    return not ua or any(w in ua for w in BOT_WORDS)


def record_visit(vid):
    with db() as con:
        con.execute(
            "INSERT INTO visitors (id) VALUES (?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen=datetime('now','localtime'), views=views+1",
            (vid,),
        )
        con.execute(
            "INSERT OR IGNORE INTO visit_days (day, visitor_id) VALUES (date('now','localtime'), ?)",
            (vid,),
        )


def visit_stats():
    with db() as con:
        one = lambda q: con.execute(q).fetchone()[0]
        total = one("SELECT COUNT(*) FROM visitors")
        views = one("SELECT COALESCE(SUM(views),0) FROM visitors")
        today = one("SELECT COUNT(*) FROM visit_days WHERE day=date('now','localtime')")
        new_today = one("SELECT COUNT(*) FROM visitors WHERE date(first_seen)=date('now','localtime')")
        days = con.execute(
            "SELECT day, COUNT(*) AS n FROM visit_days GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()
    return {"total": total, "views": views, "today": today, "new_today": new_today, "days": days}


# ------------------------------------------------------------------- admin
RESTAURANT_COLS = [
    ("id", "ID"), ("created_at", "Date"), ("restaurant_name", "Restaurant"),
    ("owner_name", "Owner"), ("phone", "Phone"), ("village", "Village"), ("address", "Address"),
]
RIDER_COLS = [
    ("id", "ID"), ("created_at", "Date"), ("name", "Name"), ("phone", "Phone"),
    ("village", "Village"), ("vehicle", "Vehicle"), ("has_license", "Licence"),
]


def table_html(title, rows, cols, csv_name):
    head = "".join(f"<th>{html.escape(l)}</th>" for _, l in cols)
    body = ""
    for r in rows:
        tds = ""
        for k, _ in cols:
            v = html.escape(str(r[k]))
            if k == "phone":
                v = f'<a href="tel:{v}">{v}</a>'
            tds += f"<td>{v}</td>"
        body += f"<tr>{tds}</tr>"
    if not body:
        body = f'<tr><td colspan="{len(cols)}">No sign-ups yet.</td></tr>'
    return (
        f'<h2>{title} ({len(rows)}) <a class="dl" href="/admin/export/{csv_name}.csv">Download CSV</a></h2>'
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def admin_page():
    with db() as con:
        rest = con.execute("SELECT * FROM restaurants ORDER BY id DESC").fetchall()
        rid = con.execute("SELECT * FROM riders ORDER BY id DESC").fetchall()
    st = visit_stats()
    day_rows = "".join(
        f"<tr><td>{html.escape(r['day'])}</td><td>{r['n']}</td></tr>" for r in st["days"]
    ) or '<tr><td colspan="2">No visitors yet.</td></tr>'
    cards = "".join(
        f'<div class="card"><b>{v}</b><span>{html.escape(l)}</span></div>'
        for v, l in [
            (st["total"], "Unique visitors (all time)"),
            (st["today"], "Visitors today"),
            (st["new_today"], "New visitors today"),
            (st["views"], "Total page views"),
            (len(rest), "Restaurants signed up"),
            (len(rid), "Riders signed up"),
        ]
    )
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dropzyy sign-ups</title>
<style>
body{{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#FFF8F2;color:#1B1B30;margin:0;padding:24px}}
h1{{margin:0 0 6px}} h2{{margin:32px 0 10px;font-size:1.2rem}}
.dl{{font-size:.85rem;font-weight:600;color:#F2493A;margin-left:10px}}
.scroll{{overflow-x:auto;background:#fff;border-radius:14px;border:1px solid #F0E2D8}}
table{{border-collapse:collapse;width:100%;font-size:.92rem}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #F0E2D8;white-space:nowrap}}
th{{background:#1B1B30;color:#fff}} td a{{color:#F2493A}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0 8px}}
.card{{background:#fff;border:1px solid #F0E2D8;border-radius:16px;padding:16px 18px}}
.card b{{display:block;font-size:2rem;line-height:1.1;color:#F2493A}}
.card span{{font-size:.85rem;color:#5E5E73}}
.small{{max-width:320px}} .note{{font-size:.85rem;color:#5E5E73;margin-top:8px}}
</style></head><body>
<h1>Dropzyy dashboard</h1>
<div class="cards">{cards}</div>
<h2>Unique visitors, last 7 days</h2>
<div class="scroll small"><table><thead><tr><th>Date</th><th>Unique visitors</th></tr></thead><tbody>{day_rows}</tbody></table></div>
<p class="note">Visitors are counted by browser, so one person using two devices counts twice. Bots are not counted.</p>
{table_html("Restaurants", rest, RESTAURANT_COLS, "restaurants")}
{table_html("Riders", rid, RIDER_COLS, "riders")}
</body></html>"""


def csv_safe(v):
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def export_csv(table):
    cols = RESTAURANT_COLS if table == "restaurants" else RIDER_COLS
    with db() as con:
        rows = con.execute(f"SELECT * FROM {table} ORDER BY id DESC").fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([l for _, l in cols])
    for r in rows:
        w.writerow([csv_safe(r[k]) for k, _ in cols])
    return out.getvalue().encode("utf-8-sig")


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "Dropzyy"

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json", {"Cache-Control": "no-store"})

    def _ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _authed(self):
        if not ADMIN_PASSWORD:
            self._send(
                403,
                "Admin is off. Start the server with ADMIN_PASSWORD set to a strong password.",
                "text/plain; charset=utf-8",
            )
            return False
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok_user = hmac.compare_digest(user.encode(), ADMIN_USER.encode())
                ok_pw = hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())
                if ok_user and ok_pw:
                    return True
            except Exception:
                pass
        self._send(
            401, "Login required", "text/plain; charset=utf-8",
            {"WWW-Authenticate": 'Basic realm="Dropzyy admin"'},
        )
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/dropzyy-launch.html"):
            extra = {"Cache-Control": "no-cache"}
            if not is_bot(self.headers.get("User-Agent")):
                vid = ""
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "dz_vid" and VID_RE.fullmatch(v):
                        vid = v
                if not vid:
                    vid = secrets.token_hex(16)
                    secure = "; Secure" if TRUST_PROXY else ""
                    extra["Set-Cookie"] = (
                        f"dz_vid={vid}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax{secure}"
                    )
                try:
                    record_visit(vid)
                except Exception:
                    pass
            self._send(200, PAGE.read_bytes(), extra=extra)
        elif path == "/admin":
            if self._authed():
                self._send(200, admin_page(), extra={"Cache-Control": "no-store"})
        elif path in ("/admin/export/restaurants.csv", "/admin/export/riders.csv"):
            if self._authed():
                name = path.rsplit("/", 1)[1][:-4]
                self._send(
                    200, export_csv(name), "text/csv; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="dropzyy-{name}.csv"',
                     "Cache-Control": "no-store"},
                )
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/restaurants", "/api/riders"):
            return self._json(404, {"ok": False, "error": "Not found."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._json(400, {"ok": False, "error": "Invalid request."})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._json(400, {"ok": False, "error": "Invalid request."})

        if data.get("website"):  # hidden field only bots fill in
            return self._json(200, {"ok": True, "message": "Thank you! We will contact you soon."})
        if not rate_ok(self._ip()):
            return self._json(429, {"ok": False, "error": "Too many attempts. Please try again later."})

        try:
            if path == "/api/restaurants":
                save_restaurant(data)
                msg = "Thank you! Your restaurant is registered. We will call you soon."
            else:
                save_rider(data)
                msg = "Thank you! You are registered as a rider. We will call you soon."
        except Invalid as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except sqlite3.IntegrityError:
            return self._json(409, {"ok": False, "error": "This mobile number is already registered."})
        except Exception:
            return self._json(500, {"ok": False, "error": "Server error. Please try again."})
        self._json(200, {"ok": True, "message": msg})


if __name__ == "__main__":
    init_db()
    if not ADMIN_PASSWORD:
        print("Note: ADMIN_PASSWORD is not set, so /admin is disabled.")
    print(f"Dropzyy running at http://localhost:{PORT}  (database: {DB_PATH})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
