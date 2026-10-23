import sys, re, pathlib
import paypal_client as pp

if len(sys.argv) < 2:
    sys.exit("Usage: python register_webhook.py https://your-public-url")
base = sys.argv[1].rstrip("/")
wh = pp.register_webhook(base + "/api/webhooks/paypal")
env = pathlib.Path(".env")
txt = env.read_text(encoding="utf-8-sig")
txt = re.sub(r"^PAYPAL_WEBHOOK_ID=.*\n?", "", txt, flags=re.M).rstrip("\n") + f"\nPAYPAL_WEBHOOK_ID={wh['id']}\n"
env.write_text(txt, encoding="utf-8")
print("Webhook registered:", wh["id"], "->", wh["url"])
