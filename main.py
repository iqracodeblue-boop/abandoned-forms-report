import os
import requests
import json
from datetime import datetime, timedelta

# --- PHASE 1: Fetch Airtable Data ---
def fetch_airtable_records():
    api_key = os.environ.get("AIRTABLE_API_KEY")
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    table_id = os.environ.get("AIRTABLE_TABLE_ID")
    
    if not all([api_key, base_id, table_id]):
        print("[ERROR] Missing Airtable credentials.")
        return []

    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    now = datetime.utcnow()
    end_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(days=1)
    
    start_str = start_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_str = end_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    print(f"[INFO] Fetching Airtable data from {start_str} to {end_str}...")

    params = {
        "filterByFormula": f"AND(IS_AFTER(LAST_MODIFIED_TIME(), '{start_str}'), IS_BEFORE(LAST_MODIFIED_TIME(), '{end_str}'))",
        "sort[0][field]": "Last Modified",
        "sort[0][direction]": "desc"
    }

    all_records = []
    offset = None

    while True:
        if offset:
            params["offset"] = offset
            
        response = requests.get(url, headers=headers, params=params)

        if response.status_code == 200:
            data = response.json()
            all_records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
        else:
            print(f"[ERROR] Airtable fetch failed: {response.status_code}")
            break

    print(f"[SUCCESS] Airtable Total: {len(all_records)} records.")
    return all_records

# --- PHASE 2: Fetch Clarity Data ---
def fetch_clarity_records():
    api_token = os.environ.get("CLARITY_API_TOKEN")
    base_urls_str = os.environ.get("CLARITY_BASE_URLS")
    
    if not all([api_token, base_urls_str]):
        print("[ERROR] Missing Clarity credentials.")
        return []

    base_urls = [url.strip() for url in base_urls_str.split(",")]
    endpoint = "/api/v1/internal/abandoned-forms"
    
    all_clarity_records = []

    print(f"[INFO] Fetching Clarity data from {len(base_urls)} projects...")

    for base_url in base_urls:
        page = 1
        while True:
            url = f"{base_url}{endpoint}?per_page=25&api_token={api_token}&page={page}"
            try:
                response = requests.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    records = data.get("data", [])
                    all_clarity_records.extend(records)
                    
                    # Check if there are more pages
                    if len(records) < 25:
                        break
                    page += 1
                else:
                    print(f"[WARNING] {base_url} returned status {response.status_code}")
                    break
            except Exception as e:
                print(f"[ERROR] Failed to fetch from {base_url}: {e}")
                break

    print(f"[SUCCESS] Clarity Total: {len(all_clarity_records)} records.")
    return all_clarity_records

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    airtable_data = fetch_airtable_records()
    clarity_data = fetch_clarity_records()

    print("\n" + "="*50)
    print("[COMPARISON REPORT]")
    print(f"Airtable Total Records: {len(airtable_data)}")
    print(f"Clarity Total Records:   {len(clarity_data)}")
    
    if len(airtable_data) == len(clarity_data):
        print("[RESULT] ✅ Results MATCH perfectly!")
    else:
        print("[RESULT] ⚠️ Results DO NOT MATCH. Data discrepancy detected.")
    print("="*50)
    
    # Save Clarity data as an artifact
    with open("clarity_data.json", "w") as f:
        json.dump(clarity_data, f, indent=4)
    print("[INFO] Clarity data saved to clarity_data.json for review.")
