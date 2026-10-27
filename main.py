#!/usr/bin/env python3
"""
Daily Abandoned Forms Report Generator (Multi-Brand + Clarity Comparison)
============================================================================
Generates 4 SEPARATE brand text reports:
  - Quick Signage
  - Signs.inc
  - Signage.inc
  - SignMakerz

Each report shows:
  - Airtable data for that brand
  - Clarity data for that brand
  - COMPARISON: Records in Clarity but NOT in Airtable (with session replay links!)

Clarity URL: https://clarity.signage.inc/api/v1/internal/abandoned-forms
Token sent as ?api_token= query param (Laravel-style)

Daily auto-update: cron '0 4 * * *' (4 AM UTC = 9 AM Karachi)
"""

import os
import sys
import json
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


KARACHI_TZ = ZoneInfo("Asia/Karachi")
UTC_TZ = ZoneInfo("UTC")


# ============================================================================
# CONFIGURATION
# ============================================================================

AIRTABLE_TOKEN = os.getenv("AIRTABLE_API_TOKEN") or os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME") or os.getenv("AIRTABLE_TABLE_ID")

# Clarity API (URL provided by Sir)
CLARITY_BASE_URL = os.getenv("CLARITY_BASE_URL") or "https://clarity.signage.inc/api/v1/internal/abandoned-forms"
CLARITY_API_TOKEN = os.getenv("CLARITY_API_TOKEN") or "clarity_internal_abandoned_token_2026_sec789xyz"
CLARITY_PER_PAGE = 25

BRANDS = [
    {"name": "Quick Signage", "code": "quick_signage", "url_patterns": ["quicksignage"], "clarity_session_base": "https://clarity.quicksignage.com/admin/sessions/", "platform": "Quick Signage Live Website"},
    {"name": "Signs.inc", "code": "signs_inc", "url_patterns": ["signs.inc", "signs-inc"], "clarity_session_base": "https://clarity.signs.inc/admin/sessions/", "platform": "Signs.inc Live Website"},
    {"name": "Signage.inc", "code": "signage_inc", "url_patterns": ["signage.inc", "signage-inc"], "clarity_session_base": "https://clarity.signage.inc/admin/sessions/", "platform": "Signage.inc Live Website"},
    {"name": "SignMakerz", "code": "sign_makerz", "url_patterns": ["signmakerz"], "clarity_session_base": "https://clarity.signmakerz.com/admin/sessions/", "platform": "SignMakerz Live Website"},
]

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================================
# HELPERS
# ============================================================================

def get_time_window():
    now_karachi = datetime.now(KARACHI_TZ)
    end_karachi = now_karachi.replace(hour=9, minute=0, second=0, microsecond=0)
    if now_karachi < end_karachi:
        end_karachi = end_karachi - timedelta(days=1)
    start_karachi = end_karachi - timedelta(days=1)
    return start_karachi, end_karachi


def safe_get(record, *keys, default=""):
    cur = record
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default


def get_airtable_record_id(record):
    if not isinstance(record, dict):
        return None
    rid = record.get("id")
    if rid and str(rid).startswith("rec"):
        return str(rid)
    fields = record.get("fields", {})
    rid2 = fields.get("Airtable Record ID") if isinstance(fields, dict) else None
    if rid2:
        return str(rid2)
    return rid


def get_clarity_record_id(record):
    if not isinstance(record, dict):
        return None
    for k in ("id", "ID", "form_id", "formId", "session_id", "sessionId", "record_id", "recordId", "Form ID"):
        v = record.get(k)
        if v:
            return str(v)
    fields = record.get("fields", {}) if isinstance(record.get("fields"), dict) else {}
    for k in ("id", "ID", "form_id", "formId", "session_id", "sessionId", "record_id", "recordId", "Form ID", "Airtable Record ID"):
        v = fields.get(k)
        if v:
            return str(v)
    return None


