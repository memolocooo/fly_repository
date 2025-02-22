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
        "reportType": "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2",
        "dataStartTime": (datetime.utcnow() - timedelta(days=365)).isoformat(),  # Last 30 days
        "dataEndTime": datetime.utcnow().isoformat(),
        "marketplaceIds": ["A1AM78C64UM0Y8"]  # Amazon Mexico Marketplace
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 200:
        report_id = response.json().get("reportId")
        print(f"✅ Report requested: {report_id}")
        return report_id
    else:
        print(f"❌ Error requesting report: {response.text}")
        return None

def get_report_status(access_token, report_id):
    """Check the status of a requested report."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"

    headers = {
        "x-amz-access-token": access_token
    }

    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        processing_status = response.json().get("processingStatus")
        document_id = response.json().get("reportDocumentId")
        print(f"🔍 Report status: {processing_status}, Document ID: {document_id}")
        return processing_status, document_id
    else:
        print(f"❌ Error checking report status: {response.text}")
        return None, None

def download_report(access_token, document_id):
    """Download the settlement report and extract its contents."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}"

    headers = {
        "x-amz-access-token": access_token
    }

    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        report_url = response.json().get("url")

        # Download the report
        report_response = requests.get(report_url, stream=True)
        with open("settlement_report.gz", "wb") as f:
            f.write(report_response.content)

        # Extract the report
        with gzip.open("settlement_report.gz", "rb") as f_in, open("settlement_report.csv", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        print("✅ Report downloaded and extracted.")
        return "settlement_report.csv"
    else:
        print(f"❌ Error downloading report: {response.text}")
        return None

def process_settlement_report(file_path, selling_partner_id):
    """Process and store settlement data in PostgreSQL."""
    with open(file_path, mode='r', encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            print(f"Processing row: {row}")  # Debugging print
            new_entry = AmazonSettlementData(
                selling_partner_id=selling_partner_id,
                settlement_id=row.get("settlement_id"),
                date_time=row.get("date_time"),
                order_id=row.get("order_id"),
                type=row.get("type"),
                amount=row.get("amount"),
                amazon_fee=row.get("amazon_fee"),
                shipping_fee=row.get("shipping_fee"),
                total_amount=row.get("total_amount"),
                created_at=datetime.utcnow()
            )
            db.session.add(new_entry)

        db.session.commit()
    print("✅ Settlement data saved to database.")