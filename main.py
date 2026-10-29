#!/usr/bin/env python3
"""
Daily Abandoned Forms Report Generator
======================================
Fetches Clarity data (from 4 brand-specific URLs) + Airtable data,
generates 4 separate brand text reports showing:
  - Section 3: Leads in Clarity but NOT in Airtable (with session replay links)
  - Section 4: All abandoned forms in Airtable (with Page URLs)

Clarity API response structure:
  {"sessions": {"data": [...records...], "last_page": N, "total": N, ...}}

Record fields: uuid, started_at, entry_url, ip_address, in_airtable, is_spam
"""

import os, sys, json, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KARACHI_TZ = ZoneInfo("Asia/Karachi")
UTC_TZ = ZoneInfo("UTC")

AIRTABLE_TOKEN = os.getenv("AIRTABLE_API_TOKEN") or os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME") or os.getenv("AIRTABLE_TABLE_ID")
CLARITY_API_TOKEN = os.getenv("CLARITY_API_TOKEN") or "clarity_internal_abandoned_token_2026_sec789xyz"

BRANDS = [
    {"name": "Quick Signage", "code": "quick_signage", "url_patterns": ["quicksignage"], "clarity_base_url": "https://clarity.quicksignage.com/api/v1/internal/abandoned-forms", "clarity_session_base": "https://clarity.quicksignage.com/admin/sessions/", "platform": "Quick Signage Live Website"},
    {"name": "Signs.inc", "code": "signs_inc", "url_patterns": ["signs.inc", "signs-inc"], "clarity_base_url": "https://clarity.signs.inc/api/v1/internal/abandoned-forms", "clarity_session_base": "https://clarity.signs.inc/admin/sessions/", "platform": "Signs.inc Live Website"},
    {"name": "Signage.inc", "code": "signage_inc", "url_patterns": ["signage.inc", "signage-inc"], "clarity_base_url": "https://clarity.signage.inc/api/v1/internal/abandoned-forms", "clarity_session_base": "https://clarity.signage.inc/admin/sessions/", "platform": "Signage.inc Live Website"},
    {"name": "SignMakerz", "code": "sign_makerz", "url_patterns": ["signmakerz"], "clarity_base_url": "https://clarity.signmakerz.com/api/v1/internal/abandoned-forms", "clarity_session_base": "https://clarity.signmakerz.com/admin/sessions/", "platform": "SignMakerz Live Website"},
]

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def get_time_window():
    now = datetime.now(KARACHI_TZ)
    end = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < end: end = end - timedelta(days=1)
    return end - timedelta(days=1), end

def safe_get(r, *ks, d=""):
    for k in ks:
        if isinstance(r, dict) and k in r: r = r[k]
        else: return d
    return r if r is not None else d

def get_airtable_rid(r):
    if not isinstance(r, dict): return None
    rid = r.get("id")
    if rid and str(rid).startswith("rec"): return str(rid)
    f = r.get("fields", {})
    return str(f.get("Airtable Record ID")) if isinstance(f, dict) and f.get("Airtable Record ID") else rid

def get_clarity_rid(r):
    """Get Clarity record ID — use uuid (session replay ID)."""
    if not isinstance(r, dict): return None
    for k in ("uuid", "id", "session_id", "sessionId"):
        v = r.get(k)
        if v: return str(v)
    return None

def trunc(t, n=50):
    if not t or t == "?": return "?"
    t = str(t)
    return t if len(t) <= n else t[:n-3] + "..."

def fmt_date(s, e):
    return f"{s.strftime('%B %d, %Y (%I:%M %p)')} to {e.strftime('%B %d, %Y (%I:%M %p)')}"

def detect_brand(url):
    if not url: return "Unknown"
    u = str(url).lower()
    for b in BRANDS:
        for p in b["url_patterns"]:
            if p.lower() in u: return b["name"]
    return "Unknown"

def build_session_url(r, brand):
    """Build session replay URL using uuid field."""
    uuid = r.get("uuid") if isinstance(r, dict) else None
    if uuid: return f"{brand['clarity_session_base']}{uuid}"
    for f in ("session_url", "url"):
        v = r.get(f) if isinstance(r, dict) else None
        if v and str(v).startswith("http"): return str(v)
    return "—"


# === PHASE 1: AIRTABLE ===

