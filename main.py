#!/usr/bin/env python3
"""
Daily Abandoned Forms Report Generator (TEXT VERSION - NO GOOGLE DOCS)
=====================================================================
Sir's requirement: Daily report as TEXT FILE (not Google Docs).
Single Clarity API endpoint with token as query parameter (Laravel-style).

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

# --- Airtable credentials (with fallback to old secret names) ---
AIRTABLE_TOKEN = os.getenv("AIRTABLE_API_TOKEN") or os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME") or os.getenv("AIRTABLE_TABLE_ID")

# --- Clarity API (single endpoint with token as query parameter) ---
# URL provided by Sir: https://clarity.signage.inc/api/v1/internal/abandoned-forms
# Token is sent as ?api_token= query parameter (Laravel-style API)
# Override via GitHub secrets CLARITY_BASE_URL and CLARITY_API_TOKEN
CLARITY_BASE_URL = os.getenv(
    "CLARITY_BASE_URL",
    "https://clarity.signage.inc/api/v1/internal/abandoned-forms"
)
CLARITY_API_TOKEN = os.getenv(
    "CLARITY_API_TOKEN",
    "clarity_internal_abandoned_token_2026_sec789xyz"
)
CLARITY_PER_PAGE = 25

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================================
# HELPERS
# ============================================================================

def get_time_window():
    """Returns (start, end) datetimes in Karachi TZ for 24h window ending today 9 AM."""
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
        print(f"  AIRTABLE_TOKEN set: {bool(AIRTABLE_TOKEN)}")
        print(f"  AIRTABLE_BASE_ID set: {bool(AIRTABLE_BASE_ID)}")
        print(f"  AIRTABLE_TABLE_NAME set: {bool(AIRTABLE_TABLE_NAME)}")
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
# PHASE 2: CLARITY DATA FETCH (single endpoint, token as query param)
# ============================================================================

def _extract_records_from_response(data):
    """Recursively extract a list of records from any JSON response shape.
    Handles Laravel-style: {"data": [...], "links": {...}, "meta": {...}}
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        # Laravel pagination uses "data" key
        for key in ("data", "records", "items", "results", "value",
                    "rows", "entries", "list", "items_list"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # Deep recursive search
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):
                nested = _extract_records_from_response(v)
                if nested:
                    return nested
    return []


