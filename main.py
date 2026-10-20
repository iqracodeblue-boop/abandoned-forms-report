#!/usr/bin/env python3
"""
Daily Abandoned Forms Report Generator
======================================
End-to-end pipeline that runs in GitHub Actions daily.

PHASE 1 - Fetch abandoned form records from Airtable (9 AM -> next day 9 AM)
PHASE 2 - Fetch records from 5 Clarity endpoints, compare with Airtable
PHASE 3 - Auto-generate / overwrite a Google Docs report with the comparison
"""

import os
import sys
import json
import requests
from datetime import datetime, timedelta

# Phase 3 - Google Docs imports
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================================
# CONFIGURATION (loaded from environment variables / GitHub Secrets)
# ============================================================================

# --- Airtable credentials ---
# --- Airtable credentials ---
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

# --- Output directory (for GitHub Artifacts) ---
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================================
# TIME WINDOW: yesterday 9 AM -> today 9 AM
# ============================================================================

def get_time_window():
    """Returns (start, end) datetimes for the 24h window ending today 9 AM."""
    now = datetime.now()
    end = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < end:
        end = end - timedelta(days=1)
    start = end - timedelta(days=1)
    return start, end


# ============================================================================
# HELPERS - safely extract fields from Airtable/Clarity records
# ============================================================================

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


# ============================================================================
# PHASE 1: AIRTABLE DATA FETCH (with pagination)
# ============================================================================

