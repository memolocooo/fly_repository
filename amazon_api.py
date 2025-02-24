import requests
from datetime import datetime, timedelta  
from models import db, AmazonSettlementData
import gzip
import shutil
import csv
import os
import requests

def fetch_orders_from_amazon(selling_partner_id, access_token, created_after):
    url = "https://sellingpartnerapi-na.amazon.com/orders/v0/orders"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    params = {
        "MarketplaceIds": ["A1AM78C64UM0Y8"],  # ✅ Amazon Mexico Marketplace
        "CreatedAfter": (datetime.utcnow() - timedelta(days=365)).isoformat(),  # ✅ Ensure 1 year
        "OrderStatuses": ["Shipped", "Unshipped", "Canceled"]
    }

    print(f"🔍 Fetching orders for seller {selling_partner_id} since {params['CreatedAfter']}")

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        orders = response.json()
        print(f"✅ Amazon API Response: {orders}")  # ✅ Debugging print

        if "Orders" in orders:
            return orders["Orders"]
        elif "payload" in orders and "Orders" in orders["payload"]:
            return orders["payload"]["Orders"]
        else:
            print("❌ No orders found in response!")
            return []
    else:
        print(f"❌ Error fetching orders: {response.status_code} - {response.text}")
        return []


def request_settlement_report(access_token, selling_partner_id):
    """Request the settlement report from Amazon."""
    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    payload = {
        "reportType": "GET_LEDGER_SUMMARY_VIEW_DATA",  # ✅ Corrected Report Type
        "dataStartTime": (datetime.utcnow() - timedelta(days=180)).isoformat(),
        "dataEndTime": datetime.utcnow().isoformat(),
        "marketplaceIds": ["A1AM78C64UM0Y8"]  # Amazon Mexico Marketplace
    }

    print(f"📤 Requesting settlement report with payload: {payload}")

    response = requests.post(url, headers=headers, json=payload)

    # Log full response for debugging
    print(f"🛑 Amazon Response: {response.status_code} - {response.text}")

    if response.status_code == 202:  # ✅ 202 Accepted means report is processing
        report_id = response.json().get("reportId")
        print(f"✅ Report request accepted, processing... Report ID: {report_id}")
        return report_id  
    else:
        print(f"❌ Error requesting report: {response.status_code} - {response.text}")
        return None


    

def get_report_status(access_token, report_id):
    """Check the status of a requested report."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"

    headers = {
        "x-amz-access-token": access_token
    }

    print(f"📡 Checking status for report ID: {report_id}")

    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        processing_status = response.json().get("processingStatus")
        document_id = response.json().get("reportDocumentId")
        print(f"🔍 Report status: {processing_status}, Document ID: {document_id}")
        return processing_status, document_id
    else:
        print(f"❌ Error checking report status: {response.text}")
        return None, None



import os
import requests

def download_report(access_token, document_id):
    """Download the settlement report from Amazon."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}"
    
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    print(f"📥 Downloading report with Document ID: {document_id}")

    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        print(f"✅ Report metadata retrieved: {response.json()}")  # Debugging

        report_url = response.json().get("url")
        if not report_url:
            print("❌ No download URL found!")
            return None

        print(f"🔗 Report Download URL: {report_url}")

        # Download actual file
        report_response = requests.get(report_url)
        if report_response.status_code == 200:
            file_path = f"settlement_report_{document_id}.txt"
            print(f"✅ Report downloaded successfully, saving to {file_path}")

            with open(file_path, "wb") as f:
                f.write(report_response.content)
            
            print(f"📂 Report saved at {file_path}")
            return file_path
        else:
            print(f"❌ Error downloading report: {report_response.status_code}")
            return None
    else:
        print(f"❌ Failed to retrieve document metadata: {response.status_code}")
        return None





def process_settlement_report(file_path, selling_partner_id):
    """Process the settlement report and store data in PostgreSQL."""
    print(f"📂 Opening settlement report: {file_path}")

    with open(file_path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    print(f"🔍 First 5 lines of the report:\n{lines[:5]}")  # DEBUGGING LINE

    if not lines:
        print("❌ Settlement report is empty!")
        return

    # Process CSV data
    reader = csv.reader(lines)
    headers = next(reader)  # Get column names
    print(f"📝 Headers: {headers}")  # DEBUGGING LINE

    # Continue with inserting into the database...


def store_settlement_data(data):
    """Insert settlement data into PostgreSQL."""
    for row in data:
        print(f"🛠️ Inserting row: {row}")  # DEBUGGING LINE
        new_entry = AmazonSettlementData(
            selling_partner_id=row["selling_partner_id"],
            settlement_id=row["settlement_id"],
            date_time=row["date_time"],
            order_id=row["order_id"],
            type=row["type"],
            amount=row["amount"],
            amazon_fee=row["amazon_fee"],
            shipping_fee=row["shipping_fee"],
            total_amount=row["total_amount"],
            created_at=datetime.utcnow()
        )
        db.session.add(new_entry)
    
    db.session.commit()
    print(f"✅ Successfully stored {len(data)} settlement records.")

