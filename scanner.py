"""
Real Estate Investment Scanner
Runs via GitHub Actions cron, saves results to listings.json
"""
import os, json, time, datetime, smtplib, hashlib, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import anthropic

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALERT_EMAIL       = os.environ.get("ALERT_EMAIL", "")
GMAIL_USER        = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS    = os.environ.get("GMAIL_APP_PASS", "")
CITY              = os.environ.get("SCAN_CITY", "Köln")
LISTINGS_FILE     = "docs/listings.json"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Helpers ──────────────────────────────────────────────────────────────────
def listing_id(l):
    """Stable ID based on URL or title+price"""
    key = l.get("url") or f"{l.get('title','')}-{l.get('price',0)}"
    return hashlib.md5(key.encode()).hexdigest()[:12]

def load_existing():
    if os.path.exists(LISTINGS_FILE):
        with open(LISTINGS_FILE) as f:
            data = json.load(f)
            return data.get("listings", [])
    return []

def save_listings(listings, summary=""):
    os.makedirs("docs", exist_ok=True)
    data = {
        "last_scan": datetime.datetime.utcnow().isoformat() + "Z",
        "city": CITY,
        "total": len(listings),
        "market_summary": summary,
        "listings": listings,
    }
    with open(LISTINGS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved {len(listings)} listings to {LISTINGS_FILE}")

def merge(existing, incoming):
    existing_ids = {listing_id(l) for l in existing}
    fresh = [l for l in incoming if listing_id(l) not in existing_ids]
    merged = fresh + existing
    merged.sort(key=lambda l: l.get("investment_score", 0), reverse=True)
    return merged, fresh

# ── Search batches ────────────────────────────────────────────────────────────
def build_batches(city):
    return [
        [f"{city} Wohnung kaufen immobilienscout24",
         f"{city} Wohnung kaufen immowelt",
         f"{city} Wohnung kaufen immonet",
         f"{city} Wohnung kaufen kleinanzeigen"],
        [f"Wohnung kaufen {city} unter 100000",
         f"Wohnung kaufen {city} 100000 bis 200000",
         f"Wohnung kaufen {city} 200000 bis 300000",
         f"Wohnung kaufen {city} 300000 bis 500000"],
        [f"Eigentumswohnung kaufen {city}",
         f"Haus kaufen {city} günstig",
         f"Mehrfamilienhaus kaufen {city}",
         f"Kapitalanlage {city} kaufen"],
        [f"Renditeimmobilien {city} kaufen",
         f"vermietete Wohnung kaufen {city}",
         f"{city} Immobilien Anlage günstig",
         f"{city} Nord Wohnung kaufen",
         f"{city} Süd Wohnung kaufen"],
        [f"{city} Ost Wohnung kaufen",
         f"{city} West Wohnung kaufen",
         f"Kapitalanlage Wohnung {city} Rendite",
         f"{city} Immobilien Investor günstig"],
    ]

# ── Single batch scan ─────────────────────────────────────────────────────────
def scan_batch(queries, batch_idx):
    search_list = "\n".join(f"{i+1}. {q}" for i,q in enumerate(queries))
    print(f"\n🔍 Batch {batch_idx}: running {len(queries)} searches…")

    # Step 1 — search
    try:
        resp1 = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                f"Search for real estate listings for sale in {CITY}. Run each search:\n{search_list}\n\n"
                f"For every individual listing found note: title, district, price (€), size (m²), "
                f"direct listing URL, property type, year built, days listed, rental income. "
                f"List raw data only, no analysis. Aim for 20+ results."
            }]
        )
        search_text = "".join(b.text for b in resp1.content if hasattr(b, "text"))
        if not search_text:
            print(f"  ⚠️  Batch {batch_idx}: no text from step 1")
            return []
    except Exception as e:
        print(f"  ⚠️  Batch {batch_idx} step1 error: {e}")
        return []

    # Step 2 — convert to JSON
    try:
        resp2 = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=16000,
            messages=[
                {"role": "user",    "content": f"Find real estate listings in {CITY}."},
                {"role": "assistant","content": search_text},
                {"role": "user",    "content":
                    f"Convert every listing above to JSON. Include ALL listings, any price.\n"
                    f"- monthly_rent: listed value or estimate (central {CITY} €13-15/m², outer €9-11/m²)\n"
                    f"- gross_yield: (monthly_rent*12)/price*100\n"
                    f"- price_per_m2: price/size_m2\n"
                    f"- url: direct expose URL (e.g. immobilienscout24.de/expose/123456)\n"
                    f"- recommendation: 'Strong Buy' if yield>5% OR price_per_m2<3000, else 'Watch' or 'Pass'\n"
                    f"- investment_score: 1-10\n\n"
                    f"Output ONLY raw JSON starting with {{ ending with }}, no other text:\n"
                    f'{{\"listings\":[{{\"title\":\"\",\"district\":\"\",\"price\":0,\"size_m2\":0,'
                    f'\"price_per_m2\":0,\"monthly_rent\":0,\"rent_is_estimated\":true,\"gross_yield\":0,'
                    f'\"days_listed\":null,\"property_type\":\"\",\"year_built\":null,\"url\":\"\",'
                    f'\"key_positives\":[\"\",\"\"],\"key_risks\":[\"\",\"\"],\"verdict\":\"\",'
                    f'\"recommendation\":\"\",\"investment_score\":0}}]}}'
                }
            ]
        )
        text2 = "".join(b.text for b in resp2.content if hasattr(b, "text"))
        if not text2:
            print(f"  ⚠️  Batch {batch_idx}: no text from step 2")
            return []

        # Parse JSON — with repair fallback
        s = re.sub(r'^```(?:json)?\s*', '', text2.strip(), flags=re.I)
        s = re.sub(r'\s*```$', '', s)
        a, b = s.find("{"), s.rfind("}")
        if a == -1 or b == -1:
            print(f"  ⚠️  Batch {batch_idx}: no JSON found")
            return []
        s = s[a:b+1]
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            cut = max(s.rfind("},\n"), s.rfind("},\r\n"), s.rfind("},"))
            if cut > 0:
                try:
                    parsed = json.loads(s[:cut+1] + "]}")
                    print(f"  🔧 Batch {batch_idx}: JSON repaired")
                except:
                    print(f"  ⚠️  Batch {batch_idx}: JSON unrecoverable")
                    return []
            else:
                return []

        results = [l for l in parsed.get("listings", []) if l.get("price",0) > 0 and l.get("size_m2",0) > 0]
        print(f"  ✅ Batch {batch_idx}: {len(results)} listings found")
        return results

    except Exception as e:
        print(f"  ⚠️  Batch {batch_idx} step2 error: {e}")
        return []

