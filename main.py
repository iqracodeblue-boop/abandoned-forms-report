import os
import requests
import json
import time
from datetime import datetime, timedelta

def fetch_airtable_records():
    api_key = os.environ.get("AIRTABLE_API_KEY")
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    table_id = os.environ.get("AIRTABLE_TABLE_ID")
    
    if not all([api_key, base_id, table_id]):
        print("[ERROR] Missing Airtable credentials.")
        return

    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # EXACT 9 AM to 9 AM Logic
    now = datetime.utcnow()
    end_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(days=1)
    
    start_str = start_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_str = end_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    print(f"[INFO] Fetching abandoned forms from {start_str} to {end_str}...")

    params = {
        "filterByFormula": f"AND(IS_AFTER(LAST_MODIFIED_TIME(), '{start_str}'), IS_BEFORE(LAST_MODIFIED_TIME(), '{end_str}'))",
        "sort[0][field]": "Last Modified",
        "sort[0][direction]": "desc"
    }

    all_records = []
    offset = None
    page = 1

    while True:
        if offset:
            params["offset"] = offset
            
        response = requests.get(url, headers=headers, params=params)

        if response.status_code == 200:
            data = response.json()
            records = data.get("records", [])
            all_records.extend(records)
            print(f"[INFO] Page {page}: Fetched {len(records)} records.")
            offset = data.get("offset")
            if not offset:
                break
            page += 1
            time.sleep(0.2) # 200ms delay to respect Airtable rate limits
        else:
            print(f"[ERROR] Failed to fetch data: {response.status_code}")
            print(response.text)
            break

    print(f"[SUCCESS] Total fetched: {len(all_records)} records.")
    
    # Save data as an artifact for Sir to review
    with open("abandoned_forms_data.json", "w") as f:
        json.dump(all_records, f, indent=4)
    print("[INFO] Data saved to abandoned_forms_data.json for review.")

if __name__ == "__main__":
    fetch_airtable_records()