def fetch_airtable_data(start_time, end_time):
    print("\n" + "=" * 60)
    print("PHASE 1: Fetching data from Airtable")
    print("=" * 60)
    print(f"Time window: {start_time} -> {end_time}")

    if not all([AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME]):
        print("Missing Airtable credentials - skipping Phase 1")
        return []

    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end_time.strftime("%Y-%m-%dT%H:%M:%S")

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }

    filter_formula = (
        f"AND("
        f"IS_AFTER({{Created}}, '{start_str}'), "
        f"IS_BEFORE({{Created}}, '{end_str}')"
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
            print(f"  Page {page}: fetched {len(records)} records "
                  f"(running total: {len(all_records)})")
            page += 1
            offset = data.get("offset")
            if not offset:
                break
        except requests.exceptions.RequestException as e:
            print(f"Airtable API error: {e}")
            break

    print(f"Airtable TOTAL: {len(all_records)} records")

    out_path = os.path.join(OUTPUT_DIR, "airtable_data.json")
    with open(out_path, "w") as f:
        json.dump({
            "source": "airtable",
            "time_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
            "total_count": len(all_records),
            "records": all_records,
        }, f, indent=2, default=str)

    return all_records


# ============================================================================
# PHASE 2: CLARITY DATA FETCH + COMPARISON
# ============================================================================

def fetch_clarity_data(start_time, end_time):
    print("\n" + "=" * 60)
    print("PHASE 2: Fetching data from Clarity APIs")
    print("=" * 60)

    all_clarity_records = []
    start_str = start_time.isoformat()
    end_str = end_time.isoformat()

    for endpoint in CLARITY_ENDPOINTS:
        idx = endpoint["index"]
        url = endpoint["url"]
        token = endpoint["token"]

        print(f"\n  Endpoint {idx}: {url}")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        params = {"start": start_str, "end": end_str}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                records = data.get("records") or data.get("data") or data.get("items") or data.get("results") or []
            elif isinstance(data, list):
                records = data
            else:
                records = []
            print(f"    Received {len(records)} records from endpoint {idx}")
            for r in records:
                if isinstance(r, dict):
                    r["_clarity_endpoint"] = idx
            all_clarity_records.extend(records)
        except Exception as e:
            print(f"    Endpoint {idx} error: {e}")

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
    print(f"  Only in Clarity  (not in Airtable): {len(only_in_clarity)}")
    print(f"  Only in Airtable (not in Clarity ): {len(only_in_airtable)}")

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
# PHASE 3: GOOGLE DOCS REPORT GENERATION
# ============================================================================

def format_airtable_summary(record):
    fields = safe_get(record, "fields", default={}) or {}
    rid = get_airtable_record_id(record) or "?"
    created = (fields.get("Created") or fields.get("Created At")
               or fields.get("US NY Time") or "?")
    page_url = fields.get("Page URL") or "?"
    record_source = fields.get("Record Source") or "?"
    is_spam = fields.get("isSpam") or "?"
    ip = fields.get("IP Address") or "?"
    project = fields.get("Project") or "?"
    return (f"  - ID: {rid}\n    Created: {created}\n"
            f"    Project: {project} | Source: {record_source}\n"
            f"    Page: {page_url}\n    IP: {ip} | isSpam: {is_spam}")


def format_clarity_summary(record):
    rid = get_clarity_record_id(record) or "?"
    endpoint = record.get("_clarity_endpoint", "?") if isinstance(record, dict) else "?"
    url = (record.get("url") or record.get("pageUrl")
           or safe_get(record, "fields", "Page URL") or "?")
    ts = (record.get("timestamp") or record.get("createdAt")
          or record.get("created_at") or safe_get(record, "fields", "Created") or "?")
    return (f"  - ID: {rid} (Endpoint {endpoint})\n"
            f"    Timestamp: {ts}\n    URL: {url}")


def generate_google_docs_report(comparison, airtable_records, clarity_records):
    print("\n" + "=" * 60)
    print("PHASE 3: Generating Google Docs Report")
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

        now = datetime.now()
        date_str = now.strftime("%d %b %Y, %H:%M")
        window_start = comparison["time_window"]["start"]
        window_end = comparison["time_window"]["end"]
        only_in_clarity_records = comparison.get("only_in_clarity_records", [])
        only_in_airtable_records = comparison.get("only_in_airtable_records", [])

        def build_airtable_mismatch(records, max_display=15):
            if not records:
                return "None - all Airtable forms have matching Clarity records."
            shown = records[:max_display]
            lines = [format_airtable_summary(r) for r in shown]
            text = "\n".join(lines)
            if len(records) > max_display:
                text += f"\n\n  ... and {len(records) - max_display} more"
            return text

        def build_clarity_mismatch(records, max_display=15):
            if not records:
                return "None - all Clarity forms have matching Airtable records."
            shown = records[:max_display]
            lines = [format_clarity_summary(r) for r in shown]
            text = "\n".join(lines)
            if len(records) > max_display:
                text += f"\n\n  ... and {len(records) - max_display} more"
            return text

        clarity_mismatch_text = build_clarity_mismatch(only_in_clarity_records)
        airtable_mismatch_text = build_airtable_mismatch(only_in_airtable_records)

        if comparison["difference"] > 0:
            recommendation = (f"Clarity has {comparison['difference']} MORE forms than Airtable. "
                "Likely cause: some form submissions were captured by Clarity but failed to sync to Airtable. "
                "Action: review the unmatched Clarity records below, then check Airtable's webhook/integration health.")
        elif comparison["difference"] < 0:
            recommendation = (f"Airtable has {abs(comparison['difference'])} MORE forms than Clarity. "
                "Likely cause: stale records in Airtable, or Clarity's date filter is excluding valid sessions. "
                "Action: verify Airtable entries match today's window, and confirm Clarity's date filter is inclusive of the 9 AM -> 9 AM window.")
        else:
            recommendation = ("Both sources match perfectly in record count. Check the unique-ID "
                "comparison below to confirm every individual form matches, not just the totals.")

        spam_count = 0
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            v = fields.get("isSpam")
            if v and str(v).lower() == "true":
                spam_count += 1
        non_spam_count = comparison["airtable_count"] - spam_count

        source_counts = {}
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            src = fields.get("Record Source") or "Unknown"
            source_counts[src] = source_counts.get(src, 0) + 1
        source_breakdown = "\n".join(f"    {src}: {cnt}" for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])) or "    (no source info)"

        project_counts = {}
        for r in airtable_records:
            fields = safe_get(r, "fields", default={}) or {}
            p = fields.get("Project") or "Unknown"
            project_counts[p] = project_counts.get(p, 0) + 1
        project_breakdown = "\n".join(f"    {p}: {cnt}" for p, cnt in sorted(project_counts.items(), key=lambda x: -x[1])) or "    (no project info)"

        airtable_sample = json.dumps(airtable_records[:3], indent=2, default=str)
        if len(airtable_sample) > 2000:
            airtable_sample = airtable_sample[:2000] + "\n... (truncated)"
        clarity_sample = json.dumps(clarity_records[:3], indent=2, default=str)
        if len(clarity_sample) > 2000:
            clarity_sample = clarity_sample[:2000] + "\n... (truncated)"

        report_body = f"""DAILY ABANDONED FORMS REPORT
==================================================

Report Date Range  : {window_start} -> {window_end}
Report Generated   : {date_str}
Generated By       : Daily Forms Bot (GitHub Actions)

==================================================
                SUMMARY
==================================================
Airtable Total Forms           : {comparison['airtable_count']}
  - Spam (isSpam=true)         : {spam_count}
  - Non-spam                   : {non_spam_count}
Clarity Total Forms             : {comparison['clarity_count']}
Difference (Clarity - Airtable) : {comparison['difference']}

Airtable Unique IDs : {comparison.get('airtable_unique_ids', 'N/A')}
Clarity Unique IDs  : {comparison.get('clarity_unique_ids', 'N/A')}

==================================================
        AIRTABLE BREAKDOWN (by Source)
==================================================
{source_breakdown}

==================================================
        AIRTABLE BREAKDOWN (by Project)
==================================================
{project_breakdown}

==================================================
              MISMATCHED FORMS
==================================================

[A] Forms ONLY in Clarity (not in Airtable)
    Count: {len(only_in_clarity_records)}
{clarity_mismatch_text}

[B] Forms ONLY in Airtable (not in Clarity)
    Count: {len(only_in_airtable_records)}
{airtable_mismatch_text}

==================================================
              RECOMMENDATION
==================================================
{recommendation}

==================================================
              RAW DATA SAMPLE (first 3 records)
==================================================
Airtable sample:
{airtable_sample}

Clarity sample:
{clarity_sample}

==================================================
For full JSON data, download the GitHub Actions artifacts
named 'airtable-data', 'clarity-data', and 'comparison-report'
from the workflow run page.
==================================================
"""

        doc_content = doc.get("body", {}).get("content", [])
        if doc_content:
            end_index = max(c.get("endIndex", 1) for c in doc_content)
            if end_index > 1:
                delete_request = [{
                    "deleteContentRange": {
                        "range": {"startIndex": 1, "endIndex": max(1, end_index - 1)}
                    }
                }]
                try:
                    docs_service.documents().batchUpdate(
                        documentId=doc_id, body={"requests": delete_request}
                    ).execute()
                    print("  Cleared existing doc content")
                except HttpError as e:
                    print(f"  (skip clear) {e}")

        insert_request = [{
            "insertText": {
                "location": {"index": 1},
                "text": report_body,
            }
        }]
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": insert_request}
        ).execute()
        print("  Inserted new report content")

        new_title = f"Daily Abandoned Forms Report - {now.strftime('%Y-%m-%d')}"
        drive_service.files().update(fileId=doc_id, body={"name": new_title}).execute()
        print(f"  Renamed doc to: '{new_title}'")

        print(f"\n  REPORT READY:")
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
    print("=" * 60)

    start_time, end_time = get_time_window()
    print(f"Time window: {start_time} -> {end_time}")

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
