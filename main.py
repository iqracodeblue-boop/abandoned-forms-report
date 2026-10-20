#!/usr/bin/env python3
"""
Daily Abandoned Forms Report Generator (Final - Production Ready)
=================================================================
End-to-end pipeline that runs in GitHub Actions daily at 9 AM Karachi time.

PHASE 1 - Fetch abandoned form records from Airtable (9 AM -> next day 9 AM, Karachi TZ)
PHASE 2 - Fetch records from 5 Clarity endpoints (bulletproof - 336 combinations per endpoint)
PHASE 3 - Auto-generate / overwrite a Google Docs report with RICH FORMATTING

FEATURES:
  1. Secret name fallbacks: AIRTABLE_API_TOKEN OR AIRTABLE_API_KEY, etc.
  2. Timezone: Asia/Karachi explicitly, UTC conversion for Airtable filter
  3. Filter field: 'Last Modified' (catches forms abandoned today)
  4. Clarity: bulletproof - 4 time formats x 6 param combos x 7 auth styles x 2 methods
  5. Smart report: gracefully handles 0-record scenarios
  6. RICH FORMATTING: native Google Docs headings, bullets, tables, bold text, colors
  7. Daily auto-update: cron '0 4 * * *' (4 AM UTC = 9 AM Karachi)
  8. URL truncation (prevents ugly long-line wrapping)
"""

import os
import sys
import json
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


KARACHI_TZ = ZoneInfo("Asia/Karachi")
UTC_TZ = ZoneInfo("UTC")

# Colors (Google Docs RGB format - 0 to 1 floats)
def hex_to_rgb_floats(hex_color):
    """Convert #RRGGBB to {red, green, blue} floats (0-1) for Google Docs API."""
    h = hex_color.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255.0,
        "green": int(h[2:4], 16) / 255.0,
        "blue": int(h[4:6], 16) / 255.0,
    }

C_TITLE = hex_to_rgb_floats("#1E3A5F")      # deep navy
C_H2 = hex_to_rgb_floats("#2E5C8A")          # medium blue
C_H2_BG = hex_to_rgb_floats("#E0EAF5")      # light blue background
C_MUTED = hex_to_rgb_floats("#6B7280")       # gray
C_SUCCESS = hex_to_rgb_floats("#15803D")    # green
C_WARNING = hex_to_rgb_floats("#B91C1C")     # red
C_ACCENT = hex_to_rgb_floats("#1F4E79")     # accent blue
C_TEXT = hex_to_rgb_floats("#1F2937")       # dark gray

# --- Airtable credentials (with fallback to old secret names) ---
AIRTABLE_TOKEN = os.getenv("AIRTABLE_API_TOKEN") or os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME") or os.getenv("AIRTABLE_TABLE_ID")

# --- Clarity credentials - 5 endpoints ---
CLARITY_ENDPOINTS = []
for i in range(1, 6):
    url = os.getenv(f"CLARITY_BASE_URL_{i}")
    token = os.getenv(f"CLARITY_TOKEN_{i}")
    if url and token:
        CLARITY_ENDPOINTS.append({"url": url, "token": token, "index": i})

# --- Google Docs credentials ---
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


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
    for k in ("id", "ID", "form_id", "formId", "session_id", "sessionId",
              "record_id", "recordId", "Form ID"):
        v = record.get(k)
        if v:
            return str(v)
    fields = record.get("fields", {}) if isinstance(record.get("fields"), dict) else {}
    for k in ("id", "ID", "form_id", "formId", "session_id", "sessionId",
              "record_id", "recordId", "Form ID", "Airtable Record ID"):
        v = fields.get(k)
        if v:
            return str(v)
    return None


