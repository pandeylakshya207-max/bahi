"""Creates a clearly-labelled DEMO dispute (same shape as PayPal's dispute object) on Sarah's invoice."""
import json, datetime as dt
import db, disputes

rows = [r for r in db.list_invoices() if r["number"] == "0001"] or \
       [r for r in db.list_invoices() if r["status"] in ("PAID", "PARTIALLY_REFUNDED")]
if not rows:
    raise SystemExit("No paid invoice found to attach a demo dispute to.")
r = rows[0]
now = dt.datetime.now(dt.timezone.utc)
dispute = {
    "dispute_id": "PP-D-DEMO-0001", "demo": True, "invoice_id": r["id"],
    "create_time": (now - dt.timedelta(days=1)).isoformat(timespec="seconds"),
    "update_time": now.isoformat(timespec="seconds"),
    "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_life_cycle_stage": "INQUIRY",
    "dispute_amount": {"currency_code": r["currency"], "value": "400.00"},
    "seller_response_due_date": (now + dt.timedelta(days=9)).isoformat(timespec="seconds"),
    "disputed_transactions": [{"buyer": {"name": r["client_name"]}, "invoice_number": r["number"],
                               "gross_amount": {"currency_code": r["currency"], "value": f"{r['amount']:.2f}"}}],
    "messages": [{"posted_by": "BUYER", "time_posted": (now - dt.timedelta(days=1)).isoformat(timespec="seconds"),
                  "content": "Only 2 of the 3 logo designs were delivered and they don't match the brief we agreed. "
                             "I want the remaining $400 refunded."}]}
items = [d for d in disputes._demo() if d["dispute_id"] != dispute["dispute_id"]] + [dispute]
disputes._save_demo(items)
db.log("dispute_opened", f"{r['client_name']} opened a dispute: Not as described ({r['currency']} 400.00) [demo]", r["id"])
print("Demo dispute created on invoice #" + r["number"], "for", r["client_name"])
