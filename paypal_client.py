import os, time, datetime, httpx
from dotenv import load_dotenv
load_dotenv()

BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
_token = {"value": None, "exp": 0}

class PayPalError(Exception):
    pass

def key_shapes():
    out = {}
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_BASE"):
        v = os.getenv(k) or ""
        out[k] = {"len": len(v), "starts": v[:2], "whitespace": v != v.strip() or " " in v}
    return out

def fingerprint():
    import hashlib
    out = {}
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"):
        v = os.getenv(k) or ""
        out[k] = {"len": len(v), "hash": hashlib.sha256(v.encode()).hexdigest()[:8]}
    out["PAYPAL_BASE"] = os.getenv("PAYPAL_BASE")
    try:
        _get_token(); out["login"] = "OK"
    except Exception as e:
        out["login"] = str(e)[:80]
    return out

def _get_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = httpx.post(f"{BASE}/v1/oauth2/token",
                   auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                   data={"grant_type": "client_credentials"}, timeout=30)
    if r.status_code >= 400:
        raise PayPalError(f"PayPal login failed ({r.status_code}). Check PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.")
    d = r.json()
    _token.update(value=d["access_token"], exp=time.time() + d["expires_in"])
    return _token["value"]

def api(method, path, json=None, params=None):
    r = httpx.request(method, f"{BASE}{path}", json=json, params=params, timeout=30,
                      headers={"Authorization": f"Bearer {_get_token()}",
                               "Content-Type": "application/json",
                               "Prefer": "return=representation"})
    if r.status_code >= 400:
        raise PayPalError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
    return r.json() if r.content else {}

# ---------------- Invoicing ----------------
def next_invoice_number():
    return api("POST", "/v2/invoicing/generate-next-invoice-number")["invoice_number"]

def create_invoice(client_name, client_email, items, currency="USD", due_days=7, note=""):
    """items: [{"name": str, "quantity": number, "unit_amount": number}]"""
    first, _, last = client_name.strip().partition(" ")
    due = (datetime.date.today() + datetime.timedelta(days=int(due_days))).isoformat()
    body = {
        "detail": {
            "invoice_number": next_invoice_number(),
            "currency_code": currency,
            "note": note or "Thank you for your business!",
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due},
        },
        "primary_recipients": [{"billing_info": {
            "name": {"given_name": first or client_name, "surname": last or ""},
            "email_address": client_email}}],
        "items": [{
            "name": it["name"],
            "quantity": str(it.get("quantity", 1)),
            "unit_amount": {"currency_code": currency, "value": f'{float(it["unit_amount"]):.2f}'},
            "unit_of_measure": "QUANTITY"} for it in items],
    }
    body["invoicer"] = {"business_name": os.getenv("BAHI_BUSINESS_NAME", "Aarav Designs")}
    res = api("POST", "/v2/invoicing/invoices", json=body)
    inv_id = res.get("id") or res["href"].rstrip("/").split("/")[-1]
    return get_invoice(inv_id)

def send_invoice(invoice_id):
    api("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
        json={"send_to_invoicer": False, "send_to_recipient": True})
    return get_invoice(invoice_id)

def get_invoice(invoice_id):
    return api("GET", f"/v2/invoicing/invoices/{invoice_id}")

def list_invoices(page_size=20):
    return api("GET", "/v2/invoicing/invoices",
               params={"page": 1, "page_size": page_size, "total_required": "true"}).get("items", [])

def summarize(inv):
    d, amt = inv.get("detail", {}), inv.get("amount", {})
    return {"id": inv.get("id"), "number": d.get("invoice_number"), "status": inv.get("status"),
            "amount": f'{amt.get("value")} {amt.get("currency_code")}',
            "pay_link": d.get("metadata", {}).get("recipient_view_url")}

if __name__ == "__main__":
    inv = create_invoice("Sarah Miller", "sarah.client@example.com",
                         [{"name": "Logo design", "quantity": 3, "unit_amount": 150}], due_days=7)
    print("Created:", summarize(inv))
    inv = send_invoice(inv["id"])
    print("Sent:   ", summarize(inv))

# ---------------- Webhooks ----------------
WEBHOOK_EVENTS = ["INVOICING.INVOICE.PAID", "INVOICING.INVOICE.CANCELLED", "INVOICING.INVOICE.REFUNDED",
                  "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED",
                  "CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"]

def verify_webhook(headers, event, webhook_id):
    """Ask PayPal to confirm this webhook really came from PayPal (prevents fake 'paid' events)."""
    body = {"auth_algo": headers.get("paypal-auth-algo"), "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id, "webhook_event": event}
    if not all(body.values()):
        return False
    res = api("POST", "/v1/notifications/verify-webhook-signature", json=body)
    return res.get("verification_status") == "SUCCESS"

def register_webhook(url, events=WEBHOOK_EVENTS):
    """Replace this app's webhooks with one pointing at url (tunnel URLs change on restart)."""
    for wh in api("GET", "/v1/notifications/webhooks").get("webhooks", []):
        api("DELETE", f"/v1/notifications/webhooks/{wh['id']}")
    return api("POST", "/v1/notifications/webhooks",
               json={"url": url, "event_types": [{"name": e} for e in events]})
