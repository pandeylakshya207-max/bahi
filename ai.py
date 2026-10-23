import os, json, time, datetime, httpx
from typing import Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from google import genai
from google.genai import types, errors as genai_errors
load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=20000, retry_options=types.HttpRetryOptions(attempts=1)))
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
FALLBACK_MODELS = [MODEL] + [m for m in ["gemini-3-flash-preview", "gemini-flash-lite-latest"] if m != MODEL]

class AIBusyError(Exception):
    pass

def generate(contents, config):
    """Try the main model, retry on overload, then fall back to backup models."""
    last = None
    for model in FALLBACK_MODELS:
        for attempt in range(2):
            try:
                return client.models.generate_content(model=model, contents=contents, config=config)
            except (genai_errors.ServerError, httpx.TimeoutException) as e:          # 500/503 overload -> wait and retry
                last = e
                time.sleep(1.5 * (attempt + 1))
            except genai_errors.ClientError as e:          # 404 model gone / 429 quota -> next model
                if getattr(e, "code", None) in (404, 429):
                    last = e
                    break
                raise
    raise AIBusyError(f"Bahi's AI is busy right now. Please try again in a few seconds. ({last})")

class Item(BaseModel):
    name: str = Field(description="Short professional English name of the service/product")
    quantity: float
    unit_amount: float = Field(description="Price per single unit")

class InvoiceDraft(BaseModel):
    client_name: str
    client_email: Optional[str] = Field(None, description="Only if explicitly given. Never invent.")
    currency: str = Field(description="ISO code e.g. USD, EUR, GBP, INR")
    due_days: int = Field(description="Days from today until payment is due")
    items: list[Item]
    note: Optional[str] = Field(None, description="Short polite note to the client, in English")
    input_language: str = Field(description="Language of the user's input, e.g. Hinglish, Hindi, English")
    missing_fields: list[str] = Field(description="Required info that was NOT provided, e.g. client_email")
    confidence: float = Field(description="0 to 1, how sure you are about the extraction")

PROMPT = """You are Bahi, an AI back office for Indian freelancers and small exporters who get paid through PayPal.
Extract an invoice from the user's input (text, a photo of a handwritten bill, or a voice note).
Rules:
- Input may be English, Hindi, Hinglish or mixed. Understand slang like 'bhejo', 'ka', 'wala'.
- Translate item names into short professional English.
- If a total is given for N units, compute unit_amount = total / N.
- Currency: $ or dollars -> USD, euro -> EUR, pound -> GBP, rupees/Rs/₹ -> INR. Default USD if unclear.
- due_days: convert phrases like 'next Friday' or '2 weeks' using today's date ({today}). Default 7.
- NEVER invent an email. If missing, put 'client_email' in missing_fields.
- Write a warm, professional one-line note to the client."""

def extract_invoice(text=None, file_bytes=None, mime_type=None) -> InvoiceDraft:
    parts = []
    if file_bytes:
        parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
    parts.append(PROMPT.format(today=datetime.date.today().strftime("%A, %d %B %Y"))
                 + (f"\n\nUser input: {text}" if text else "\n\nExtract the invoice from the attached file."))
    resp = generate(parts, types.GenerateContentConfig(response_mime_type="application/json",
                                                       response_schema=InvoiceDraft, temperature=0.1))
    return resp.parsed if isinstance(resp.parsed, InvoiceDraft) else InvoiceDraft.model_validate_json(resp.text)

if __name__ == "__main__":
    t = "Sarah ko 3 logo designs ka total $450 bhejo, sarah@acme.com, payment 2 hafte me"
    print(json.dumps(extract_invoice(t).model_dump(), indent=1, ensure_ascii=False))

