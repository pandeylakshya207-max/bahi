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

# ---- serve the web app (must stay at the very end) ----
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="static", html=True), name="static")
