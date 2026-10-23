"""Dispute Defender: gathers evidence from the ledger, has the AI assess the case honestly,
and drafts responses. Works with real PayPal disputes and with a clearly-labelled demo dispute."""
import json, pathlib, datetime
from typing import Literal
import httpx
from pydantic import BaseModel, Field
from google.genai import types
import paypal_client as pp
import db
from ai import generate

DEMO_FILE = pathlib.Path("demo_disputes.json")
REASONS = {"MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Not as described",
           "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Not received", "UNAUTHORISED": "Unauthorised payment",
           "CREDIT_NOT_PROCESSED": "Refund not processed", "DUPLICATE_TRANSACTION": "Duplicate payment",
           "INCORRECT_AMOUNT": "Incorrect amount", "PAYMENT_BY_OTHER_MEANS": "Paid by other means",
           "CANCELED_RECURRING_BILLING": "Cancelled subscription", "OTHER": "Other"}

def _demo():
    return json.loads(DEMO_FILE.read_text(encoding="utf-8")) if DEMO_FILE.exists() else []

def _save_demo(items):
    DEMO_FILE.write_text(json.dumps(items, indent=1), encoding="utf-8")

def _brief(d, demo):
    return {"id": d["dispute_id"], "demo": demo, "reason": REASONS.get(d.get("reason"), d.get("reason")),
            "status": d.get("status"), "amount": d.get("dispute_amount"),
            "due": d.get("seller_response_due_date"), "created": d.get("create_time")}

def list_disputes():
    out = []
    try:
        for d in pp.api("GET", "/v1/customer/disputes", params={"page_size": 20}).get("items", []):
            out.append(_brief(d, demo=False))
    except pp.PayPalError as e:
        db.log("error", f"Could not list disputes: {str(e)[:200]}")
    return out + [_brief(d, demo=True) for d in _demo()]

def get_dispute(dispute_id):
    for d in _demo():
        if d["dispute_id"] == dispute_id:
            return d
    return pp.api("GET", f"/v1/customer/disputes/{dispute_id}")

def _find_invoice(d):
    if d.get("invoice_id"):
        return d["invoice_id"]
    t = (d.get("disputed_transactions") or [{}])[0]
    inv_no, sid = t.get("invoice_number"), t.get("seller_transaction_id")
    gross = t.get("gross_amount") or {}
    rows = db.list_invoices()
    for r in rows:
        if inv_no and r["number"] == inv_no:
            return r["id"]
    for r in rows:
        try:
            inv = pp.get_invoice(r["id"])
        except pp.PayPalError:
            continue
        if sid and any(x.get("payment_id") == sid for x in inv.get("payments", {}).get("transactions", [])):
            return r["id"]
    for r in rows:
        if gross.get("value") and r["currency"] == gross.get("currency_code") and abs(r["amount"] - float(gross["value"])) < 0.01:
            return r["id"]
    return None

def gather_evidence(d):
    iid = _find_invoice(d)
    ev = {"invoice_id": iid, "invoice": None, "timeline": []}
    if iid:
        inv = pp.get_invoice(iid)
        dd = inv.get("detail", {})
        ev["invoice"] = {
            "number": dd.get("invoice_number"), "invoice_date": dd.get("invoice_date"),
            "due_date": dd.get("payment_term", {}).get("due_date"), "note_to_client": dd.get("note"),
            "items": [{"name": i.get("name"), "quantity": i.get("quantity"),
                       "unit_amount": i.get("unit_amount", {}).get("value")} for i in inv.get("items", [])],
            "total": inv.get("amount"), "status": inv.get("status"),
            "payments": [{"method": t.get("method"), "amount": t.get("amount"), "date": t.get("payment_date")}
                         for t in inv.get("payments", {}).get("transactions", [])],
            "refunds": [{"amount": t.get("amount"), "date": t.get("refund_date")}
                        for t in inv.get("refunds", {}).get("transactions", [])]}
        ev["timeline"] = [{"time": e["ts"], "event": e["message"]}
                          for e in reversed(db.list_events(300)) if e["invoice_id"] == iid]
    return ev

class Assessment(BaseModel):
    buyer_claim: str = Field(description="One sentence: what the buyer is claiming")
    strength: Literal["STRONG", "MODERATE", "WEAK"] = Field(description="Strength of the SELLER's case")
    win_likelihood: float = Field(description="0 to 1, honest estimate that the seller wins if they fight")
    facts_for_seller: list[str] = Field(description="Facts from the evidence that support the seller, with dates/amounts")
    weaknesses: list[str] = Field(description="Facts or gaps that hurt the seller")
    missing_evidence: list[str] = Field(description="Proof the seller should attach to win (e.g. delivery files, chat approvals)")
    recommendation: Literal["FIGHT", "OFFER_PARTIAL_REFUND", "ACCEPT"]
    suggested_refund: float = Field(description="Refund to offer if OFFER_PARTIAL_REFUND, else 0")
    reasoning: str = Field(description="2-3 sentences explaining the recommendation")
    response_to_paypal: str = Field(description="Formal, factual seller response for PayPal, 120-200 words")
    message_to_buyer: str = Field(description="Short, warm message to the buyer aiming for an amicable resolution")