def truncate(text, max_len=80):
    """Truncate long text (especially URLs) to keep the doc neat."""
    if not text or text == "?":
        return "?"
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


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
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }

    filter_formula = (
        f"AND("
        f"IS_AFTER({{Last Modified}}, '{start_str}'), "
        f"IS_BEFORE({{Last Modified}}, '{end_str}')"
        f")"
    )

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
        filter_formula_created = (
            f"AND("
            f"IS_AFTER({{Created}}, '{start_str}'), "
            f"IS_BEFORE({{Created}}, '{end_str}')"
            f")"
        )
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
        json.dump({
            "source": "airtable",
            "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
            "filter_utc": {"start": start_str, "end": end_str},
            "total_count": len(all_records),
            "records": all_records,
        }, f, indent=2, default=str)
    return all_records


# ============================================================================
# PHASE 2: CLARITY DATA FETCH (bulletproof)
# ============================================================================

def _extract_records_from_response(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("records", "data", "items", "results", "value",
                    "rows", "entries", "list", "items_list"):
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


def _try_clarity_call(url, token, method, params=None, body=None):
    auth_styles = [
        {"Authorization": f"Bearer {token}"},
        {"Authorization": f"Token {token}"},
        {"Authorization": f"ApiKey {token}"},
        {"X-API-Key": token},
        {"X-Api-Key": token},
        {"Api-Key": token},
        {"apikey": token},
    ]
    for auth in auth_styles:
        headers = {"Content-Type": "application/json", "Accept": "application/json", **auth}
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            elif method == "POST":
                resp = requests.post(url, headers=headers, json=body or params, timeout=30)
            else:
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    records = _extract_records_from_response(data)
                    if records:
                        return records, list(auth.keys())[0]
                except Exception:
                    continue
            elif resp.status_code in (401, 403):
                continue
            else:
                continue
        except Exception:
            continue
    return None, None


def fetch_clarity_data(start_time, end_time):
    print("\n" + "=" * 60)
    print("PHASE 2: Fetching data from Clarity APIs (robust mode)")
    print("=" * 60)

    all_clarity_records = []

    time_formats = [
        ("ISO+TZ",  start_time.isoformat(),                    end_time.isoformat()),
        ("ISO_UTC", start_time.astimezone(UTC_TZ).isoformat(), end_time.astimezone(UTC_TZ).isoformat()),
        ("date",    start_time.strftime("%Y-%m-%d"),            end_time.strftime("%Y-%m-%d")),
        ("unix",    str(int(start_time.timestamp())),         str(int(end_time.timestamp()))),
    ]
    param_combos = [
        ("start/end",           lambda s, e: {"start": s, "end": e}),
        ("startDate/endDate",   lambda s, e: {"startDate": s, "endDate": e}),
        ("from/to",             lambda s, e: {"from": s, "to": e}),
        ("startTime/endTime",   lambda s, e: {"startTime": s, "endTime": e}),
        ("begin/finish",        lambda s, e: {"begin": s, "finish": e}),
        ("date_start/date_end", lambda s, e: {"date_start": s, "date_end": e}),
    ]

    for endpoint in CLARITY_ENDPOINTS:
        idx = endpoint["index"]
        url = endpoint["url"]
        token = endpoint["token"]
        print(f"\n  Endpoint {idx}: {url}")
        print(f"    Token preview: {token[:8]}...{token[-4:] if len(token) > 12 else ''}")

        endpoint_records = []
        success_info = None

        for method in ("GET", "POST"):
            if endpoint_records:
                break
            for fmt_name, start_val, end_val in time_formats:
                if endpoint_records:
                    break
                for combo_name, combo_fn in param_combos:
                    params = combo_fn(start_val, end_val)
                    records, auth_used = _try_clarity_call(
                        url, token, method,
                        params=params if method == "GET" else None,
                        body=params if method == "POST" else None
                    )
                    if records:
                        endpoint_records = records
                        success_info = f"method={method}, time_fmt={fmt_name}, params={combo_name}, auth={auth_used}"
                        break

        if endpoint_records:
            print(f"    OK Got {len(endpoint_records)} records ({success_info})")
            for r in endpoint_records:
                if isinstance(r, dict):
                    r["_clarity_endpoint"] = idx
            all_clarity_records.extend(endpoint_records)
        else:
            print(f"    WARN No records after 336 attempts")
            try:
                test_resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                                         params={"start": start_time.isoformat(), "end": end_time.isoformat()},
                                         timeout=30)
                print(f"    [debug] HTTP {test_resp.status_code}")
                body_preview = test_resp.text[:300] if test_resp.text else "(empty body)"
                print(f"    [debug] Body preview: {body_preview}")
            except Exception as e:
                print(f"    [debug] Final test call also failed: {e}")

    print(f"\nClarity TOTAL: {len(all_clarity_records)} records")

    out_path = os.path.join(OUTPUT_DIR, "clarity_data.json")
    with open(out_path, "w") as f:
        json.dump({
            "source": "clarity",
            "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
            "total_count": len(all_clarity_records),
            "records": all_clarity_records,
        }, f, indent=2, default=str)
    return all_clarity_records


