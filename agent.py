import os, uuid, datetime, functools
from google.genai import types
import paypal_client as pp
import db
from ai import generate
import logging
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

REFUND_LIMIT = float(os.getenv("BAHI_REFUND_LIMIT", "500"))
PENDING = {}   # actions waiting for the user's confirmation

def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            return {"error": str(e)[:300]}
    return wrapper

def _row(invoice_id):
    key = str(invoice_id).strip().lstrip("#")
    for r in db.list_invoices():
        if r["id"] == key or r["number"] == key or r["number"].lstrip("0") == key.lstrip("0"):
            return r
    return None

def _propose(kind, row, summary, **params):
    # Merge identical proposals so the user never sees duplicate cards
    for a in PENDING.values():
        if a["kind"] == kind and a["invoice_id"] == row["id"] and a["params"] == params:
            return {"status": "AWAITING_USER_CONFIRMATION", "action_id": a["id"], "summary": a["summary"]}
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"id": aid, "kind": kind, "invoice_id": row["id"], "number": row["number"],
                    "client": row["client_name"], "summary": summary, "params": params, "created": db.now()}
    return {"status": "AWAITING_USER_CONFIRMATION", "action_id": aid, "summary": summary}

# ---------------- read-only tools (run immediately) ----------------
@_safe
def list_invoices(status: str = "") -> list[dict]:
    """List the user's invoices from the ledger, newest first. Optional status filter:
    SENT, PAID, MARKED_AS_PAID, PARTIALLY_PAID, CANCELLED, REFUNDED, PARTIALLY_REFUNDED."""
    keys = ("id", "number", "client_name", "client_email", "amount", "currency", "status", "created_at")
    return [{k: r[k] for k in keys} for r in db.list_invoices()
            if not status or r["status"] == status.upper()]

@_safe
def get_invoice_details(invoice_id: str) -> dict:
    """Fetch LIVE details of one invoice from PayPal: status, due date, items, amount paid,
    payment transactions and refunds. invoice_id can be the INV2-... id or the invoice number."""
    r = _row(invoice_id)
    inv = pp.get_invoice(r["id"] if r else invoice_id)
    db.upsert_invoice(inv)
    d, pay = inv.get("detail", {}), inv.get("payments", {})
    return {"id": inv["id"], "number": d.get("invoice_number"), "status": inv.get("status"),
            "invoice_date": d.get("invoice_date"), "due_date": d.get("payment_term", {}).get("due_date"),
            "total": inv.get("amount"), "paid_amount": pay.get("paid_amount"), "due_amount": inv.get("due_amount"),
            "items": [{"name": i.get("name"), "quantity": i.get("quantity"),
                       "unit_amount": i.get("unit_amount", {}).get("value")} for i in inv.get("items", [])],
            "payments": [{"method": t.get("method"), "amount": t.get("amount"), "date": t.get("payment_date")}
                         for t in pay.get("transactions", [])],
            "refunded_amount": inv.get("refunds", {}).get("refund_amount")}

# ---------------- action tools (only PROPOSE; user must confirm) ----------------
@_safe
def propose_payment_reminder(invoice_id: str, message: str) -> dict:
    """Prepare a payment reminder email for an UNPAID invoice. Write `message` yourself:
    2-3 warm, professional English sentences addressed to the client. Requires user confirmation."""
    r = _row(invoice_id)
    if not r:
        return {"error": "Invoice not found"}
    if r["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
        return {"error": f"Invoice #{r['number']} is {r['status']}; reminders are only for unpaid invoices."}
    return _propose("remind", r, f"Send a payment reminder to {r['client_name']} for invoice #{r['number']} "
                                 f"({r['amount']:.2f} {r['currency']})", message=message)

@_safe
def propose_mark_as_paid(invoice_id: str, method: str = "BANK_TRANSFER", note: str = "") -> dict:
    """Prepare recording a payment the user received OUTSIDE PayPal.
    method: BANK_TRANSFER, CASH, CHECK, WIRE_TRANSFER or OTHER. Requires user confirmation."""
    r = _row(invoice_id)
    if not r:
        return {"error": "Invoice not found"}
    if r["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
        return {"error": f"Invoice #{r['number']} is already {r['status']}."}
    return _propose("mark_paid", r, f"Mark invoice #{r['number']} from {r['client_name']} as paid "
                                    f"({method.replace('_', ' ').lower()})", method=method.upper(), note=note)

@_safe
def propose_refund(invoice_id: str, amount: float, reason: str) -> dict:
    """Prepare a full or partial refund to the client of a PAID invoice. `amount` is in the
    invoice currency. Requires user confirmation; refunds above the user's limit are flagged."""
    r = _row(invoice_id)
    if not r:
        return {"error": "Invoice not found"}
    if r["status"] not in ("PAID", "MARKED_AS_PAID", "PARTIALLY_REFUNDED"):
        return {"error": f"Invoice #{r['number']} is {r['status']}; only paid invoices can be refunded."}
    amount = round(float(amount), 2)
    if amount <= 0 or amount > r["amount"]:
        return {"error": f"Refund must be between 0 and {r['amount']:.2f} {r['currency']}."}
    high = amount > REFUND_LIMIT
    summary = f"Refund {amount:.2f} {r['currency']} to {r['client_name']} on invoice #{r['number']}"
    if high:
        summary += f" (above your {REFUND_LIMIT:.0f} {r['currency']} safety limit, please double-check)"
    return _propose("refund", r, summary, amount=amount, reason=reason, high_value=high)

@_safe
def propose_cancel_invoice(invoice_id: str, reason: str) -> dict:
    """Prepare cancelling an UNPAID invoice; the client is notified with `reason`. Requires user confirmation."""
    r = _row(invoice_id)
    if not r:
        return {"error": "Invoice not found"}
    if r["status"] not in ("SENT", "UNPAID", "DRAFT"):
        return {"error": f"Invoice #{r['number']} is {r['status']}; only unpaid invoices can be cancelled."}
    return _propose("cancel", r, f"Cancel invoice #{r['number']} for {r['client_name']} "
                                 f"({r['amount']:.2f} {r['currency']})", reason=reason)

TOOLS = [list_invoices, get_invoice_details, propose_payment_reminder,
         propose_mark_as_paid, propose_refund, propose_cancel_invoice]

SYSTEM = """You are Bahi, the AI back-office agent for an Indian freelancer who is paid by overseas clients through PayPal.
Today is {today}.
Rules:
- Reply in the same language and style the user writes in (Hinglish -> Hinglish, Hindi -> Hindi, English -> English). Be brief and concrete.
- Before acting, use list_invoices to find invoices by client name or number. If several match and it matters, ask which one.
- Use get_invoice_details for live status, due dates, amounts paid and refunds.
- You can NEVER change anything yourself. For reminders, marking paid, refunds or cancellations, call the matching propose_* tool.
  The user then sees a confirmation card. Say the action is waiting for their confirmation; never claim it is done.
- Write plain text only, no markdown, no asterisks.
- Refer to invoices only by their number like #0001, never by the internal INV2 id.
- Never invent amounts, emails or invoice numbers. Use the invoice currency.
- An invoice is overdue if it is unpaid and its due_date is before today."""

def chat(message, history=None):
    before = set(PENDING)
    contents = []
    for h in (history or [])[-10:]:
        role = "user" if h.get("role") == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=str(h.get("text", "")))]))
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM.format(today=datetime.date.today().strftime("%A, %d %B %Y")),
        tools=TOOLS, temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=8))
    resp = generate(contents, cfg)
    new = [PENDING[k] for k in PENDING if k not in before]
    return {"reply": (resp.text or "").strip(),
            "actions": [{"id": a["id"], "kind": a["kind"], "number": a["number"], "client": a["client"],
                         "summary": a["summary"], "high_value": a["params"].get("high_value", False)} for a in new]}

