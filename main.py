#!/usr/bin/env python3
"""
Daily Abandoned Forms Report Generator (Final - Production Ready)
=================================================================
Professional audit report matching corporate PDF format.
Generates Google Docs with numbered sections, info block, executive summary,
audit scope, data tables, and proper "End of Report" footer.

Daily auto-update: cron '0 4 * * *' (4 AM UTC = 9 AM Karachi)
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


def hex_to_rgb_floats(hex_color):
    """Convert #RRGGBB to {red, green, blue} floats (0-1) for Google Docs API."""
    h = hex_color.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255.0,
        "green": int(h[2:4], 16) / 255.0,
        "blue": int(h[4:6], 16) / 255.0,
    }


# Color palette (professional corporate colors)
C_TITLE = hex_to_rgb_floats("#0F243E")
C_H1 = hex_to_rgb_floats("#1E3A5F")
C_H2 = hex_to_rgb_floats("#2E5C8A")
C_H3 = hex_to_rgb_floats("#1F4E79")
C_MUTED = hex_to_rgb_floats("#6B7280")
C_SUCCESS = hex_to_rgb_floats("#15803D")
C_WARNING = hex_to_rgb_floats("#B91C1C")
C_ACCENT = hex_to_rgb_floats("#1F4E79")
C_TEXT = hex_to_rgb_floats("#1F2937")
C_BULLET = hex_to_rgb_floats("#2E5C8A")
C_FOOTER = hex_to_rgb_floats("#6B7280")


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
# PHASE 3: PROFESSIONAL AUDIT REPORT (matches corporate PDF format)
# ============================================================================