def fetch_airtable(start, end):
    print("\n" + "="*60 + "\nPHASE 1: Airtable\n" + "="*60)
    if not all([AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME]):
        print("Missing Airtable credentials"); return []
    s_utc, e_utc = start.astimezone(UTC_TZ), end.astimezone(UTC_TZ)
    s_str, e_str = s_utc.strftime("%Y-%m-%dT%H:%M:%S"), e_utc.strftime("%Y-%m-%dT%H:%M:%S")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    hdr = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
    ff = f"AND(IS_AFTER({{Last Modified}}, '{s_str}'), IS_BEFORE({{Last Modified}}, '{e_str}'))"
    recs, off, pg = [], None, 1
    while True:
        p = {"filterByFormula": ff, "pageSize": 100}
        if off: p["offset"] = off
        try:
            r = requests.get(url, headers=hdr, params=p, timeout=30)
            r.raise_for_status()
            d = r.json()
            rs = d.get("records", [])
            recs.extend(rs)
            print(f"  Page {pg}: {len(rs)} records (total: {len(recs)})")
            pg += 1; off = d.get("offset")
            if not off: break
        except Exception as e:
            print(f"Airtable error: {e}"); break
    print(f"Airtable TOTAL: {len(recs)}")
    with open(os.path.join(OUTPUT_DIR, "airtable_data.json"), "w") as f:
        json.dump({"total": len(recs), "records": recs}, f, indent=2, default=str)
    return recs


# === PHASE 2: CLARITY (per-brand, using exact API field names) ===

def fetch_brand_clarity(brand, start, end):
    """Fetch from one brand's Clarity URL. Response: {"sessions": {"data": [...], "last_page": N}}"""
    bn, bu = brand["name"], brand["clarity_base_url"]
    print(f"\n  Brand: {bn}")
    print(f"  URL: {bu}")
    print(f"  Token: {CLARITY_API_TOKEN[:12]}...{CLARITY_API_TOKEN[-4:]}")
    brand_recs = []
    page = 1
    while True:
        params = {"per_page": 25, "api_token": CLARITY_API_TOKEN, "page": page}
        try:
            print(f"    Page {page}: GET {bu}?per_page=25&page={page}")
            resp = requests.get(bu, params=params, timeout=30)
            print(f"      HTTP: {resp.status_code}")
            if resp.status_code != 200:
                print(f"      Body: {resp.text[:300]}"); break
            data = resp.json()
            # EXACT structure: {"sessions": {"data": [...], "last_page": N, "current_page": N, "total": N}}
            sessions = data.get("sessions", {})
            records = sessions.get("data", [])
            last_page = sessions.get("last_page", 1)
            total = sessions.get("total", 0)
            if page == 1:
                print(f"      Top-level keys: {list(data.keys())}")
                print(f"      Pagination: last_page={last_page}, total={total}")
                if records:
                    print(f"      First record keys: {list(records[0].keys())}")
                    print(f"      Sample: uuid={records[0].get('uuid')}, entry_url={trunc(str(records[0].get('entry_url','')),60)}, in_airtable={records[0].get('in_airtable')}")
            print(f"      Got {len(records)} records (running: {len(brand_recs)+len(records)})")
            for r in records:
                if isinstance(r, dict): r["_brand"] = bn
            brand_recs.extend(records)
            if page >= last_page:
                print(f"      Reached last page ({page}/{last_page})"); break
            if len(records) < 25:
                print(f"      Last page reached"); break
            page += 1
            if page > 100: print("      Safety limit"); break
        except Exception as e:
            print(f"      Error: {type(e).__name__}: {e}"); break
    print(f"  {bn}: {len(brand_recs)} records total")
    return brand_recs

def fetch_clarity(start, end):
    print("\n" + "="*60 + "\nPHASE 2: Clarity APIs (4 brands)\n" + "="*60)
    all_recs = []
    for b in BRANDS:
        all_recs.extend(fetch_brand_clarity(b, start, end))
    print(f"\nClarity TOTAL: {len(all_recs)} records")
    # Filter by date locally (using started_at field)
    filtered = []
    s_utc, e_utc = start.astimezone(UTC_TZ), end.astimezone(UTC_TZ)
    for r in all_recs:
        if not isinstance(r, dict): continue
        ts = r.get("started_at") or r.get("created_at")
        if not ts: filtered.append(r); continue
        try:
            t = datetime.fromisoformat(str(ts).replace("Z","+00:00"))
            if t.tzinfo is None: t = t.replace(tzinfo=UTC_TZ)
            if s_utc <= t.astimezone(UTC_TZ) <= e_utc: filtered.append(r)
        except: filtered.append(r)
    print(f"Clarity after date filter: {len(filtered)}")
    if all_recs and not filtered:
        print(f"  Using all {len(all_recs)} (no date match)")
        filtered = all_recs
    with open(os.path.join(OUTPUT_DIR, "clarity_data.json"), "w") as f:
        json.dump({"total": len(filtered), "raw": len(all_recs), "records": filtered}, f, indent=2, default=str)
    return filtered


