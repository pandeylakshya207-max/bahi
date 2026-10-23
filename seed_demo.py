"""Creates a realistic demo ledger in the PayPal sandbox (overdue clients, multiple currencies)."""
import datetime as dt
import paypal_client as pp
import db

db.init()
T = dt.date.today()

def make(name, email, items, cur, issued_days_ago, due_in_days, note):
    first, _, last = name.partition(" ")
    body = {"detail": {"invoice_number": pp.next_invoice_number(), "currency_code": cur, "note": note,
                       "invoice_date": (T - dt.timedelta(days=issued_days_ago)).isoformat(),
                       "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED",
                                        "due_date": (T + dt.timedelta(days=due_in_days)).isoformat()}},
            "primary_recipients": [{"billing_info": {"name": {"given_name": first, "surname": last},
                                                     "email_address": email}}],
            "items": [{"name": n, "quantity": str(q), "unit_amount": {"currency_code": cur, "value": f"{p:.2f}"},
                       "unit_of_measure": "QUANTITY"} for n, q, p in items]}
    body["invoicer"] = {"business_name": pp.os.getenv("BAHI_BUSINESS_NAME", "Aarav Designs")}
    res = pp.api("POST", "/v2/invoicing/invoices", json=body)
    iid = res.get("id") or res["href"].rstrip("/").split("/")[-1]
    inv = pp.send_invoice(iid)
    db.upsert_invoice(inv, source="demo", language="English")
    s = pp.summarize(inv)
    db.log("invoice_sent", f"Invoice #{s['number']} for {s['amount']} sent to {first}", iid)
    print(f"  #{s['number']}  {first:8} {s['amount']:>12}  {s['status']:6}  due {body['detail']['payment_term']['due_date']}")
    return inv

print("Creating demo invoices in the PayPal sandbox...")
make("Emily Carter", "emily@northwind-studio.example.com",
     [("Brand identity kit", 1, 600)], "USD", 24, -12,
     "Thank you for the brand identity project, Emily!")
make("Lukas Weber", "lukas@weber-verlag.example.com",
     [("Blog illustrations", 4, 45)], "EUR", 30, -16,
     "Danke, Lukas! It was a pleasure illustrating your blog.")
make("Olivia Brown", "olivia@brownandco.example.com",
     [("Website maintenance (monthly)", 1, 120)], "GBP", 2, 5,
     "Thanks, Olivia, for another month of working together.")
dan = make("Daniel Kim", "daniel@kimlabs.example.com",
           [("Mobile app UI mockups", 6, 150)], "USD", 9, 5,
           "Thanks, Daniel! Excited to see the app come to life.")
pp.api("POST", f"/v2/invoicing/invoices/{dan['id']}/payments",
       json={"method": "BANK_TRANSFER", "payment_date": T.isoformat(), "note": "Received via bank transfer",
             "amount": {"currency_code": "USD", "value": "900.00"}})
print("  Daniel's invoice recorded as paid by bank transfer (webhook will update Bahi).")
print("Done.")
