import sqlite3, datetime, os
from contextlib import closing
DB = os.getenv("BAHI_DB", "bahi.db")

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.datetime.now().isoformat(timespec="seconds")

def init():
    with closing(conn()) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS invoices(
            id TEXT PRIMARY KEY, number TEXT, client_name TEXT, client_email TEXT,
            amount REAL, currency TEXT, status TEXT, pay_link TEXT,
            source TEXT, language TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT, invoice_id TEXT);
        """)

def upsert_invoice(inv, source=None, language=None):
    d, amt = inv.get("detail", {}), inv.get("amount", {})
    rec = (inv.get("primary_recipients") or [{}])[0].get("billing_info", {})
    name = " ".join(filter(None, [rec.get("name", {}).get("given_name"), rec.get("name", {}).get("surname")]))
    with closing(conn()) as c, c:
        c.execute("""
        INSERT INTO invoices(id, number, client_name, client_email, amount, currency, status, pay_link,
                             source, language, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status, amount=excluded.amount,
                                      pay_link=excluded.pay_link, updated_at=excluded.updated_at""",
        (inv["id"], d.get("invoice_number"), name, rec.get("email_address"),
         float(amt.get("value") or 0), amt.get("currency_code"), inv.get("status"),
         d.get("metadata", {}).get("recipient_view_url"), source, language, now(), now()))

def log(kind, message, invoice_id=None):
    with closing(conn()) as c, c:
        c.execute("INSERT INTO events(ts, kind, message, invoice_id) VALUES(?,?,?,?)",
                  (now(), kind, message, invoice_id))

def list_invoices():
    with closing(conn()) as c, c:
        return [dict(r) for r in c.execute("SELECT * FROM invoices ORDER BY created_at DESC")]

def list_events(limit=50):
    with closing(conn()) as c, c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
