import os, httpx
from dotenv import load_dotenv
from google import genai
load_dotenv()

# --- PayPal ---
try:
    base = os.environ["PAYPAL_BASE"]
    r = httpx.post(f"{base}/v1/oauth2/token",
                   auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                   data={"grant_type": "client_credentials"}, timeout=30)
    print("PayPal login:", r.status_code, "OK" if r.status_code == 200 else r.text[:300])
    if r.status_code == 200:
        scopes = r.json().get("scope", "")
        for s in ["invoicing", "disputes", "payments", "webhook"]:
            print(f"   {s:10} scope:", any(s in x for x in scopes.split()))
except Exception as e:
    print("PayPal error:", e)

# --- Gemini ---
try:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    models = [m.name for m in client.models.list() if "flash" in m.name]
    print("Gemini flash models:", models[:8])
    m = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    resp = client.models.generate_content(model=m, contents="Reply with exactly: Bahi is alive")
    print("Gemini:", m, "->", resp.text.strip())
except Exception as e:
    print("Gemini error:", e)