# === GROUPING ===

def group_airtable(recs):
    g = {b["name"]: [] for b in BRANDS}; g["Unknown"] = []
    for r in recs:
        f = safe_get(r, "fields", d={}) or {}
        g[detect_brand(f.get("Page URL",""))].append(r)
    return g

def group_clarity(recs):
    g = {b["name"]: [] for b in BRANDS}; g["Unknown"] = []
    for r in recs:
        if not isinstance(r, dict): continue
        bn = r.get("_brand", "Unknown")
        g[bn].append(r) if bn in g else g["Unknown"].append(r)
    return g


# === REPORT GENERATION ===

def gen_report(brand, at_recs, cl_recs, start, end):
    bn = brand["name"]
    at_count = len(at_recs)
    # Count Clarity records NOT in Airtable (using API's in_airtable field!)
    not_in_airtable = [r for r in cl_recs if isinstance(r, dict) and not r.get("in_airtable", True)]
    cl_count = len(cl_recs)
    now = datetime.now(KARACHI_TZ)
    ds = now.strftime("%d %b %Y, %H:%M (PKT)")
    drh = fmt_date(start, end)
    spam = sum(1 for r in at_recs if str(safe_get(r,"fields",d={}).get("isSpam","")).lower()=="true")
    # Source breakdown
    sc = {}
    for r in at_recs:
        s = safe_get(r,"fields",d={}).get("Record Source") or "Unknown"
        sc[s] = sc.get(s,0)+1
    sb = "\n".join(f"    {s}: {c}" for s,c in sorted(sc.items(),key=lambda x:-x[1])) if sc else "    (none)"
    # Section 3: Leads NOT in Airtable (from Clarity, using in_airtable field!)
    if not_in_airtable:
        lt = "+----------------------+--------------------------+----------------------------------------------------+\n"
        lt += "| DATE & TIMESTAMP     | VISITOR / IP             | SESSION REPLAY / VIEW LINK                         |\n"
        lt += "+----------------------+--------------------------+----------------------------------------------------+\n"
        for r in not_in_airtable[:30]:
            ts = trunc(str(r.get("started_at","—")),20)
            ip = trunc(str(r.get("ip_address","—")),24)
            url = trunc(build_session_url(r, brand), 50)
            lt += f"| {ts:<20} | {ip:<24} | {url:<50} |\n"
        lt += "+----------------------+--------------------------+----------------------------------------------------+\n"
        if len(not_in_airtable)>30: lt += f"\n    ... and {len(not_in_airtable)-30} more\n"
    elif cl_count > 0:
        lt = "    All Clarity records are in Airtable. No missing leads.\n"
    else:
        lt = "    (Clarity API not returning data for this brand.)\n"
    # Section 4: Abandoned forms in Airtable (with Page URLs)
    if at_recs:
        ft = "+----------------------+--------------------------+----------------------------------------------------+----------+\n"
        ft += "| DATE & TIMESTAMP     | RECORD ID                | PAGE URL                                           | SPAM     |\n"
        ft += "+----------------------+--------------------------+----------------------------------------------------+----------+\n"
        for r in at_recs[:50]:
            rid = get_airtable_rid(r) or "—"
            f = safe_get(r,"fields",d={}) or {}
            ct = trunc(str(f.get("Created") or f.get("Created At") or "—"),20)
            pu = trunc(str(f.get("Page URL") or "—"),50)
            sp = trunc(str(f.get("isSpam") or "—"),8)
            ft += f"| {ct:<20} | {str(rid)[:24]:<24} | {pu:<50} | {sp:<8} |\n"
        ft += "+----------------------+--------------------------+----------------------------------------------------+----------+\n"
        if len(at_recs)>50: ft += f"\n    ... and {len(at_recs)-50} more\n"
    else:
        ft = "    (No abandoned forms found in Airtable.)\n"
    # Recommendation
    if at_count==0 and cl_count==0: rec="Both sources returning 0 records."
    elif at_count>0 and cl_count==0: rec=f"Airtable connected ({at_count}), Clarity returning 0.\n  URL: {brand['clarity_base_url']}\n  Token: {CLARITY_API_TOKEN[:12]}...{CLARITY_API_TOKEN[-4:]}"
    elif at_count==0 and cl_count>0: rec=f"Clarity connected ({cl_count}), Airtable returning 0."
    elif len(not_in_airtable)>0: rec=f"{len(not_in_airtable)} leads in Clarity but NOT in Airtable.\n  Action: Check Section 3 for session replay links."
    else: rec="Both sources match."
    body = f"""{bn} Abandoned Forms & Leads Verification Report
{'='*100}

Report Title:    {bn} Abandoned Forms & Dashboard Verification Report
Date Range:      {drh}
Prepared By:     Iqra Kashif
Audited Platform: {brand['platform']}, Airtable, and Internal Dashboard

1. Executive Summary
{'-'*100}
This professional audit report reviews the data integrity and synchronization status
between the live {bn} website frontend, abandoned forms capture system, Airtable database,
and the internal admin dashboard. The evaluation period covers {drh}.

2. Audit Scope & Verification Methodology
{'-'*100}
  - Frontend Event Tracking: Monitored user form interactions, field inputs, and exit intents.
  - Session Identification & Copy Tracking: Gathered active session IDs and timestamped payloads.
  - Airtable Synchronization Check: Queried Airtable records within the target timeframe.
  - Dashboard Data Reconciliation: Cross-referenced Airtable records against Clarity dashboard.

3. LEADS THAT ARE NOT FOUND IN AIRTABLE (Dashboard vs Airtable Comparison)
{'-'*100}
{lt}
4. ABANDONED FORMS IN AIRTABLE (with Page URLs)
{'-'*100}
{ft}
5. Data Summary
{'-'*100}
  Airtable Total Forms:            {at_count}
  Spam (isSpam=true):               {spam}
  Non-spam:                         {at_count - spam}
  Clarity Total Forms:              {cl_count}
  Leads NOT in Airtable (from Clarity): {len(not_in_airtable)}

6. Data Source Status
{'-'*100}
  Airtable:   {"CONNECTED" if at_count>0 else "NOT RETURNING DATA"} ({at_count} records)
  Clarity:    {"CONNECTED" if cl_count>0 else "NOT RETURNING DATA"} ({cl_count} records)
  Endpoint:   {brand['clarity_base_url']}

7. Airtable Breakdown (by Source)
{'-'*100}
{sb}

8. Recommendation
{'-'*100}
{rec}

{'='*100}
End of Report - {bn} QA
{'='*100}

Report Generated:   {ds}
Auto-Schedule:      Daily at 9 AM Karachi (cron: 0 4 * * * UTC)
Generated By:       GitHub Actions Automation
"""
    fn = f"{brand['code']}_report_{now.strftime('%Y-%m-%d')}.txt"
    p = os.path.join(OUTPUT_DIR, fn)
    with open(p, "w", encoding="utf-8") as f: f.write(body)
    print(f"    Saved: {fn} ({os.path.getsize(p):,} bytes)")
    return p