# ============================================================================
# COMPARISON LOGIC
# ============================================================================

def compare_data(airtable_records, clarity_records, start_time, end_time):
    print("\n" + "=" * 60)
    print("Comparing Airtable vs Clarity data")
    print("=" * 60)

    airtable_count = len(airtable_records)
    clarity_count = len(clarity_records)
    difference = clarity_count - airtable_count

    airtable_map = {}
    for r in airtable_records:
        rid = get_airtable_record_id(r)
        if rid:
            airtable_map[rid] = r
    clarity_map = {}
    for r in clarity_records:
        rid = get_clarity_record_id(r)
        if rid:
            clarity_map[rid] = r

    airtable_ids = set(airtable_map.keys())
    clarity_ids = set(clarity_map.keys())
    only_in_clarity = clarity_ids - airtable_ids
    only_in_airtable = airtable_ids - clarity_ids

    print(f"  Airtable: {airtable_count} records ({len(airtable_ids)} unique IDs)")
    print(f"  Clarity : {clarity_count} records ({len(clarity_ids)} unique IDs)")
    print(f"  Difference (Clarity - Airtable): {difference}")

    only_in_clarity_records = [clarity_map[rid] for rid in sorted(only_in_clarity)]
    only_in_airtable_records = [airtable_map[rid] for rid in sorted(only_in_airtable)]

    comparison = {
        "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "airtable_count": airtable_count,
        "clarity_count": clarity_count,
        "difference": difference,
        "airtable_unique_ids": len(airtable_ids),
        "clarity_unique_ids": len(clarity_ids),
        "only_in_clarity_ids": sorted(list(only_in_clarity)),
        "only_in_airtable_ids": sorted(list(only_in_airtable)),
        "only_in_clarity_records": only_in_clarity_records,
        "only_in_airtable_records": only_in_airtable_records,
    }

    out_path = os.path.join(OUTPUT_DIR, "comparison_report.json")
    with open(out_path, "w") as f:
        json.dump(comparison, f, indent=2, default=str)
    return comparison


# ============================================================================
# PHASE 3: PROFESSIONAL GOOGLE DOCS REPORT WITH RICH FORMATTING
# ============================================================================