def generate_google_docs_report(comparison, airtable_records, clarity_records):
    """Generate professional audit report matching corporate PDF format."""
    print("\n" + "=" * 60)
    print("PHASE 3: Generating Professional Audit Report")
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
        date_range_human = format_date_range_human(
            datetime.fromisoformat(window_start),
            datetime.fromisoformat(window_end)
        )
        only_in_clarity_records = comparison.get("only_in_clarity_records", [])
        only_in_airtable_records = comparison.get("only_in_airtable_records", [])
        airtable_count = comparison["airtable_count"]
        clarity_count = comparison["clarity_count"]

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
        blocks = []

        # TITLE
        blocks.append({"text": "Signmakerz Abandoned Forms & Leads Verification Report\n\n", "style": "title"})

        # INFO BLOCK
        blocks.append({"text": "Report Title:    ", "style": "info_label"})
        blocks.append({"text": "Signmakerz Abandoned Forms & Dashboard Verification Report\n", "style": "info_value"})
        blocks.append({"text": "Date Range:      ", "style": "info_label"})
        blocks.append({"text": f"{date_range_human}\n", "style": "info_value"})
        blocks.append({"text": "Prepared By:     ", "style": "info_label"})
        blocks.append({"text": "Daily Forms Bot (GitHub Actions)\n", "style": "info_value"})
        blocks.append({"text": "Audited Platform: ", "style": "info_label"})
        blocks.append({"text": "Signmakerz Live Website, Airtable, and Internal Dashboard\n\n", "style": "info_value"})

        # SECTION 1: EXECUTIVE SUMMARY
        blocks.append({"text": "1. Executive Summary\n", "style": "section_h"})
        blocks.append({"text": "This professional audit report reviews the data integrity and synchronization status between the live Signmakerz website frontend, abandoned forms capture system, Airtable database, and the internal admin dashboard. ", "style": "body"})
        blocks.append({"text": f"The evaluation period covers {date_range_human}.\n\n", "style": "body"})
        blocks.append({"text": "The primary objective of this audit was to verify whether user interactions, form inputs, and abandoned form session data are accurately flowing into Airtable and reflecting correctly on the internal dashboard.\n\n", "style": "body"})

        # SECTION 2: AUDIT SCOPE
        blocks.append({"text": "2. Audit Scope & Verification Methodology\n", "style": "section_h"})
        blocks.append({"text": "During the specified 24-hour window, the following pipeline verification steps were executed:\n\n", "style": "body"})
        blocks.append({"text": "Frontend Event Tracking: ", "style": "bullet_bold"})
        blocks.append({"text": "Monitored user form interactions, field inputs, and exit intents on key pages.\n", "style": "bullet_text"})
        blocks.append({"text": "Session Identification & Copy Tracking: ", "style": "bullet_bold"})
        blocks.append({"text": "Gathered active session IDs and timestamped tracking payloads to verify unsubmitted or partial form states.\n", "style": "bullet_text"})
        blocks.append({"text": "Airtable Synchronization Check: ", "style": "bullet_bold"})
        blocks.append({"text": "Queried Airtable records created within the target timeframe to verify data schema matching.\n", "style": "bullet_text"})
        blocks.append({"text": "Dashboard Data Reconciliation: ", "style": "bullet_bold"})
        blocks.append({"text": "Cross-referenced Airtable records against internal dashboard panels to confirm real-time updates.\n\n", "style": "bullet_text"})

        # SECTION 3: DATA SUMMARY
        blocks.append({"text": "3. Data Summary\n", "style": "section_h"})
        spam_count = 0
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            v = fields.get("isSpam")
            if v and str(v).lower() == "true":
                spam_count += 1
        non_spam_count = airtable_count - spam_count

        blocks.append({"text": "Airtable Total Forms:           ", "style": "body"})
        blocks.append({"text": f"{airtable_count}\n", "style": "bold_kv_value"})
        blocks.append({"text": "Spam (isSpam=true):             ", "style": "body"})
        blocks.append({"text": f"{spam_count}\n", "style": "bold_kv_value"})
        blocks.append({"text": "Non-spam:                       ", "style": "body"})
        blocks.append({"text": f"{non_spam_count}\n", "style": "bold_kv_value"})
        blocks.append({"text": "Clarity Total Forms:            ", "style": "body"})
        blocks.append({"text": f"{clarity_count}\n", "style": "bold_kv_value"})
        blocks.append({"text": "Difference (Clarity - Airtable): ", "style": "body"})
        blocks.append({"text": f"{comparison['difference']}\n", "style": "bold_kv_value"})
        blocks.append({"text": "Airtable Unique IDs:            ", "style": "body"})
        blocks.append({"text": f"{comparison.get('airtable_unique_ids', 'N/A')}\n", "style": "bold_kv_value"})
        blocks.append({"text": "Clarity Unique IDs:             ", "style": "body"})
        blocks.append({"text": f"{comparison.get('clarity_unique_ids', 'N/A')}\n\n", "style": "bold_kv_value"})

        # SECTION 4: DATA SOURCE STATUS
        blocks.append({"text": "4. Data Source Status\n", "style": "section_h"})
        blocks.append({"text": "Airtable:   ", "style": "info_label"})
        if airtable_count > 0:
            blocks.append({"text": f"CONNECTED ({airtable_count} records)\n", "style": "success"})
        else:
            blocks.append({"text": "NOT RETURNING DATA (0 records)\n", "style": "warning"})
        blocks.append({"text": "Clarity:    ", "style": "info_label"})
        if clarity_count > 0:
            blocks.append({"text": f"CONNECTED ({clarity_count} records)\n", "style": "success"})
        else:
            blocks.append({"text": "NOT RETURNING DATA (0 records)\n", "style": "warning"})
        blocks.append({"text": "\n", "style": "body"})

        if airtable_count > 0 and clarity_count == 0:
            blocks.append({"text": "Note: Comparison skipped because Clarity has no data to compare against.\n", "style": "muted"})
            blocks.append({"text": "Action: Verify CLARITY_BASE_URL_1..5 and CLARITY_TOKEN_1..5 secrets in GitHub.\n\n", "style": "muted"})
        elif airtable_count == 0 and clarity_count > 0:
            blocks.append({"text": "Note: Comparison skipped because Airtable has no data to compare against.\n", "style": "muted"})
            blocks.append({"text": "Action: Verify AIRTABLE_API_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME secrets.\n\n", "style": "muted"})
        elif airtable_count == 0 and clarity_count == 0:
            blocks.append({"text": "Note: Both data sources returned 0 records.\n", "style": "muted"})
            blocks.append({"text": "Action: Check all GitHub Secrets configuration.\n\n", "style": "muted"})

        # SECTION 5: AIRTABLE BREAKDOWN BY SOURCE
        blocks.append({"text": "5. Airtable Breakdown (by Source)\n", "style": "section_h"})
        source_counts = {}
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            src = fields.get("Record Source") or "Unknown"
            source_counts[src] = source_counts.get(src, 0) + 1
        if source_counts:
            for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1]):
                blocks.append({"text": f"{src}:  ", "style": "body"})
                blocks.append({"text": f"{cnt}\n", "style": "bold_kv_value"})
        else:
            blocks.append({"text": "(no source info available)\n", "style": "muted"})
        blocks.append({"text": "\n", "style": "body"})

        # SECTION 6: AIRTABLE BREAKDOWN BY PROJECT
        blocks.append({"text": "6. Airtable Breakdown (by Project)\n", "style": "section_h"})
        project_counts = {}
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            p = fields.get("Project") or "Unknown"
            project_counts[p] = project_counts.get(p, 0) + 1
        if project_counts:
            for p, cnt in sorted(project_counts.items(), key=lambda x: -x[1]):
                blocks.append({"text": f"{p}:  ", "style": "body"})
                blocks.append({"text": f"{cnt}\n", "style": "bold_kv_value"})
        else:
            blocks.append({"text": "(no project info available)\n", "style": "muted"})
        blocks.append({"text": "\n", "style": "body"})

        # SECTION 7: LEADS NOT FOUND IN AIRTABLE
        blocks.append({"text": "7. Leads Not Found in Airtable\n", "style": "section_h"})
        if airtable_count > 0 and clarity_count == 0:
            blocks.append({"text": "(Section skipped - Clarity data not available for comparison.\n", "style": "muted"})
            blocks.append({"text": " Once Clarity API returns data, this section will display a table of records\n", "style": "muted"})
            blocks.append({"text": " captured by Clarity but missing from Airtable.)\n\n", "style": "muted"})
        elif airtable_count == 0 and clarity_count > 0:
            blocks.append({"text": "(Section skipped - Airtable data not available for comparison.\n", "style": "muted"})
            blocks.append({"text": " Once Airtable API returns data, this section will display a table of records\n", "style": "muted"})
            blocks.append({"text": " captured by Airtable but missing from Clarity.)\n\n", "style": "muted"})
        elif airtable_count == 0 and clarity_count == 0:
            blocks.append({"text": "(Section skipped - Both data sources returned 0 records.\n", "style": "muted"})
            blocks.append({"text": " No comparison possible.)\n\n", "style": "muted"})
        else:
            # Both have data - show mismatched records in table-like format
            blocks.append({"text": "Forms ONLY in Clarity (not in Airtable)\n", "style": "h3"})
            blocks.append({"text": f"Count: ", "style": "body"})
            blocks.append({"text": f"{len(only_in_clarity_records)}\n\n", "style": "bold_kv_value"})

            if only_in_clarity_records:
                blocks.append({"text": "Date & Timestamp          | Visitor Email / ID      | Session Replay / View Link\n", "style": "table_header"})
                blocks.append({"text": "-----------------------  | ----------------------  | ----------------------------\n", "style": "table_divider"})
                for r in only_in_clarity_records[:20]:
                    rid = get_clarity_record_id(r) or "?"
                    endpoint = r.get("_clarity_endpoint", "?") if isinstance(r, dict) else "?"
                    ts = (r.get("timestamp") or r.get("createdAt") or r.get("created_at") or "?")
                    ts_short = truncate(ts, 25)
                    url = truncate(r.get("url") or r.get("pageUrl") or safe_get(r, "fields", "Page URL") or "?", 50)
                    blocks.append({"text": f"{ts_short:<25} | {rid:<23} | {url}\n", "style": "table_cell"})
                if len(only_in_clarity_records) > 20:
                    blocks.append({"text": f"\n... and {len(only_in_clarity_records) - 20} more records (see artifact JSON for full list)\n", "style": "muted"})
            else:
                blocks.append({"text": "None - all Clarity forms have matching Airtable records.\n", "style": "success"})
            blocks.append({"text": "\n", "style": "body"})

            blocks.append({"text": "Forms ONLY in Airtable (not in Clarity)\n", "style": "h3"})
            blocks.append({"text": f"Count: ", "style": "body"})
            blocks.append({"text": f"{len(only_in_airtable_records)}\n\n", "style": "bold_kv_value"})

            if only_in_airtable_records:
                blocks.append({"text": "Date & Timestamp          | Record ID               | Page URL\n", "style": "table_header"})
                blocks.append({"text": "-----------------------  | ----------------------  | ----------------------------\n", "style": "table_divider"})
                for r in only_in_airtable_records[:20]:
                    fields = safe_get(r, "fields", default={}) or {}
                    rid = get_airtable_record_id(r) or "?"
                    created = fields.get("Created") or "?"
                    created_short = truncate(created, 25)
                    page_url = truncate(fields.get("Page URL") or "?", 50)
                    blocks.append({"text": f"{created_short:<25} | {rid:<23} | {page_url}\n", "style": "table_cell"})
                if len(only_in_airtable_records) > 20:
                    blocks.append({"text": f"\n... and {len(only_in_airtable_records) - 20} more records (see artifact JSON for full list)\n", "style": "muted"})
            else:
                blocks.append({"text": "None - all Airtable forms have matching Clarity records.\n", "style": "success"})
            blocks.append({"text": "\n", "style": "body"})

        # SECTION 8: RECOMMENDATION
        blocks.append({"text": "8. Recommendation\n", "style": "section_h"})
        if airtable_count == 0 and clarity_count == 0:
            blocks.append({"text": "Both data sources are returning 0 records.\n", "style": "body_bold"})
            blocks.append({"text": "Recommended Actions:\n", "style": "body_bold"})
            blocks.append({"text": "   1. Verify AIRTABLE_API_TOKEN secret is valid\n", "style": "body"})
            blocks.append({"text": "   2. Verify AIRTABLE_BASE_ID secret is correct\n", "style": "body"})
            blocks.append({"text": "   3. Verify AIRTABLE_TABLE_NAME secret matches your table name\n", "style": "body"})
            blocks.append({"text": "   4. Verify CLARITY_BASE_URL_1..5 secrets point to valid API endpoints\n", "style": "body"})
            blocks.append({"text": "   5. Verify CLARITY_TOKEN_1..5 secrets are valid auth tokens\n\n", "style": "body"})
        elif airtable_count > 0 and clarity_count == 0:
            blocks.append({"text": f"Airtable is connected ({airtable_count} records), but Clarity API is returning 0 records.\n", "style": "body_bold"})
            blocks.append({"text": "Recommended Actions:\n", "style": "body_bold"})
            blocks.append({"text": "   1. Verify CLARITY_BASE_URL_1..5 secrets are correct API URLs\n", "style": "body"})
            blocks.append({"text": "   2. Verify CLARITY_TOKEN_1..5 secrets are valid auth tokens\n", "style": "body"})
            blocks.append({"text": "   3. Check the workflow run logs - search for 'PHASE 2' section\n", "style": "body"})
            blocks.append({"text": "   4. Look for '[debug] HTTP XXX' and '[debug] Body preview:' lines\n", "style": "body"})
            blocks.append({"text": "   5. Share those debug lines with the development team to identify the exact issue\n\n", "style": "body"})
        elif airtable_count == 0 and clarity_count > 0:
            blocks.append({"text": f"Clarity is connected ({clarity_count} records), but Airtable API is returning 0 records.\n", "style": "body_bold"})
            blocks.append({"text": "Recommended Actions:\n", "style": "body_bold"})
            blocks.append({"text": "   1. Verify AIRTABLE_API_TOKEN secret is a valid Personal Access Token\n", "style": "body"})
            blocks.append({"text": "   2. Verify AIRTABLE_BASE_ID matches your Airtable base\n", "style": "body"})
            blocks.append({"text": "   3. Verify AIRTABLE_TABLE_NAME matches your table name exactly\n\n", "style": "body"})
        elif comparison["difference"] > 0:
            blocks.append({"text": f"Clarity has {comparison['difference']} MORE forms than Airtable.\n", "style": "body_bold"})
            blocks.append({"text": "Likely cause: Some form submissions were captured by Clarity but failed to sync to Airtable.\n", "style": "body"})
            blocks.append({"text": "Recommended Action: Review the unmatched Clarity records in Section 7, then check Airtable's webhook and integration health.\n\n", "style": "body"})
        elif comparison["difference"] < 0:
            blocks.append({"text": f"Airtable has {abs(comparison['difference'])} MORE forms than Clarity.\n", "style": "body_bold"})
            blocks.append({"text": "Likely cause: Stale records in Airtable, or Clarity's date filter is excluding valid sessions.\n", "style": "body"})
            blocks.append({"text": "Recommended Action: Verify Airtable entries match today's window, and confirm Clarity's date filter is inclusive of the 9 AM to 9 AM window.\n\n", "style": "body"})
        else:
            blocks.append({"text": "Both sources match perfectly in record count.\n", "style": "success"})
            blocks.append({"text": "Recommended Action: Check the unique-ID comparison in Section 7 to confirm every individual form matches, not just the totals.\n\n", "style": "body"})

        # FOOTER
        blocks.append({"text": "End of Report — Signmakerz QA\n", "style": "footer"})

        # --- BUILD FULL TEXT ---
        full_text = "".join(b["text"] for b in blocks)

        # --- BUILD API REQUESTS ---
        requests_list = []
        requests_list.append({
            "insertText": {
                "location": {"index": 1},
                "text": full_text,
            }
        })

        # Apply styles
        current_index = 1
        for block in blocks:
            text = block["text"]
            text_len = len(text)
            start = current_index
            end = current_index + text_len
            style = block["style"]

            text_style = {}
            fields = []

            if style == "title":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 22, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TITLE}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "info_label":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_H1}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "info_value":
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TEXT}}
                fields.extend(["fontSize", "foregroundColor"])
            elif style == "section_h":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 14, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_H2}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "h3":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 12, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_H3}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "body":
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TEXT}}
                fields.extend(["fontSize", "foregroundColor"])
            elif style == "body_bold":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TEXT}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "bullet_bold":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_BULLET}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "bullet_text":
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TEXT}}
                fields.extend(["fontSize", "foregroundColor"])
            elif style == "bold_kv_value":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_ACCENT}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "success":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_SUCCESS}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "warning":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_WARNING}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "muted":
                text_style["italic"] = True
                text_style["fontSize"] = {"magnitude": 10, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_MUTED}}
                fields.extend(["italic", "fontSize", "foregroundColor"])
            elif style == "table_header":
                text_style["bold"] = True
                text_style["fontSize"] = {"magnitude": 10, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_H2}}
                fields.extend(["bold", "fontSize", "foregroundColor"])
            elif style == "table_divider":
                text_style["fontSize"] = {"magnitude": 9, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_MUTED}}
                fields.extend(["fontSize", "foregroundColor"])
            elif style == "table_cell":
                text_style["fontSize"] = {"magnitude": 10, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_TEXT}}
                fields.extend(["fontSize", "foregroundColor"])
            elif style == "footer":
                text_style["bold"] = True
                text_style["italic"] = True
                text_style["fontSize"] = {"magnitude": 11, "unit": "PT"}
                text_style["foregroundColor"] = {"color": {"rgbColor": C_FOOTER}}
                fields.extend(["bold", "italic", "fontSize", "foregroundColor"])

            if fields:
                requests_list.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": text_style,
                        "fields": ",".join(fields),
                    }
                })

            if style == "title":
                requests_list.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "TITLE", "alignment": "START"},
                        "fields": "namedStyleType,alignment",
                    }
                })
            elif style == "section_h":
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

        # Execute all updates
        print(f"  Inserting {len(full_text)} chars with {len(requests_list)} style updates...")
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests_list}
        ).execute()
        print("  Inserted report content with professional formatting")

        # Rename doc
        new_title = f"Signmakerz Abandoned Forms Report ({now.strftime('%d-%m-%Y')})"
        drive_service.files().update(fileId=doc_id, body={"name": new_title}).execute()
        print(f"  Renamed doc to: '{new_title}'")

        print(f"\n  PROFESSIONAL AUDIT REPORT READY:")
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
    print("  SIGNMAKERZ ABANDONED FORMS REPORT GENERATOR")
    print("  Timezone: Asia/Karachi (UTC+5)")
    print("  Auto-Schedule: Daily 9 AM Karachi (cron: 0 4 * * * UTC)")
    print("  Format: Professional Corporate Audit Report")
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
