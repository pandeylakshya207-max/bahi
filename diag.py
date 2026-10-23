import os
from dotenv import load_dotenv
load_dotenv()
for k in ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"]:
    v = os.getenv(k) or ""
    print(f"{k}: length={len(v)}, starts={v[:2]!r}, spaces={v != v.strip() or ' ' in v}, quotes={chr(34) in v or chr(39) in v}")
v = os.getenv("GEMINI_API_KEY") or ""; print(f"GEMINI_API_KEY: length={len(v)}, starts={v[:4]!r}")