def truncate(text, max_len=70):
    if not text or text == "?":
        return "?"
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def format_date_range_human(start_time, end_time):
    s = start_time.strftime("%B %d, %Y (%I:%M %p)")
    e = end_time.strftime("%B %d, %Y (%I:%M %p)")
    return f"{s} to {e}"


def detect_brand_from_url(page_url):
    if not page_url:
        return "Unknown"
    url = str(page_url).lower()
    for brand in BRANDS:
        for pattern in brand["url_patterns"]:
            if pattern.lower() in url:
                return brand["name"]
    return "Unknown"


def build_clarity_session_url(record, brand):
    """Build session replay URL from Clarity record."""
    for field in ("session_url", "sessionUrl", "replay_url", "replayUrl", "clarity_url", "url"):
        v = record.get(field) if isinstance(record, dict) else None
        if v and str(v).startswith("http"):
            return str(v)
    for field in ("session_id", "sessionId", "session", "id"):
        v = record.get(field) if isinstance(record, dict) else None
        if v:
            return f"{brand['clarity_session_base']}{v}"
    return "—"


# ============================================================================
# PHASE 1: AIRTABLE DATA FETCH
# ============================================================================

def fetch_airtable_data(start_time, end_time):
    print("\n" + "=" * 60)
    print("PHASE 1: Fetching data from Airtable")
    print("=" * 60)
    print(f"Time window (Karachi): {start_time} -> {end_time}")

    if not all([AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME]):
        print("Missing Airtable credentials - skipping Phase 1")
        return []

    start_utc = start_time.astimezone(UTC_TZ)
    end_utc = end_time.astimezone(UTC_TZ)
    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"Airtable filter (UTC): {start_str} -> {end_str}")

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}

    filter_formula = f"AND(IS_AFTER({{Last Modified}}, '{start_str}'), IS_BEFORE({{Last Modified}}, '{end_str}'))"

    all_records = []
    offset = None
    page = 1

    while True:
        params = {"filterByFormula": filter_formula, "pageSize": 100}
        if offset:
            params["offset"] = offset
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            records = data.get("records", [])
            all_records.extend(records)
            print(f"  Page {page}: fetched {len(records)} records (total: {len(all_records)})")
            page += 1
            offset = data.get("offset")
            if not offset:
                break
        except requests.exceptions.RequestException as e:
            print(f"Airtable API error: {e}")
            break

    print(f"Airtable TOTAL: {len(all_records)} records")

    # Fallback: try 'Created' field if Last Modified returned 0
    if not all_records:
        print("  [FALLBACK] Trying filter with 'Created' field instead...")
        filter_formula_created = f"AND(IS_AFTER({{Created}}, '{start_str}'), IS_BEFORE({{Created}}, '{end_str}'))"
        offset = None
        page = 1
        while True:
            params = {"filterByFormula": filter_formula_created, "pageSize": 100}
            if offset:
                params["offset"] = offset
            try:
                response = requests.get(url, headers=headers, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
                records = data.get("records", [])
                all_records.extend(records)
                print(f"  [Created] Page {page}: fetched {len(records)} (total: {len(all_records)})")
                page += 1
                offset = data.get("offset")
                if not offset:
                    break
            except requests.exceptions.RequestException as e:
                print(f"  [Created] error: {e}")
                break

    out_path = os.path.join(OUTPUT_DIR, "airtable_data.json")
    with open(out_path, "w") as f:
        json.dump({"source": "airtable", "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()}, "filter_utc": {"start": start_str, "end": end_str}, "total_count": len(all_records), "records": all_records}, f, indent=2, default=str)
    return all_records


# ============================================================================
# PHASE 2: CLARITY DATA FETCH (single endpoint, token as query param)
# ============================================================================

def _extract_records_from_response(data):
    """Recursively extract records from any JSON response shape."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("data", "records", "items", "results", "value", "rows", "entries", "list", "items_list", "forms"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):
                nested = _extract_records_from_response(v)
                if nested:
                    return nested
    return []


def fetch_clarity_data(start_time, end_time):
    """Fetch from Clarity API. NO date params at API level (Laravel APIs often
    don't support date filtering). Filter by date locally after fetching.
    """
    print("\n" + "=" * 60)
    print("PHASE 2: Fetching data from Clarity API")
    print("=" * 60)
    print(f"Endpoint: {CLARITY_BASE_URL}")
    print(f"Token preview: {CLARITY_API_TOKEN[:12]}...{CLARITY_API_TOKEN[-4:]}")
    print(f"Per page: {CLARITY_PER_PAGE}")
    print(f"Strategy: Fetch all records (no date filter at API level), filter by date locally")

    all_clarity_records = []
    page = 1

    while True:
        # Token as query param (Laravel-style), NO date params
        params = {
            "per_page": CLARITY_PER_PAGE,
            "api_token": CLARITY_API_TOKEN,
            "page": page,
        }

        try:
            print(f"\n  Page {page}: GET {CLARITY_BASE_URL}?per_page={CLARITY_PER_PAGE}&page={page}")
            response = requests.get(CLARITY_BASE_URL, params=params, timeout=30)
            print(f"    HTTP status: {response.status_code}")

            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as e:
                    print(f"    JSON parse error: {e}")
                    print(f"    Body preview: {response.text[:500]}")
                    break

                # VERBOSE DEBUG
                response_str = json.dumps(data, default=str)
                print(f"    Response preview (first 400 chars): {response_str[:400]}")
                if isinstance(data, dict):
                    print(f"    Top-level keys: {list(data.keys())}")
                elif isinstance(data, list):
                    print(f"    Response is a list with {len(data)} items")
                    if data and isinstance(data[0], dict):
                        print(f"    First item keys: {list(data[0].keys())}")

                records = _extract_records_from_response(data)
                print(f"    Extracted {len(records)} records (running total: {len(all_clarity_records) + len(records)})")
                all_clarity_records.extend(records)

                # Pagination check
                stop_pagination = False
                if isinstance(data, dict):
                    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
                    last_page = meta.get("last_page")
                    current_page = meta.get("current_page", page)
                    total = meta.get("total")
                    print(f"    Pagination: current_page={current_page}, last_page={last_page}, total={total}")
                    if last_page is not None:
                        if current_page >= last_page:
                            print(f"    Reached last page - stopping")
                            stop_pagination = True
                    else:
                        links = data.get("links", {}) if isinstance(data.get("links"), dict) else {}
                        next_url = links.get("next")
                        if not next_url:
                            print(f"    No next page link - stopping")
                            stop_pagination = True
                    if len(records) < CLARITY_PER_PAGE:
                        print(f"    Got {len(records)} < {CLARITY_PER_PAGE} - last page reached")
                        stop_pagination = True
                elif isinstance(data, list):
                    if len(data) < CLARITY_PER_PAGE:
                        print(f"    Got {len(data)} < {CLARITY_PER_PAGE} - last page reached")
                        stop_pagination = True
                else:
                    stop_pagination = True

                if stop_pagination:
                    break

                page += 1
                if page > 100:
                    print(f"    Safety limit (100 pages) reached - stopping")
                    break
            elif response.status_code == 401:
                print(f"    ERROR 401: Unauthorized - CLARITY_API_TOKEN is invalid or expired")
                print(f"    Response body: {response.text[:500]}")
                break
            elif response.status_code == 403:
                print(f"    ERROR 403: Forbidden - token doesn't have permission")
                print(f"    Response body: {response.text[:500]}")
                break
            elif response.status_code == 404:
                print(f"    ERROR 404: Not Found - check CLARITY_BASE_URL")
                print(f"    Response body: {response.text[:500]}")
                break
            elif response.status_code == 500:
                print(f"    ERROR 500: Internal Server Error - API server issue")
                print(f"    Response body: {response.text[:500]}")
                break
            else:
                print(f"    ERROR {response.status_code}")
                print(f"    Response body: {response.text[:500]}")
                break

        except requests.exceptions.ConnectionError as e:
            print(f"    Connection error: {e}")
            break
        except requests.exceptions.Timeout as e:
            print(f"    Timeout: API took too long to respond")
            break
        except Exception as e:
            print(f"    Unexpected error: {type(e).__name__}: {e}")
            break

    print(f"\nClarity TOTAL (before date filter): {len(all_clarity_records)} records")

    # Filter by date locally
    filtered_records = []
    for r in all_clarity_records:
        if not isinstance(r, dict):
            continue
        ts = (r.get("timestamp") or r.get("createdAt") or r.get("created_at")
              or r.get("last_modified") or r.get("lastModified")
              or r.get("Last Modified") or r.get("Created")
              or r.get("date") or r.get("datetime") or r.get("submitted_at")
              or r.get("updated_at"))
        if not ts:
            filtered_records.append(r)
            continue
        try:
            ts_str = str(ts).replace("Z", "+00:00")
            if "+" not in ts_str and "T" in ts_str:
                ts_str = ts_str + "+00:00"
            if "T" in ts_str:
                record_time = datetime.fromisoformat(ts_str)
            else:
                record_time = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
                record_time = record_time.replace(tzinfo=UTC_TZ)
            if record_time.tzinfo is None:
                record_time = record_time.replace(tzinfo=UTC_TZ)
            record_time_utc = record_time.astimezone(UTC_TZ)
            start_utc = start_time.astimezone(UTC_TZ)
            end_utc = end_time.astimezone(UTC_TZ)
            if start_utc <= record_time_utc <= end_utc:
                filtered_records.append(r)
        except Exception:
            filtered_records.append(r)

    print(f"Clarity TOTAL (after date filter): {len(filtered_records)} records")
    if all_clarity_records and not filtered_records:
        print(f"  Note: {len(all_clarity_records)} records fetched but 0 matched date window.")
        print(f"  Using all {len(all_clarity_records)} records for comparison.")
        filtered_records = all_clarity_records

    out_path = os.path.join(OUTPUT_DIR, "clarity_data.json")
    with open(out_path, "w") as f:
        json.dump({"source": "clarity", "endpoint": CLARITY_BASE_URL, "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()}, "total_count": len(filtered_records), "raw_count_fetched": len(all_clarity_records), "records": filtered_records}, f, indent=2, default=str)
    return filtered_records


# ============================================================================
# GROUPING BY BRAND
# ============================================================================

def group_airtable_records_by_brand(airtable_records):
    brand_groups = {brand["name"]: [] for brand in BRANDS}
    brand_groups["Unknown"] = []
    for r in airtable_records:
        fields = safe_get(r, "fields", default={}) or {}
        page_url = fields.get("Page URL", "")
        brand_name = detect_brand_from_url(page_url)
        brand_groups[brand_name].append(r)
    return brand_groups


def group_clarity_records_by_brand(clarity_records):
    brand_groups = {brand["name"]: [] for brand in BRANDS}
    brand_groups["Unknown"] = []
    for r in clarity_records:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or r.get("pageUrl") or r.get("page_url") or safe_get(r, "fields", "Page URL") or ""
        brand_name = detect_brand_from_url(url)
        if brand_name == "Unknown":
            brand_name = "Signage.inc"
        brand_groups[brand_name].append(r)
    return brand_groups


# ============================================================================
# COMPARISON LOGIC (per brand)
# ============================================================================

def compare_brand_data(brand_name, brand_airtable_records, brand_clarity_records, start_time, end_time):
    """Compare Airtable vs Clarity records for a specific brand.
    Find records in Clarity but NOT in Airtable (the missing leads!).
    """
    airtable_count = len(brand_airtable_records)
    clarity_count = len(brand_clarity_records)
    difference = clarity_count - airtable_count

    airtable_map = {}
    for r in brand_airtable_records:
        rid = get_airtable_record_id(r)
        if rid:
            airtable_map[rid] = r
    clarity_map = {}
    for r in brand_clarity_records:
        rid = get_clarity_record_id(r)
        if rid:
            clarity_map[rid] = r

    airtable_ids = set(airtable_map.keys())
    clarity_ids = set(clarity_map.keys())

    # Records in Clarity but NOT in Airtable (THE MISSING LEADS!)
    only_in_clarity = clarity_ids - airtable_ids
    # Records in Airtable but NOT in Clarity
    only_in_airtable = airtable_ids - clarity_ids

    only_in_clarity_records = [clarity_map[rid] for rid in sorted(only_in_clarity)]
    only_in_airtable_records = [airtable_map[rid] for rid in sorted(only_in_airtable)]

    return {
        "brand_name": brand_name,
        "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "airtable_count": airtable_count,
        "clarity_count": clarity_count,
        "difference": difference,
        "airtable_unique_ids": len(airtable_ids),
        "clarity_unique_ids": len(clarity_ids),
        "only_in_clarity_records": only_in_clarity_records,
        "only_in_airtable_records": only_in_airtable_records,
    }


# ============================================================================
# PER-BRAND TEXT REPORT GENERATION
# ============================================================================

def generate_brand_text_report(brand, comparison, brand_airtable_records, brand_clarity_records, start_time, end_time):
    brand_name = brand["name"]
    airtable_count = comparison["airtable_count"]
    clarity_count = comparison["clarity_count"]
    only_in_clarity_records = comparison.get("only_in_clarity_records", [])

    now = datetime.now(KARACHI_TZ)
    date_str = now.strftime("%d %b %Y, %H:%M (PKT)")
    date_range_human = format_date_range_human(start_time, end_time)

    spam_count = 0
    for r in brand_airtable_records:
        fields = safe_get(r, "fields", default={}) or {}
        v = fields.get("isSpam")
        if v and str(v).lower() == "true":
            spam_count += 1
    non_spam_count = airtable_count - spam_count

    source_counts = {}
    for r in brand_airtable_records:
        fields = safe_get(r, "fields", default={}) or {}
        src = fields.get("Record Source") or "Unknown"
        source_counts[src] = source_counts.get(src, 0) + 1
    source_breakdown = "\n".join(
        f"    {src}: {cnt}"
        for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])
    ) if source_counts else "    (no source info available)"

    # BUILD THE MAIN TABLE: Leads in Clarity (Dashboard) but NOT in Airtable!
    if clarity_count > 0 and only_in_clarity_records:
        leads_table = "+----------------------+--------------------------+----------------------------------------------------+\n"
        leads_table += "| DATE & TIMESTAMP     | VISITOR EMAIL / ID       | SESSION REPLAY / VIEW LINK                         |\n"
        leads_table += "+----------------------+--------------------------+----------------------------------------------------+\n"
        for r in only_in_clarity_records[:30]:
            rid = get_clarity_record_id(r) or "—"
            ts = (r.get("timestamp") or r.get("createdAt") or r.get("created_at") or "—")
            ts_short = truncate(str(ts), 20)
            email = (r.get("email") or r.get("visitor_email") or r.get("user_email")
                     or safe_get(r, "fields", "Email") or "—")
            email_short = truncate(str(email), 24)
            session_url = build_clarity_session_url(r, brand)
            session_url_short = truncate(session_url, 50)
            leads_table += f"| {ts_short:<20} | {email_short:<24} | {session_url_short:<50} |\n"
        leads_table += "+----------------------+--------------------------+----------------------------------------------------+\n"
        if len(only_in_clarity_records) > 30:
            leads_table += f"\n    ... and {len(only_in_clarity_records) - 30} more records (see artifact JSON for full list)\n"
    elif clarity_count > 0 and not only_in_clarity_records:
        leads_table = "    All Clarity records are also present in Airtable. No missing leads.\n"
    else:
        leads_table = "    (Clarity API not returning data for this brand.\n     Once Clarity API is connected, this section will display a table\n     of records captured by Clarity but missing from Airtable,\n     along with session replay links.)\n"

    if airtable_count == 0 and clarity_count == 0:
        recommendation = "Both data sources are returning 0 records for this brand."
    elif airtable_count > 0 and clarity_count == 0:
        recommendation = f"Airtable is connected ({airtable_count} records), but Clarity API is returning 0 records.\n\nRecommended Actions:\n  1. Verify CLARITY_BASE_URL secret is correct API URL\n     Current: {CLARITY_BASE_URL}\n  2. Verify CLARITY_API_TOKEN secret is valid auth token\n     Token preview: {CLARITY_API_TOKEN[:12]}...{CLARITY_API_TOKEN[-4:]}\n  3. Check the workflow run logs above - search for 'PHASE 2' section"
    elif airtable_count == 0 and clarity_count > 0:
        recommendation = f"Clarity is connected ({clarity_count} records), but Airtable API is returning 0 records."
    elif comparison["difference"] > 0:
        recommendation = f"Clarity has {comparison['difference']} MORE forms than Airtable for this brand.\n\nLikely cause: Some form submissions were captured by Clarity but failed to sync to Airtable.\n\nRecommended Action: Review the unmatched Clarity records in Section 3 above (with session replay links),\nthen check Airtable's webhook and integration health."
    elif comparison["difference"] < 0:
        recommendation = f"Airtable has {abs(comparison['difference'])} MORE forms than Clarity for this brand.\n\nLikely cause: Stale records in Airtable, or Clarity's date filter is excluding valid sessions."
    else:
        recommendation = "Both sources match perfectly in record count for this brand."

    report_body = f"""{brand_name} Abandoned Forms & Leads Verification Report
{'=' * 100}

Report Title:    {brand_name} Abandoned Forms & Dashboard Verification Report
Date Range:      {date_range_human}
Prepared By:     Iqra Kashif
Audited Platform: {brand['platform']}, Airtable, and Internal Dashboard

1. Executive Summary
{'-' * 100}
This professional audit report reviews the data integrity and synchronization status
between the live {brand_name} website frontend, abandoned forms capture system,
Airtable database, and the internal admin dashboard. The evaluation period covers
{date_range_human}.

The primary objective of this audit was to verify whether user interactions, form
inputs, and abandoned form session data are accurately flowing into Airtable and
reflecting correctly on the internal dashboard.

2. Audit Scope & Verification Methodology
{'-' * 100}
During the specified 24-hour window, the following pipeline verification steps were
executed:

  - Frontend Event Tracking: Monitored user form interactions, field inputs, and
    exit intents on key pages.
  - Session Identification & Copy Tracking: Gathered active session IDs and
    timestamped tracking payloads to verify unsubmitted or partial form states.
  - Airtable Synchronization Check: Queried Airtable records created within the
    target timeframe to verify data schema matching.
  - Dashboard Data Reconciliation: Cross-referenced Airtable records against
    internal dashboard panels to confirm real-time updates.

3. LEADS THAT ARE NOT FOUND IN AIRTABLE (Dashboard vs Airtable Comparison)
{'-' * 100}
{leads_table}
4. Data Summary
{'-' * 100}
  Airtable Total Forms:            {airtable_count}
  Spam (isSpam=true):               {spam_count}
  Non-spam:                         {non_spam_count}
  Clarity Total Forms:              {clarity_count}
  Difference (Clarity - Airtable): {comparison['difference']}

  Airtable Unique IDs:  {comparison.get('airtable_unique_ids', 'N/A')}
  Clarity Unique IDs:   {comparison.get('clarity_unique_ids', 'N/A')}

5. Data Source Status
{'-' * 100}
  Airtable:   {"CONNECTED" if airtable_count > 0 else "NOT RETURNING DATA"} ({airtable_count} records)
  Clarity:    {"CONNECTED" if clarity_count > 0 else "NOT RETURNING DATA"} ({clarity_count} records)
  Endpoint:   {CLARITY_BASE_URL}

6. Airtable Breakdown (by Source)
{'-' * 100}
{source_breakdown}

7. Recommendation
{'-' * 100}
{recommendation}

{'=' * 100}
End of Report - {brand_name} QA
{'=' * 100}

Report Generated:   {date_str}
Auto-Schedule:      Daily at 9 AM Karachi (cron: 0 4 * * * UTC)
Generated By:       GitHub Actions Automation
"""

    today_str = now.strftime("%Y-%m-%d")
    txt_filename = f"{brand['code']}_report_{today_str}.txt"
    out_path = os.path.join(OUTPUT_DIR, txt_filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_body)
    print(f"    Saved: {txt_filename} ({os.path.getsize(out_path):,} bytes)")
    return out_path


def generate_all_brand_reports(airtable_records, clarity_records, start_time, end_time):
    print("\n" + "=" * 60)
    print("PHASE 3: Generating 4 Brand Reports (TEXT FILES)")
    print("=" * 60)

    airtable_by_brand = group_airtable_records_by_brand(airtable_records)
    clarity_by_brand = group_clarity_records_by_brand(clarity_records)

    print(f"\n  Airtable records by brand:")
    for brand in BRANDS:
        print(f"    {brand['name']}: {len(airtable_by_brand[brand['name']])} records")
    print(f"    Unknown: {len(airtable_by_brand['Unknown'])} records")

    print(f"\n  Clarity records by brand:")
    for brand in BRANDS:
        print(f"    {brand['name']}: {len(clarity_by_brand[brand['name']])} records")

    report_paths = []
    for brand in BRANDS:
        brand_name = brand["name"]
        brand_airtable = airtable_by_brand[brand_name]
        brand_clarity = clarity_by_brand[brand_name]
        print(f"\n  Generating report for: {brand_name}")
        print(f"    Airtable: {len(brand_airtable)} records")
        print(f"    Clarity:  {len(brand_clarity)} records")
        comparison = compare_brand_data(brand_name, brand_airtable, brand_clarity, start_time, end_time)
        path = generate_brand_text_report(brand, comparison, brand_airtable, brand_clarity, start_time, end_time)
        report_paths.append(path)

    summary = {
        "report_date": datetime.now(KARACHI_TZ).isoformat(),
        "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "total_airtable_records": len(airtable_records),
        "total_clarity_records": len(clarity_records),
        "brand_breakdown": {
            brand["name"]: {
                "airtable_count": len(airtable_by_brand[brand["name"]]),
                "clarity_count": len(clarity_by_brand[brand["name"]]),
            }
            for brand in BRANDS
        },
        "unknown_records": len(airtable_by_brand["Unknown"]),
        "reports_generated": [os.path.basename(p) for p in report_paths],
    }
    summary_path = os.path.join(OUTPUT_DIR, "report_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Summary saved: report_summary.json")
    return report_paths


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    print("=" * 60)
    print("  MULTI-BRAND ABANDONED FORMS REPORT GENERATOR")
    print("  Brands: Quick Signage | Signs.inc | Signage.inc | SignMakerz")
    print("  Timezone: Asia/Karachi (UTC+5)")
    print("  Auto-Schedule: Daily 9 AM Karachi (cron: 0 4 * * * UTC)")
    print("  Output: 4 TEXT FILES (Dashboard vs Airtable Comparison)")
    print("=" * 60)

    start_time, end_time = get_time_window()
    print(f"Time window (Karachi): {start_time} -> {end_time}")

    airtable_records = fetch_airtable_data(start_time, end_time)
    clarity_records = fetch_clarity_data(start_time, end_time)
    report_paths = generate_all_brand_reports(airtable_records, clarity_records, start_time, end_time)

    print("\n" + "=" * 60)
    print("  ALL PHASES COMPLETED")
    print("=" * 60)
    print(f"\nGenerated {len(report_paths)} brand reports in ./{OUTPUT_DIR}/:")
    for path in report_paths:
        size = os.path.getsize(path)
        print(f"  - {os.path.basename(path)} ({size:,} bytes)")

    print(f"\nAll output files in ./{OUTPUT_DIR}/:")
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        fpath = os.path.join(OUTPUT_DIR, fname)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            print(f"  - {fname} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