def execute(action_id):
    a = PENDING.pop(action_id, None)
    if not a:
        raise KeyError("This action has expired or was already handled.")
    iid, p, today = a["invoice_id"], a["params"], datetime.date.today().isoformat()
    if a["kind"] == "remind":
        pp.api("POST", f"/v2/invoicing/invoices/{iid}/remind",
               json={"subject": f"Friendly reminder: invoice #{a['number']}", "note": p["message"],
                     "send_to_invoicer": False, "send_to_recipient": True})
        msg = f"Reminder sent to {a['client']} for invoice #{a['number']}"
    elif a["kind"] == "mark_paid":
        inv = pp.get_invoice(iid)
        due = inv.get("due_amount") or inv["amount"]
        pp.api("POST", f"/v2/invoicing/invoices/{iid}/payments",
               json={"method": p["method"], "payment_date": today, "note": p.get("note") or "Recorded by Bahi",
                     "amount": {"currency_code": due["currency_code"], "value": due["value"]}})
        msg = f"Invoice #{a['number']} from {a['client']} marked as paid"
    elif a["kind"] == "cancel":
        pp.api("POST", f"/v2/invoicing/invoices/{iid}/cancel",
               json={"subject": f"Invoice #{a['number']} cancelled", "note": p["reason"],
                     "send_to_invoicer": False, "send_to_recipient": True})
        msg = f"Invoice #{a['number']} for {a['client']} cancelled"
    elif a["kind"] == "refund":
        inv = pp.get_invoice(iid)
        cur = inv["amount"]["currency_code"]
        txs = inv.get("payments", {}).get("transactions", [])
        paypal_tx = next((t for t in txs if t.get("method") == "PAYPAL" and t.get("payment_id")), None)
        if paypal_tx:   # real PayPal payment -> real money goes back to the client
            pp.api("POST", f"/v2/payments/captures/{paypal_tx['payment_id']}/refund",
                   json={"amount": {"value": f"{p['amount']:.2f}", "currency_code": cur},
                         "note_to_payer": p["reason"][:255]})
        else:           # paid outside PayPal -> record the refund on the invoice
            pp.api("POST", f"/v2/invoicing/invoices/{iid}/refunds",
                   json={"method": "BANK_TRANSFER", "refund_date": today,
                         "amount": {"currency_code": cur, "value": f"{p['amount']:.2f}"}})
        msg = f"Refunded {p['amount']:.2f} {cur} to {a['client']} on invoice #{a['number']}"
    else:
        raise KeyError("Unknown action")
    inv = pp.get_invoice(iid)
    db.upsert_invoice(inv)
    db.log("agent_action", msg, iid)
    return {"ok": True, "message": msg, "status": inv.get("status")}

if __name__ == "__main__":
    for q in ["Sarah ka payment aaya kya? Kitna mila?",
              "Sarah ko 50 dollar refund kar do, ek logo revision cancel ho gaya tha"]:
        print("\nYOU  :", q)
        out = chat(q)
        print("BAHI :", out["reply"])
        for act in out["actions"]:
            print("  CONFIRM CARD ->", act["summary"])
