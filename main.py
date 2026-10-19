import os
import requests
from datetime import datetime, timedelta

def fetch_airtable_records():
    api_key = os.environ.get("AIRTABLE_API_KEY")
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    table_id = os.environ.get("AIRTABLE_TABLE_ID")
    
    if not all([api_key, base_id, table_id]):
        print("Error: Missing Airtable credentials.")
        return

    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # EXACT 9 AM to 9 AM Logic
    now = datetime.utcnow()
    # End time is TODAY at exactly 9:00:00 AM
    end_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    # Start time is YESTERDAY at exactly 9:00:00 AM
    start_time = end_time - timedelta(days=1)
    
    # Format dates for Airtable
    start_str = start_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_str = end_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    print(f"Fetching abandoned forms from {start_str} to {end_str}...")

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
            records = data.get("records", [])
            all_records.extend(records)
            offset = data.get("offset")
            if not offset:
                break
        else:
            print(f"❌ Error fetching data: {response.status_code}")
            print(response.text)
            break

    print(f"✅ Successfully fetched {len(all_records)} records.")
    
    for record in all_records[:10]:
        fields = record.get("fields", {})
        print(f"ID: {record.get('id')} | Name: {fields.get('Name', 'N/A')}")

if __name__ == "__main__":
    fetch_airtable_records()
