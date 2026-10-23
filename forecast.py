"""Cash-flow forecast: when will unpaid invoices likely be paid, and how much in INR."""
import datetime as dt, json, math, time
import httpx
from google.genai import types
import paypal_client as pp
import db
from ai import generate

OPEN = ("SENT", "UNPAID", "PARTIALLY_PAID")
PAID = ("PAID", "MARKED_AS_PAID", "PARTIALLY_REFUNDED", "REFUNDED")
_fx = {"t": 0, "rates": {}, "src": ""}
_ins = {"key": None, "text": ""}

def inr_rates(currencies):
    need = {c for c in currencies if c and c != "INR"}
    if time.time() - _fx["t"] < 3600 and need <= set(_fx["rates"]):
        return _fx["rates"], _fx["src"]
    rates, src = {"INR": 1.0}, "live ECB reference rates (frankfurter)"
    for c in need:
        rate = None
        for url, params in (("https://api.frankfurter.dev/v1/latest", {"base": c, "symbols": "INR"}),
                            ("https://api.frankfurter.app/latest", {"from": c, "to": "INR"})):
            try:
                r = httpx.get(url, params=params, timeout=10, follow_redirects=True)
                rate = float(r.json()["rates"]["INR"])
                break
            except Exception:
                continue
        rates[c] = rate
        if rate is None:
            src = "reference rates (some currencies unavailable)"
    _fx.update(t=time.time(), rates=rates, src=src)
    return rates, src

def client_delays():
    """Average days late per client, from invoices they already paid (negative = early)."""
    out = {}
    for r in db.list_invoices():
        if r["status"] not in PAID or not r.get("due_date"):
            continue
        try:
            txs = pp.get_invoice(r["id"]).get("payments", {}).get("transactions", [])
        except pp.PayPalError:
            continue
        if txs and txs[0].get("payment_date"):
            paid = dt.date.fromisoformat(txs[0]["payment_date"][:10])
            out.setdefault(r["client_email"], []).append((paid - dt.date.fromisoformat(r["due_date"])).days)
    return {k: sum(v) / len(v) for k, v in out.items()}

def forecast(days=30):
    today = dt.date.today()
    rows = [r for r in db.list_invoices() if r["status"] in OPEN]
    rates, src = inr_rates({r["currency"] for r in rows})
    delays = client_delays()
    reminded = {e['invoice_id']: e['ts'][:10] for e in reversed(db.list_events(500))
                if e['kind'] == 'agent_action' and e['message'].startswith('Reminder sent')}
    items = []
    for r in rows:
        due = dt.date.fromisoformat(r["due_date"]) if r.get("due_date") else today + dt.timedelta(days=7)
        late = (today - due).days
        hist = delays.get(r["client_email"])
        if late > 0:
            expected = today + dt.timedelta(days=max(3, min(14, late // 2)))
            prob = max(0.35, 0.9 - 0.03 * late)
            basis = f"{late} days overdue"
            if r["id"] in reminded:
                basis += f", reminder already sent on {reminded[r['id']]}"
        else:
            shift = round(hist) if hist is not None else 2
            expected = due + dt.timedelta(days=max(0, shift))
            prob = 0.9 if hist is None or hist <= 3 else 0.75
            basis = ("usually pays on time" if hist is not None and hist <= 3 else
                     f"usually pays about {round(hist)} days late" if hist is not None else
                     "new client, assuming a couple of days after the due date")
        rate = rates.get(r["currency"])
        inr = round(r["amount"] * rate) if rate else None
        items.append({"number": r["number"], "client": r["client_name"], "amount": r["amount"],
                      "currency": r["currency"], "amount_inr": inr, "due_date": due.isoformat(),
                      "expected_date": expected.isoformat(), "expected_label": expected.strftime("around %d %b"),
                      "probability": round(prob, 2), "basis": basis, "in_window": expected <= today + dt.timedelta(days=days)})
    weeks = []
    for w in range(math.ceil(days / 7)):
        a, b = today + dt.timedelta(days=7 * w), today + dt.timedelta(days=7 * w + 6)
        inside = [i for i in items if a <= dt.date.fromisoformat(i["expected_date"]) <= b and i["amount_inr"]]
        weeks.append({"label": a.strftime("%d %b"),
                      "expected_inr": round(sum(i["amount_inr"] * i["probability"] for i in inside)),
                      "best_inr": round(sum(i["amount_inr"] for i in inside))})
    win = [i for i in items if i["in_window"] and i["amount_inr"]]
    return {"days": days, "today": today.isoformat(), "rates_source": src,
            "rates_to_inr": {k: v for k, v in rates.items() if k != "INR"},
            "expected_inr": round(sum(i["amount_inr"] * i["probability"] for i in win)),
            "best_case_inr": round(sum(i["amount_inr"] for i in win)),
            "at_risk": [i for i in items if i["probability"] < 0.6],
            "weeks": weeks, "items": sorted(items, key=lambda i: i["expected_date"])}

def insight():
    f = forecast(30)
    key = json.dumps(f["items"], sort_keys=True, default=str)
    if _ins["key"] == key and _ins["text"]:
        return {"insight": _ins["text"]}
    prompt = ("You advise an Indian freelancer paid by overseas clients through PayPal. Using ONLY this forecast "
              "(INR amounts are approximate), write 2-3 short plain-text sentences: how much is likely to arrive in "
              "the next 30 days, which client is the biggest risk and one concrete NEXT action. Only mention reminders if an item's basis explicitly says a reminder was sent; never guess. If one was sent, do not suggest another reminder; suggest a sensible next step instead (for example a short personal follow-up message, or offering a payment plan). No markdown.\n"
              + json.dumps(f, default=str))
    resp = generate(prompt, types.GenerateContentConfig(temperature=0.3))
    _ins.update(key=key, text=(resp.text or "").strip())
    return {"insight": _ins["text"]}

if __name__ == "__main__":
    f = forecast(30)
    print("Rates to INR:", f["rates_to_inr"], "|", f["rates_source"])
    for i in f["items"]:
        print(f"  #{i['number']} {i['client']:14} {i['amount']:>8.2f} {i['currency']}  ~Rs {i['amount_inr']}  "
              f"{i['expected_label']}  {int(i['probability']*100)}%  ({i['basis']})")
    print("Expected next 30 days: Rs", f["expected_inr"], "| best case: Rs", f["best_case_inr"])
    print("Insight:", insight()["insight"])
