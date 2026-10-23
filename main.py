from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
import paypal_client as pp
import db
from ai import extract_invoice, InvoiceDraft, AIBusyError

app = FastAPI(title="Bahi API", description="AI back office for freelancers paid through PayPal")
db.init()

@app.exception_handler(AIBusyError)
async def ai_busy(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

class TextIn(BaseModel):
    text: str

@app.post("/api/extract/text", summary="Messy text (English/Hindi/Hinglish) -> invoice draft (AI)")
async def extract_text(body: TextIn):
    if not body.text.strip():
        raise HTTPException(400, "Text is empty")
    draft = await run_in_threadpool(extract_invoice, body.text, None, None)
    return draft.model_dump()

@app.post("/api/extract/file", summary="Photo of a bill or voice note -> invoice draft (AI)")
async def extract_file(file: UploadFile = File(...), text: Optional[str] = Form(None)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "File is empty")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 15 MB)")
    draft = await run_in_threadpool(extract_invoice, text, data, file.content_type)
    return draft.model_dump()

@app.post("/api/invoices", summary="Confirmed draft -> real PayPal invoice, sent to client")
async def create(draft: InvoiceDraft):
    if not draft.client_email:
        raise HTTPException(400, "client_email is required before sending")
    if not draft.items:
        raise HTTPException(400, "Invoice needs at least one item")
    try:
        inv = await run_in_threadpool(pp.create_invoice, draft.client_name, draft.client_email,
                                      [i.model_dump() for i in draft.items], draft.currency,
                                      draft.due_days, draft.note or "")
        inv = await run_in_threadpool(pp.send_invoice, inv["id"])
    except pp.PayPalError as e:
        db.log("error", str(e))
        raise HTTPException(502, str(e))
    db.upsert_invoice(inv, source="ai", language=draft.input_language)
    s = pp.summarize(inv)
    db.log("invoice_sent", f"Invoice #{s['number']} for {s['amount']} sent to {draft.client_name}", inv["id"])
    return s

@app.get("/api/invoices", summary="Ledger: all invoices")
def invoices():
    return db.list_invoices()

@app.post("/api/invoices/{invoice_id}/refresh", summary="Re-check status with PayPal")
async def refresh(invoice_id: str):
    inv = await run_in_threadpool(pp.get_invoice, invoice_id)
    db.upsert_invoice(inv)
    return pp.summarize(inv)

@app.get("/api/events", summary="Activity feed")
def events():
    return db.list_events()

# ---- PayPal webhooks: payments update the ledger live ----
import os
from fastapi import Request
from dotenv import dotenv_values

def _webhook_id():
    return os.getenv("PAYPAL_WEBHOOK_ID") or dotenv_values(".env").get("PAYPAL_WEBHOOK_ID")

@app.post("/api/webhooks/paypal", include_in_schema=False)
async def paypal_webhook(request: Request):
    event = await request.json()
    etype = event.get("event_type", "UNKNOWN")
    wh_id = _webhook_id()
    ok = bool(wh_id) and await run_in_threadpool(pp.verify_webhook, request.headers, event, wh_id)
    if not ok:
        db.log("webhook_rejected", f"Rejected an unverified webhook ({etype})")
        raise HTTPException(400, "Webhook signature verification failed")
    res = event.get("resource") or {}
    inv_id = res.get("id") if str(res.get("id", "")).startswith("INV2") else (res.get("invoice") or {}).get("id")
    if etype.startswith("INVOICING.") and inv_id:
        inv = await run_in_threadpool(pp.get_invoice, inv_id)
        db.upsert_invoice(inv)
        s = pp.summarize(inv)
        name = ((inv.get("primary_recipients") or [{}])[0].get("billing_info", {})
                .get("name", {}).get("given_name") or "Client")
        if etype == "INVOICING.INVOICE.PAID":
            db.log("payment_received", f"{name} paid {s['amount']} on invoice #{s['number']}", inv_id)
        else:
            db.log("invoice_update", f"Invoice #{s['number']} for {name} is now {s['status']}", inv_id)
    else:
        db.log("paypal_event", event.get("summary") or etype)
    return {"ok": True}
# ---- Bahi agent: tool-calling over PayPal, money moves only after confirmation ----
import agent

class ChatIn(BaseModel):
    message: str
    history: list[dict] = []

@app.post("/api/agent", summary="Talk to Bahi's agent")
async def agent_chat(body: ChatIn):
    return await run_in_threadpool(agent.chat, body.message, body.history)

@app.get("/api/actions", summary="Actions waiting for confirmation")
def pending_actions():
    return list(agent.PENDING.values())

@app.post("/api/actions/{action_id}/confirm", summary="User confirms an agent action")
async def confirm_action(action_id: str):
    try:
        return await run_in_threadpool(agent.execute, action_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except pp.PayPalError as e:
        db.log("error", str(e))
        raise HTTPException(502, str(e))

@app.post("/api/actions/{action_id}/dismiss", summary="User rejects an agent action")
def dismiss_action(action_id: str):
    if not agent.PENDING.pop(action_id, None):
        raise HTTPException(404, "Action not found")
    return {"ok": True}

# ---- Dispute Defender ----
import disputes

class AssessIn(BaseModel):
    seller_notes: str = ""

class DisputeActIn(BaseModel):
    action: str
    text: str
    amount: float = 0

@app.get("/api/disputes", summary="Real PayPal disputes + labelled demo disputes")
async def disputes_list():
    return await run_in_threadpool(disputes.list_disputes)

@app.post("/api/disputes/{dispute_id}/assess", summary="Gather evidence and let the AI assess the case")
async def disputes_assess(dispute_id: str, body: AssessIn):
    try:
        return await run_in_threadpool(disputes.assess, dispute_id, body.seller_notes)
    except pp.PayPalError as e:
        raise HTTPException(502, str(e))

@app.post("/api/disputes/{dispute_id}/act", summary="User-confirmed response: respond / offer / accept")
async def disputes_act(dispute_id: str, body: DisputeActIn):
    try:
        return await run_in_threadpool(disputes.act, dispute_id, body.action, body.text, body.amount)
    except (pp.PayPalError, ValueError) as e:
        raise HTTPException(502, str(e))

# ---- serve the web app (must stay at the very end) ----
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="static", html=True), name="static")