PROMPT = """You are Bahi's Dispute Defender, helping an Indian freelancer respond to a PayPal dispute from an overseas client.
Today is {today}.
Be honest: do not overstate the seller's chances. Use ONLY facts in the dispute, the evidence and the seller's notes.
Never invent delivery proof, files, emails or dates. NEVER say any document is attached, enclosed or provided: nothing is attached. Instead say the seller can provide it on request. If proof is missing, list it under missing_evidence.
Seller notes are the freelancer's own statements: you may use them, but note that PayPal will want proof.
Consider refunds already given, the amount in dispute versus the invoice total, and the buyer's exact words.
suggested_refund is the ADDITIONAL amount to offer now, on top of refunds already given; never exceed the disputed amount.
response_to_paypal must be formal and factual, cite invoice number, dates and amounts.

DISPUTE (from PayPal):
{dispute}

EVIDENCE GATHERED BY BAHI (from PayPal invoice + ledger):
{evidence}

SELLER'S NOTES:
{notes}"""

def assess(dispute_id, seller_notes=""):
    d = get_dispute(dispute_id)
    ev = gather_evidence(d)
    slim = {k: d.get(k) for k in ("dispute_id", "reason", "status", "dispute_amount", "create_time",
                                  "seller_response_due_date", "dispute_life_cycle_stage", "messages",
                                  "disputed_transactions")}
    resp = generate(PROMPT.format(today=datetime.date.today().isoformat(),
                                  dispute=json.dumps(slim, indent=1, default=str),
                                  evidence=json.dumps(ev, indent=1, default=str),
                                  notes=seller_notes or "(none provided)"),
                    types.GenerateContentConfig(response_mime_type="application/json",
                                                response_schema=Assessment, temperature=0.2))
    a = resp.parsed if isinstance(resp.parsed, Assessment) else Assessment.model_validate_json(resp.text)
    db.log("dispute_assessed", f"Bahi assessed dispute {dispute_id}: {a.strength} case, recommends {a.recommendation}",
           ev["invoice_id"])
    return {"dispute": _brief(d, bool(d.get("demo"))), "evidence": ev, "assessment": a.model_dump()}

def act(dispute_id, action, text, amount=0.0):
    if action not in ("respond", "offer", "accept"):
        raise ValueError("Unknown action")
    d = get_dispute(dispute_id)
    demo = bool(d.get("demo"))
    cur = (d.get("dispute_amount") or {}).get("currency_code", "USD")
    if demo:
        items = _demo()
        for x in items:
            if x["dispute_id"] == dispute_id:
                x["status"] = {"respond": "UNDER_REVIEW", "offer": "WAITING_FOR_BUYER_RESPONSE",
                               "accept": "RESOLVED"}[action]
        _save_demo(items)
    elif action == "respond":
        r = httpx.post(f"{pp.BASE}/v1/customer/disputes/{dispute_id}/provide-evidence",
                       headers={"Authorization": f"Bearer {pp._get_token()}"}, timeout=30,
                       files={"input": (None, json.dumps({"evidences": [{"evidence_type": "OTHER",
                                                                           "notes": text[:2000]}]}),
                                        "application/json")})
        if r.status_code >= 400:
            raise pp.PayPalError(f"provide-evidence -> {r.status_code}: {r.text[:400]}")
    elif action == "offer":
        pp.api("POST", f"/v1/customer/disputes/{dispute_id}/make-offer",
               json={"note": text[:2000], "offer_type": "REFUND",
                     "offer_amount": {"currency_code": cur, "value": f"{float(amount):.2f}"}})
    else:
        pp.api("POST", f"/v1/customer/disputes/{dispute_id}/accept-claim", json={"note": text[:2000]})
    label = {"respond": "Sent the evidence response", "offer": f"Offered a {float(amount):.2f} {cur} refund",
             "accept": "Accepted the claim"}[action]
    msg = f"{label} on dispute {dispute_id}" + (" (demo dispute)" if demo else "")
    db.log("dispute_action", msg, d.get("invoice_id"))
    return {"ok": True, "message": msg}

if __name__ == "__main__":
    ds = list_disputes()
    print("Disputes:", [(x["id"], x["reason"], x["status"], "demo" if x["demo"] else "real") for x in ds])
    if ds:
        out = assess(ds[0]["id"], "All 3 logo concepts were emailed on 20 Oct; client approved logo 1 on a call.")
        a = out["assessment"]
        print("\nEvidence found for invoice:", out["evidence"]["invoice"] and out["evidence"]["invoice"]["number"])
        print("Claim     :", a["buyer_claim"])
        print("Strength  :", a["strength"], f"(win ~{a['win_likelihood']:.0%})")
        print("Recommend :", a["recommendation"], "| refund:", a["suggested_refund"])
        print("Reasoning :", a["reasoning"])
        print("Facts     :", *a["facts_for_seller"], sep="\n  - ")
        print("Missing   :", *a["missing_evidence"], sep="\n  - ")