# ── Email alert ───────────────────────────────────────────────────────────────
def send_alert(new_strong_buys):
    if not (ALERT_EMAIL and GMAIL_USER and GMAIL_APP_PASS):
        print("⚠️  Alert email not configured — skipping")
        return
    body_lines = []
    for l in new_strong_buys:
        body_lines.append(
            f"🏠 {l.get('title','')}\n"
            f"📍 {l.get('district','')} | {l.get('property_type','')}\n"
            f"💰 €{l.get('price',0):,.0f} | {l.get('size_m2',0)}m² | "
            f"€{l.get('price_per_m2',0):,.0f}/m² | Yield: {l.get('gross_yield',0):.1f}%\n"
            f"🔗 {l.get('url','—')}\n"
            f"📋 {l.get('verdict','')}"
        )
    body = f"Found {len(new_strong_buys)} new Strong Buy listing(s) in {CITY}:\n\n" + "\n\n---\n\n".join(body_lines)
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = ALERT_EMAIL
    msg["Subject"] = f"🏠 {len(new_strong_buys)} Strong Buy(s) in {CITY} — Investment Scanner"
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASS)
            s.send_message(msg)
        print(f"✉️  Alert sent to {ALERT_EMAIL} ({len(new_strong_buys)} listings)")
    except Exception as e:
        print(f"⚠️  Email failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🏙️  Starting scan for {CITY} at {datetime.datetime.utcnow().isoformat()}Z")
    existing   = load_existing()
    print(f"📦 Loaded {len(existing)} existing listings from database")
    batches    = build_batches(CITY)
    all_new    = []

    for i, batch in enumerate(batches):
        results = scan_batch(batch, i+1)
        if results:
            _, fresh = merge(existing + all_new, results)
            all_new.extend(fresh)
        time.sleep(2)  # be polite between batches

    merged, _ = merge(existing, all_new)
    print(f"\n📊 Scan complete: {len(all_new)} new listings added, {len(merged)} total in database")

    # Save market summary from last successful batch (optional)
    save_listings(merged)

    # Alert on new Strong Buys
    new_strong_buys = [l for l in all_new if l.get("recommendation") == "Strong Buy"]
    if new_strong_buys:
        print(f"🟢 {len(new_strong_buys)} new Strong Buys found — sending alert")
        send_alert(new_strong_buys)
    else:
        print("No new Strong Buys this scan")

if __name__ == "__main__":
    main()