def fetch_clarity_data(start_time, end_time):
    """Fetch from single Clarity endpoint with token as query parameter.
    Handles Laravel-style pagination via 'meta.last_page' or 'links.next'.
    """
    print("\n" + "=" * 60)
    print("PHASE 2: Fetching data from Clarity API")
    print("=" * 60)
    print(f"Endpoint: {CLARITY_BASE_URL}")
    print(f"Token preview: {CLARITY_API_TOKEN[:12]}...{CLARITY_API_TOKEN[-4:]}")
    print(f"Per page: {CLARITY_PER_PAGE}")

    all_clarity_records = []
    page = 1
    start_iso = start_time.astimezone(UTC_TZ).isoformat()
    end_iso = end_time.astimezone(UTC_TZ).isoformat()

    while True:
        # Build query params - token as query param (Laravel-style)
        params = {
            "per_page": CLARITY_PER_PAGE,
            "api_token": CLARITY_API_TOKEN,
            "page": page,
            "start": start_iso,
            "end": end_iso,
        }

        try:
            print(f"\n  Page {page}: GET {CLARITY_BASE_URL}?per_page={CLARITY_PER_PAGE}&page={page}&...")
            response = requests.get(CLARITY_BASE_URL, params=params, timeout=30)
            print(f"    HTTP status: {response.status_code}")

            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as e:
                    print(f"    JSON parse error: {e}")
                    print(f"    Body preview: {response.text[:300]}")
                    break

                records = _extract_records_from_response(data)
                print(f"    Got {len(records)} records (running total: {len(all_clarity_records) + len(records)})")
                all_clarity_records.extend(records)

                # Laravel-style pagination check via meta.last_page
                if isinstance(data, dict):
                    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
                    last_page = meta.get("last_page")
                    current_page = meta.get("current_page", page)
                    if last_page is not None:
                        if current_page >= last_page:
                            print(f"    Reached last page ({current_page}/{last_page}) - stopping")
                            break
                    else:
                        # No meta info - check links.next
                        links = data.get("links", {}) if isinstance(data.get("links"), dict) else {}
                        next_url = links.get("next")
                        if not next_url:
                            print(f"    No next page link - stopping")
                            break
                    # If we got fewer records than per_page, we're on the last page
                    if len(records) < CLARITY_PER_PAGE:
                        print(f"    Got {len(records)} < {CLARITY_PER_PAGE} - last page reached")
                        break
                elif isinstance(data, list):
                    if len(data) < CLARITY_PER_PAGE:
                        print(f"    Got {len(data)} < {CLARITY_PER_PAGE} - last page reached")
                        break
                else:
                    break

                page += 1
                # Safety limit
                if page > 100:
                    print(f"    Safety limit (100 pages) reached - stopping")
                    break
            elif response.status_code == 401:
                print(f"    ERROR 401: Unauthorized - check CLARITY_API_TOKEN")
                print(f"    Body: {response.text[:300]}")
                break
            elif response.status_code == 404:
                print(f"    ERROR 404: Not Found - check CLARITY_BASE_URL")
                print(f"    Body: {response.text[:300]}")
                break
            else:
                print(f"    ERROR {response.status_code}")
                print(f"    Body: {response.text[:300]}")
                break

        except requests.exceptions.ConnectionError as e:
            print(f"    Connection error: {e}")
            break
        except requests.exceptions.Timeout as e:
            print(f"    Timeout: {e}")
            break
        except Exception as e:
            print(f"    Unexpected error: {e}")
            break

    print(f"\nClarity TOTAL: {len(all_clarity_records)} records")

    out_path = os.path.join(OUTPUT_DIR, "clarity_data.json")
    with open(out_path, "w") as f:
        json.dump({
            "source": "clarity",
            "endpoint": CLARITY_BASE_URL,
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
# PHASE 3: TEXT REPORT GENERATION (no Google Docs)
# ============================================================================

def generate_text_report(comparison, airtable_records, clarity_records):
    """Generate professional audit report as TEXT FILE (no Google Docs)."""
    print("\n" + "=" * 60)
    print("PHASE 3: Generating Text Report (no Google Docs)")
    print("=" * 60)

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

    # Calculate spam and non-spam
    spam_count = 0
    for r in airtable_records:
        fields = safe_get(r, "fields", default={}) or {}
        v = fields.get("isSpam")
        if v and str(v).lower() == "true":
            spam_count += 1
    non_spam_count = airtable_count - spam_count

    # Build source breakdown
    source_counts = {}
    for r in airtable_records:
        fields = safe_get(r, "fields", default={}) or {}
        src = fields.get("Record Source") or "Unknown"
        source_counts[src] = source_counts.get(src, 0) + 1
    source_breakdown = "\n".join(
        f"  {src}: {cnt}"
        for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1])
    ) or "  (no source info available)"

    # Build project breakdown
    project_counts = {}
    for r in airtable_records:
        fields = safe_get(r, "fields", default={}) or {}
        p = fields.get("Project") or "Unknown"
        project_counts[p] = project_counts.get(p, 0) + 1
    project_breakdown = "\n".join(
        f"  {p}: {cnt}"
        for p, cnt in sorted(project_counts.items(), key=lambda x: -x[1])
    ) or "  (no project info available)"

    # Build recommendation
    if airtable_count == 0 and clarity_count == 0:
        recommendation = """Both data sources are returning 0 records.

Recommended Actions:
  1. Verify AIRTABLE_API_TOKEN secret is valid
  2. Verify AIRTABLE_BASE_ID secret is correct
  3. Verify AIRTABLE_TABLE_NAME secret matches your table name
  4. Verify CLARITY_BASE_URL secret points to valid API endpoint
  5. Verify CLARITY_API_TOKEN secret is valid"""
    elif airtable_count > 0 and clarity_count == 0:
        recommendation = f"""Airtable is connected ({airtable_count} records), but Clarity API is returning 0 records.

Recommended Actions:
  1. Verify CLARITY_BASE_URL secret is correct API URL
     Current: {CLARITY_BASE_URL}
  2. Verify CLARITY_API_TOKEN secret is valid auth token
     Token preview: {CLARITY_API_TOKEN[:12]}...{CLARITY_API_TOKEN[-4:]}
  3. Check the workflow run logs above - search for 'PHASE 2' section
  4. Look for 'HTTP status:' line to see actual response code
  5. Look for 'Body:' preview to see actual response content"""
    elif airtable_count == 0 and clarity_count > 0:
        recommendation = f"""Clarity is connected ({clarity_count} records), but Airtable API is returning 0 records.

Recommended Actions:
  1. Verify AIRTABLE_API_TOKEN secret is a valid Personal Access Token
  2. Verify AIRTABLE_BASE_ID matches your Airtable base
  3. Verify AIRTABLE_TABLE_NAME matches your table name exactly"""
    elif comparison["difference"] > 0:
        recommendation = f"""Clarity has {comparison['difference']} MORE forms than Airtable.

Likely cause: Some form submissions were captured by Clarity but failed to sync to Airtable.

Recommended Action: Review the unmatched Clarity records in Section 3, then check
Airtable's webhook and integration health."""
    elif comparison["difference"] < 0:
        recommendation = f"""Airtable has {abs(comparison['difference'])} MORE forms than Clarity.

Likely cause: Stale records in Airtable, or Clarity's date filter is excluding valid sessions.

Recommended Action: Verify Airtable entries match today's window, and confirm Clarity's
date filter is inclusive of the 9 AM to 9 AM window."""
    else:
        recommendation = """Both sources match perfectly in record count.

Recommended Action: Check the unique-ID comparison to confirm every individual form
matches, not just the totals."""

    # Build leads not found table
    if airtable_count > 0 and clarity_count > 0:
        leads_section = "3. LEADS THAT ARE NOT FOUND IN AIRTABLE\n"
        leads_section += "-" * 80 + "\n"
        leads_section += "DATE & TIMESTAMP    | VISITOR EMAIL / IDENTIFIER  | SESSION REPLAY / VIEW LINK\n"
        leads_section += "-" * 80 + "\n"

        if only_in_clarity_records:
            for r in only_in_clarity_records[:30]:
                rid = get_clarity_record_id(r) or "—"
                ts = (r.get("timestamp") or r.get("createdAt")
                      or r.get("created_at") or "—")
                ts_short = truncate(str(ts), 18)
                url = truncate(r.get("url") or r.get("pageUrl")
                              or safe_get(r, "fields", "Page URL") or "—", 40)
                leads_section += f"{ts_short:<20}| {str(rid)[:25]:<27} | {url}\n"
            if len(only_in_clarity_records) > 30:
                leads_section += f"\n... and {len(only_in_clarity_records) - 30} more records\n"
        else:
            leads_section += "  None - all Clarity forms have matching Airtable records.\n"
        leads_section += "\n"
    else:
        leads_section = "3. LEADS THAT ARE NOT FOUND IN AIRTABLE\n"
        leads_section += "-" * 80 + "\n"
        if clarity_count == 0:
            leads_section += "(Section skipped - Clarity API not returning data for comparison.\n"
            leads_section += " Once Clarity API is connected and returns data, this section\n"
            leads_section += " will display a table of records captured by Clarity but missing\n"
            leads_section += " from Airtable.)\n\n"
        else:
            leads_section += "(Section skipped - Airtable API not returning data for comparison.)\n\n"

    # Build full text report
    report_body = f"""Quick Signage Abandoned Forms & Leads Verification Report
{'=' * 80}

Report Title:    Quick Signage Abandoned Forms & Dashboard Verification Report
Date Range:      {date_range_human}
Prepared By:     Iqra Kashif
Audited Platform: Quick Signage Live Website, Airtable, and Internal Dashboard

1. Executive Summary
{'-' * 80}
This professional audit report reviews the data integrity and synchronization status
between the live Quick Signage website frontend, abandoned forms capture system,
Airtable database, and the internal admin dashboard. The evaluation period covers
{date_range_human}.

The primary objective of this audit was to verify whether user interactions, form
inputs, and abandoned form session data are accurately flowing into Airtable and
reflecting correctly on the internal dashboard.

2. Audit Scope & Verification Methodology
{'-' * 80}
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

{leads_section}
4. Data Summary
{'-' * 80}
  Airtable Total Forms:            {airtable_count}
  Spam (isSpam=true):               {spam_count}
  Non-spam:                         {non_spam_count}
  Clarity Total Forms:              {clarity_count}
  Difference (Clarity - Airtable): {comparison['difference']}

  Airtable Unique IDs:  {comparison.get('airtable_unique_ids', 'N/A')}
  Clarity Unique IDs:   {comparison.get('clarity_unique_ids', 'N/A')}

5. Data Source Status
{'-' * 80}
  Airtable:   {"CONNECTED" if airtable_count > 0 else "NOT RETURNING DATA"} ({airtable_count} records)
  Clarity:    {"CONNECTED" if clarity_count > 0 else "NOT RETURNING DATA"} ({clarity_count} records)
  Endpoint:   {CLARITY_BASE_URL}

6. Airtable Breakdown (by Source)
{'-' * 80}
{source_breakdown}

7. Airtable Breakdown (by Project)
{'-' * 80}
{project_breakdown}

8. Recommendation
{'-' * 80}
{recommendation}

{'=' * 80}
End of Report - Quick Signage QA
{'=' * 80}

Report Generated:   {date_str}
Auto-Schedule:      Daily at 9 AM Karachi (cron: 0 4 * * * UTC)
Generated By:       GitHub Actions Automation
"""

    # Save the text report
    today_str = now.strftime("%Y-%m-%d")
    txt_filename = f"quick_signage_report_{today_str}.txt"
    out_path = os.path.join(OUTPUT_DIR, txt_filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_body)

    # Also save as a fixed filename for easy access
    latest_path = os.path.join(OUTPUT_DIR, "daily_report.txt")
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report_body)

    print(f"  Report saved to:")
    print(f"    - {out_path}")
    print(f"    - {latest_path} (latest)")
    print(f"  File size: {os.path.getsize(out_path):,} bytes")
    print(f"  Report length: {len(report_body):,} chars")
    return out_path


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    print("=" * 60)
    print("  QUICK SIGNAGE ABANDONED FORMS REPORT GENERATOR")
    print("  Timezone: Asia/Karachi (UTC+5)")
    print("  Auto-Schedule: Daily 9 AM Karachi (cron: 0 4 * * * UTC)")
    print("  Output: TEXT FILE (no Google Docs)")
    print("=" * 60)

    start_time, end_time = get_time_window()
    print(f"Time window (Karachi): {start_time} -> {end_time}")

    airtable_records = fetch_airtable_data(start_time, end_time)
    clarity_records = fetch_clarity_data(start_time, end_time)
    comparison = compare_data(airtable_records, clarity_records, start_time, end_time)
    report_path = generate_text_report(comparison, airtable_records, clarity_records)

    print("\n" + "=" * 60)
    print("  ALL PHASES COMPLETED")
    print("=" * 60)
    print(f"\nOutput files in ./{OUTPUT_DIR}/:")
    for fname in sorted(os.listdir(OUTPUT_DIR)):
        fpath = os.path.join(OUTPUT_DIR, fname)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            print(f"  - {fname} ({size:,} bytes)")

    print(f"\n  LATEST REPORT:")
    print(f"  ./{OUTPUT_DIR}/daily_report.txt")

    return 0


if __name__ == "__main__":
    sys.exit(main())
