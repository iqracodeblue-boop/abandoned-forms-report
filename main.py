import os
import requests
from datetime import datetime, timedelta
import pytz

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

    # Timezone Fix: Exact 9 AM to 9 PM logic for Asia/Karachi
    tz = pytz.timezone('Asia/Karachi')
    now = datetime.now(tz)
    end_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(days=1)
    
    # Format dates for Airtable
    start_str = start_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_str = end_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    params = {
        # Only get records modified between 9 AM yesterday and 9 AM today
        "filterByFormula": f"AND(IS_AFTER(LAST_MODIFIED_TIME(), '{start_str}'), IS_BEFORE(LAST_MODIFIED_TIME(), '{end_str}'))",
        "sort[0][field]": "Last Modified",
        "sort[0][direction]": "desc"
    }

    all_records = []
    offset = None

    print("Fetching abandoned forms from Airtable (9 AM to 9 AM)...")
    
    # Pagination Fix: Loop to fetch all records if they are more than 100
    while True:
        if offset:
            params["offset"] = offset
            
        response = requests.get(url, headers=headers, params=params)

        if response.status_code == 200:
            data = response.json()
            records = data.get("records", [])
            all_records.extend(records)
            offset = data.get("offset") # Check if there are more records
            if not offset:
                break # Exit loop when no more pages
        else:
            print(f"❌ Error fetching data: {response.status_code}")
            print(response.text)
            break

    print(f"✅ Successfully fetched {len(all_records)} records.")
    
    for record in all_records[:10]: # Print only first 10 to keep logs clean
        fields = record.get("fields", {})
        print(f"ID: {record.get('id')} | Name: {fields.get('Name', 'N/A')}")

if __name__ == "__main__":
    fetch_airtable_records()