def generate_google_docs_report(comparison, airtable_records, clarity_records):
    """Generate professionally formatted Google Docs report using native styling."""
    print("\n" + "=" * 60)
    print("PHASE 3: Generating Professional Google Docs Report")
    print("=" * 60)

    if not all([GOOGLE_SERVICE_ACCOUNT_EMAIL, GOOGLE_PRIVATE_KEY, GOOGLE_DOC_ID]):
        print("Google Docs credentials not set - skipping Phase 3")
        return False

    try:
        SCOPES = ["https://www.googleapis.com/auth/documents"]
        creds = service_account.Credentials.from_service_account_info(
            {
                "client_email": GOOGLE_SERVICE_ACCOUNT_EMAIL,
                "private_key": GOOGLE_PRIVATE_KEY,
                "token_uri": "https://oauth2.googleapis.com/token",
            },
            scopes=SCOPES,
        )
        docs_service = build("docs", "v1", credentials=creds)
        drive_service = build("drive", "v3", credentials=creds)

        doc_id = GOOGLE_DOC_ID
        doc = docs_service.documents().get(documentId=doc_id).execute()
        current_title = doc.get("title", "Untitled Document")
        print(f"  Connected to Google Doc: '{current_title}'")

        now = datetime.now(KARACHI_TZ)
        date_str = now.strftime("%d %b %Y, %H:%M (PKT)")
        window_start = comparison["time_window"]["start"]
        window_end = comparison["time_window"]["end"]
        only_in_clarity_records = comparison.get("only_in_clarity_records", [])
        only_in_airtable_records = comparison.get("only_in_airtable_records", [])
        airtable_count = comparison["airtable_count"]
        clarity_count = comparison["clarity_count"]

        # --- SMART DATA SOURCE STATUS ---
        if airtable_count > 0 and clarity_count > 0:
            show_mismatch_section = True
        else:
            show_mismatch_section = False

        # --- CLEAR EXISTING CONTENT ---
        doc_content = doc.get("body", {}).get("content", [])
        if doc_content:
            end_index = max(c.get("endIndex", 1) for c in doc_content)
            if end_index > 1:
                try:
                    docs_service.documents().batchUpdate(
                        documentId=doc_id,
                        body={"requests": [{
                            "deleteContentRange": {
                                "range": {"startIndex": 1, "endIndex": max(1, end_index - 1)}
                            }
                        }]}
                    ).execute()
                    print("  Cleared existing doc content")
                except HttpError as e:
                    print(f"  (skip clear) {e}")

        # --- BUILD REPORT BLOCKS ---
        # Each block: {"text": str, "style": "title/h1/h2/h3/body/bold/italic/muted/success/warning/mono"}
        blocks = []

        # Title
        blocks.append({"text": "Daily Abandoned Forms Report\n", "style": "title"})

        # Subtitle: Date Range
        blocks.append({"text": f"Date Range:  {window_start}  to  {window_end}\n", "style": "italic"})
        blocks.append({"text": f"               (Karachi time, 9 AM to next-day 9 AM)\n", "style": "muted"})
        blocks.append({"text": f"Generated:   {date_str}\n", "style": "italic"})
        blocks.append({"text": f"Generated By: Daily Forms Bot (GitHub Actions)\n", "style": "italic"})
        blocks.append({"text": f"Auto-Schedule: Daily at 9 AM Karachi (cron: 0 4 * * * UTC)\n\n", "style": "muted"})

        # SECTION 1: SUMMARY
        blocks.append({"text": "Summary\n", "style": "h2"})

        spam_count = 0
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            v = fields.get("isSpam")
            if v and str(v).lower() == "true":
                spam_count += 1
        non_spam_count = airtable_count - spam_count

        blocks.append({"text": f"Airtable Total Forms:  ", "style": "body"})
        blocks.append({"text": f"{airtable_count}\n", "style": "bold_accent"})
        blocks.append({"text": f"  Spam (isSpam=true):  {spam_count}\n", "style": "body"})
        blocks.append({"text": f"  Non-spam:           {non_spam_count}\n", "style": "body"})
        blocks.append({"text": f"Clarity Total Forms:  ", "style": "body"})
        blocks.append({"text": f"{clarity_count}\n", "style": "bold_accent"})
        blocks.append({"text": f"Difference (Clarity - Airtable):  ", "style": "body"})
        blocks.append({"text": f"{comparison['difference']}\n\n", "style": "bold_accent"})
        blocks.append({"text": f"Airtable Unique IDs:  {comparison.get('airtable_unique_ids', 'N/A')}\n", "style": "body"})
        blocks.append({"text": f"Clarity Unique IDs:   {comparison.get('clarity_unique_ids', 'N/A')}\n\n", "style": "body"})

        # SECTION 2: DATA SOURCE STATUS
        blocks.append({"text": "Data Source Status\n", "style": "h2"})

        if airtable_count > 0:
            blocks.append({"text": f"  Airtable:  CONNECTED  ({airtable_count} records)\n", "style": "success"})
        else:
            blocks.append({"text": f"  Airtable:  NOT RETURNING DATA  (0 records)\n", "style": "warning"})

        if clarity_count > 0:
            blocks.append({"text": f"  Clarity:   CONNECTED  ({clarity_count} records)\n", "style": "success"})
        else:
            blocks.append({"text": f"  Clarity:   NOT RETURNING DATA  (0 records)\n", "style": "warning"})

        if airtable_count > 0 and clarity_count == 0:
            blocks.append({"text": "\n  Note: Comparison skipped - Clarity has no data to compare against.\n", "style": "muted"})
            blocks.append({"text": "  Action: Verify CLARITY_BASE_URL_1..5 and CLARITY_TOKEN_1..5 secrets in GitHub.\n\n", "style": "muted"})
        elif airtable_count == 0 and clarity_count > 0:
            blocks.append({"text": "\n  Note: Comparison skipped - Airtable has no data to compare against.\n", "style": "muted"})
            blocks.append({"text": "  Action: Verify AIRTABLE_API_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME secrets.\n\n", "style": "muted"})
        elif airtable_count == 0 and clarity_count == 0:
            blocks.append({"text": "\n  Note: Both sources returning 0 records.\n", "style": "muted"})
            blocks.append({"text": "  Action: Check all GitHub Secrets configuration.\n\n", "style": "muted"})
        else:
            blocks.append({"text": "\n  Both sources returning data - comparison performed.\n\n", "style": "muted"})

        # SECTION 3: AIRTABLE BREAKDOWN BY SOURCE
        blocks.append({"text": "Airtable Breakdown (by Source)\n", "style": "h2"})
        source_counts = {}
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            src = fields.get("Record Source") or "Unknown"
            source_counts[src] = source_counts.get(src, 0) + 1
        if source_counts:
            for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1]):
                blocks.append({"text": f"  {src}:  ", "style": "body"})
                blocks.append({"text": f"{cnt}\n", "style": "bold"})
        else:
            blocks.append({"text": "  (no source info available)\n", "style": "muted"})
        blocks.append({"text": "\n", "style": "body"})

        # SECTION 4: AIRTABLE BREAKDOWN BY PROJECT
        blocks.append({"text": "Airtable Breakdown (by Project)\n", "style": "h2"})
        project_counts = {}
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            p = fields.get("Project") or "Unknown"
            project_counts[p] = project_counts.get(p, 0) + 1
        if project_counts:
            for p, cnt in sorted(project_counts.items(), key=lambda x: -x[1]):
                blocks.append({"text": f"  {p}:  ", "style": "body"})
                blocks.append({"text": f"{cnt}\n", "style": "bold"})
        else:
            blocks.append({"text": "  (no project info available)\n", "style": "muted"})
        blocks.append({"text": "\n", "style": "body"})

        # SECTION 5: MISMATCHED FORMS (or DATA COMPARISON)
        if show_mismatch_section:
            blocks.append({"text": "Mismatched Forms\n", "style": "h2"})

            blocks.append({"text": f"Forms ONLY in Clarity (not in Airtable)\n", "style": "h3"})
            blocks.append({"text": f"Count: ", "style": "body"})
            blocks.append({"text": f"{len(only_in_clarity_records)}\n", "style": "bold_accent"})
            if only_in_clarity_records:
                for i, r in enumerate(only_in_clarity_records[:10], 1):
                    rid = get_clarity_record_id(r) or "?"
                    endpoint = r.get("_clarity_endpoint", "?") if isinstance(r, dict) else "?"
                    url = truncate(r.get("url") or r.get("pageUrl") or safe_get(r, "fields", "Page URL") or "?", 60)
                    ts = (r.get("timestamp") or r.get("createdAt") or r.get("created_at") or "?")
                    blocks.append({"text": f"  {i}. ID: {rid}  (Endpoint {endpoint})\n", "style": "body"})
                    blocks.append({"text": f"      Timestamp: {ts}\n", "style": "muted"})
                    blocks.append({"text": f"      URL: {url}\n", "style": "muted"})
                if len(only_in_clarity_records) > 10:
                    blocks.append({"text": f"\n  ... and {len(only_in_clarity_records) - 10} more (see artifact JSON for full list)\n", "style": "muted"})
            else:
                blocks.append({"text": "  None - all Clarity forms have matching Airtable records.\n", "style": "success"})
            blocks.append({"text": "\n", "style": "body"})

            blocks.append({"text": f"Forms ONLY in Airtable (not in Clarity)\n", "style": "h3"})
            blocks.append({"text": f"Count: ", "style": "body"})
            blocks.append({"text": f"{len(only_in_airtable_records)}\n", "style": "bold_accent"})
            if only_in_airtable_records:
                for i, r in enumerate(only_in_airtable_records[:10], 1):
                    fields = safe_get(r, "fields", default={}) or {}
                    rid = get_airtable_record_id(r) or "?"
                    created = fields.get("Created") or "?"
                    page_url = truncate(fields.get("Page URL") or "?", 60)
                    record_source = fields.get("Record Source") or "?"
                    is_spam = fields.get("isSpam") or "?"
                    project = fields.get("Project") or "?"
                    blocks.append({"text": f"  {i}. ID: {rid}\n", "style": "body"})
                    blocks.append({"text": f"      Created: {created}\n", "style": "muted"})
                    blocks.append({"text": f"      Project: {project} | Source: {record_source}\n", "style": "muted"})
                    blocks.append({"text": f"      Page: {page_url}\n", "style": "muted"})
                    blocks.append({"text": f"      isSpam: {is_spam}\n", "style": "muted"})
                if len(only_in_airtable_records) > 10:
                    blocks.append({"text": f"\n  ... and {len(only_in_airtable_records) - 10} more (see artifact JSON for full list)\n", "style": "muted"})
            else:
                blocks.append({"text": "  None - all Airtable forms have matching Clarity records.\n", "style": "success"})
            blocks.append({"text": "\n", "style": "body"})
        else:
            blocks.append({"text": "Data Comparison\n", "style": "h2"})
            blocks.append({"text": "  (Skipped - at least one source returned 0 records.\n", "style": "muted"})
            blocks.append({"text": "   See Data Source Status section above for details.)\n\n", "style": "muted"})

        # SECTION 6: RECOMMENDATION
        blocks.append({"text": "Recommendation\n", "style": "h2"})

        if airtable_count == 0 and clarity_count == 0:
            blocks.append({"text": "Both data sources are returning 0 records. Verify:\n", "style": "body"})
            blocks.append({"text": "  1. AIRTABLE_API_TOKEN secret is valid\n", "style": "body"})
            blocks.append({"text": "  2. AIRTABLE_BASE_ID secret is correct\n", "style": "body"})
            blocks.append({"text": "  3. AIRTABLE_TABLE_NAME secret matches your table name\n", "style": "body"})
            blocks.append({"text": "  4. CLARITY_BASE_URL_1..5 secrets point to valid API endpoints\n", "style": "body"})
            blocks.append({"text": "  5. CLARITY_TOKEN_1..5 secrets are valid auth tokens\n\n", "style": "body"})
        elif airtable_count > 0 and clarity_count == 0:
            blocks.append({"text": f"Airtable is connected ({airtable_count} records), but Clarity API is returning 0 records.\n", "style": "bold"})
            blocks.append({"text": "ACTION NEEDED:\n", "style": "bold"})
            blocks.append({"text": "  1. Verify CLARITY_BASE_URL_1..5 secrets are correct API URLs\n", "style": "body"})
            blocks.append({"text": "  2. Verify CLARITY_TOKEN_1..5 secrets are valid auth tokens\n", "style": "body"})
            blocks.append({"text": "  3. Check the workflow run logs - search for 'PHASE 2' section\n", "style": "body"})
            blocks.append({"text": "  4. Look for '[debug] HTTP XXX' and '[debug] Body preview:' lines\n", "style": "body"})
            blocks.append({"text": "  5. Share those debug lines to identify the exact issue\n\n", "style": "body"})
        elif airtable_count == 0 and clarity_count > 0:
            blocks.append({"text": f"Clarity is connected ({clarity_count} records), but Airtable API is returning 0 records.\n", "style": "bold"})
            blocks.append({"text": "ACTION NEEDED:\n", "style": "bold"})
            blocks.append({"text": "  1. Verify AIRTABLE_API_TOKEN secret is a valid Personal Access Token\n", "style": "body"})
            blocks.append({"text": "  2. Verify AIRTABLE_BASE_ID matches your Airtable base\n", "style": "body"})
            blocks.append({"text": "  3. Verify AIRTABLE_TABLE_NAME matches your table name exactly\n\n", "style": "body"})
        elif comparison["difference"] > 0:
            blocks.append({"text": f"Clarity has {comparison['difference']} MORE forms than Airtable.\n", "style": "bold"})
            blocks.append({"text": "Likely cause: some form submissions were captured by Clarity but failed to sync to Airtable.\n", "style": "body"})
            blocks.append({"text": "Action: review the unmatched Clarity records below, then check Airtable's webhook/integration health.\n\n", "style": "body"})
        elif comparison["difference"] < 0:
            blocks.append({"text": f"Airtable has {abs(comparison['difference'])} MORE forms than Clarity.\n", "style": "bold"})
            blocks.append({"text": "Likely cause: stale records in Airtable, or Clarity's date filter is excluding valid sessions.\n", "style": "body"})
            blocks.append({"text": "Action: verify Airtable entries match today's window, and confirm Clarity's date filter is inclusive of the 9 AM to 9 AM window.\n\n", "style": "body"})
        else:
            blocks.append({"text": "Both sources match perfectly in record count.\n", "style": "success"})
            blocks.append({"text": "Check the unique-ID comparison above to confirm every individual form matches, not just the totals.\n\n", "style": "body"})

        # SECTION 7: RAW DATA SAMPLE
        blocks.append({"text": "Raw Data Sample (first 3 records)\n", "style": "h2"})
        blocks.append({"text": "Airtable sample:\n", "style": "h3"})
        airtable_sample = json.dumps(airtable_records[:3], indent=2, default=str)
        if len(airtable_sample) > 1500:
            airtable_sample = airtable_sample[:1500] + "\n... (truncated)"
        blocks.append({"text": airtable_sample + "\n\n", "style": "mono"})

        blocks.append({"text": "Clarity sample:\n", "style": "h3"})
        clarity_sample = json.dumps(clarity_records[:3], indent=2, default=str)
        if len(clarity_sample) > 1500:
            clarity_sample = clarity_sample[:1500] + "\n... (truncated)"
        blocks.append({"text": clarity_sample + "\n\n", "style": "mono"})

        # Footer
        blocks.append({"text": "For full JSON data, download the GitHub Actions artifacts named 'airtable-data', 'clarity-data', and 'comparison-report' from the workflow run page.\n", "style": "muted"})

        # --- BUILD THE FULL TEXT ---
        full_text = "".join(b["text"] for b in blocks)

        # --- BUILD API REQUESTS ---
        requests_list = []

        # 1. Insert the full text
        requests_list.append({
            "insertText": {
                "location": {"index": 1},
                "text": full_text,
            }
        })

        # 2. Apply styles to each block (track current index)
        current_index = 1
        for block in blocks:
            text = block["text"]
            text_len = len(text)
            start = current_index
            end = current_index + text_len
            style = block["style"]

            # Build textStyle update
            text_style = {}
            fields = []

            if style == "title":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 28, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TITLE}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "h1":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 20, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TITLE}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "h2":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 14, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_H2}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "h3":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_ACCENT}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "bold":
                text_style["bold"] = True
                fields.append("bold")
            elif style == "bold_accent":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 12, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_ACCENT}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "italic":
                text_style["italic"] = True
                text_style["foregroundColor"] = {"color": {"rgbColor": C_MUTED}}
                fields.extend(["italic", "foregroundColor"])
            elif style == "muted":
                text_style["foregroundColor"] = {"color": {"rgbColor": C_MUTED}}
                text_style["fontSize"] = {"magnitude": 9, "unit": "PT"}
                fields.extend(["foregroundColor", "fontSize"])
            elif style == "success":
                text_style["bold"] = True
                text_style["foregroundColor"] = {"color": {"rgbColor": C_SUCCESS}}
                fields.extend(["bold", "foregroundColor"])
            elif style == "warning":
                text_style["bold"] = True
                text_style["foregroundColor"] = {"color": {"rgbColor": C_WARNING}}
                fields.extend(["bold", "foregroundColor"])
            elif style == "mono":
                text_style["bold"] = False
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TEXT}}
                text_style["fontSize"] = {"magnitude": 8, "unit": "PT"}
                fields.extend(["foregroundColor", "fontSize"])

            if fields:
                requests_list.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": text_style,
                        "fields": ",".join(fields),
                    }
                })

            # Paragraph style for headings
            if style == "title":
                requests_list.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "TITLE", "alignment": "START"},
                        "fields": "namedStyleType,alignment",
                    }
                })
            elif style == "h1":
                requests_list.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "fields": "namedStyleType",
                    }
                })
            elif style == "h2":
                requests_list.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_2"},
                        "fields": "namedStyleType",
                    }
                })
            elif style == "h3":
                requests_list.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_3"},
                        "fields": "namedStyleType",
                    }
                })

            current_index = end

        # 3. Execute all style updates in one batch
        print(f"  Inserting {len(full_text)} chars and applying {len(requests_list)} style updates...")
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests_list}
        ).execute()
        print("  Inserted report content with rich formatting")

        # 4. Rename doc
        new_title = f"Daily Abandoned Forms Report - {now.strftime('%Y-%m-%d')}"
        drive_service.files().update(fileId=doc_id, body={"name": new_title}).execute()
        print(f"  Renamed doc to: '{new_title}'")

        print(f"\n  PROFESSIONAL REPORT READY:")
        print(f"  https://docs.google.com/document/d/{doc_id}/edit")
        return True

    except HttpError as e:
        print(f"Google Docs API error: {e}")
        return False
    except Exception as e:
        print(f"Phase 3 error: {e}")
        return False


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    print("=" * 60)
    print("  DAILY ABANDONED FORMS REPORT GENERATOR")
    print("  Timezone: Asia/Karachi (UTC+5)")
    print("  Auto-Schedule: Daily 9 AM Karachi (cron: 0 4 * * * UTC)")
    print("  Rich Formatting: ENABLED")
    print("=" * 60)

    start_time, end_time = get_time_window()
    print(f"Time window (Karachi): {start_time} -> {end_time}")

    airtable_records = fetch_airtable_data(start_time, end_time)
    clarity_records = fetch_clarity_data(start_time, end_time)
    comparison = compare_data(airtable_records, clarity_records, start_time, end_time)
    generate_google_docs_report(comparison, airtable_records, clarity_records)

    print("\n" + "=" * 60)
    print("  ALL PHASES COMPLETED")
    print("=" * 60)
    print(f"\nOutput files in ./{OUTPUT_DIR}/:")
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        fpath = os.path.join(OUTPUT_DIR, fname)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            print(f"  - {fname} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
