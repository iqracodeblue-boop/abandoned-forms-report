import os
import requests
from datetime import datetime, timedelta

def fetch_airtable_records():
    # Get credentials from environment variables (GitHub Secrets)
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

    # Calculate time range: 9 AM yesterday to 9 AM today
    now = datetime.utcnow()
    start_time = (now - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    
    # Filter records updated in the last 24 hours
    params = {
        "filterByFormula": f"IS_AFTER(LAST_MODIFIED_TIME(), '{start_time.isoformat()}Z')",
        "sort[0][field]": "Last Modified",
        "sort[0][direction]": "desc"
    }

    print("Fetching abandoned forms from Airtable...")
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        records = data.get("records", [])
        print(f"✅ Successfully fetched {len(records)} records.")
        
        for record in records:
            fields = record.get("fields", {})
            print(f"ID: {record.get('id')} | Name: {fields.get('Name', 'N/A')}")
    else:
        print(f"❌ Error fetching data: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    fetch_airtable_records()