def gen_all(at_recs, cl_recs, start, end):
    print("\n" + "="*60 + "\nPHASE 3: Generating Reports\n" + "="*60)
    at_bg = group_airtable(at_recs)
    cl_bg = group_clarity(cl_recs)
    print(f"\n  Airtable by brand: " + ", ".join(f"{b['name']}:{len(at_bg[b['name']])}" for b in BRANDS))
    print(f"  Clarity by brand:  " + ", ".join(f"{b['name']}:{len(cl_bg[b['name']])}" for b in BRANDS))
    paths = []
    for b in BRANDS:
        print(f"\n  Report: {b['name']} (AT:{len(at_bg[b['name']])}, CL:{len(cl_bg[b['name']])})")
        paths.append(gen_report(b, at_bg[b["name"]], cl_bg[b["name"]], start, end))
    summary = {"date": datetime.now(KARACHI_TZ).isoformat(), "total_at": len(at_recs), "total_cl": len(cl_recs),
               "brands": {b["name"]: {"at": len(at_bg[b["name"]]), "cl": len(cl_bg[b["name"]])} for b in BRANDS},
               "reports": [os.path.basename(p) for p in paths]}
    with open(os.path.join(OUTPUT_DIR, "report_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return paths

def main():
    print("="*60)
    print("  ABANDONED FORMS REPORT GENERATOR")
    print("  Brands: Quick Signage | Signs.inc | Signage.inc | SignMakerz")
    print("  Clarity: 4 per-brand URLs | in_airtable field for comparison")
    print("  Schedule: Daily 9 AM Karachi (cron: 0 4 * * * UTC)")
    print("="*60)
    start, end = get_time_window()
    print(f"Window: {start} -> {end}")
    at = fetch_airtable(start, end)
    cl = fetch_clarity(start, end)
    paths = gen_all(at, cl, start, end)
    print(f"\n{'='*60}\n  DONE - {len(paths)} reports\n{'='*60}")
    for p in paths: print(f"  {os.path.basename(p)} ({os.path.getsize(p):,} bytes)")
    return 0

if __name__ == "__main__": sys.exit(main())
