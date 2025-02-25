import requests
from datetime import datetime, timedelta  
from models import db, AmazonSettlementData
import gzip
import shutil
import csv
import os
import requests
import time
import json
import chardet
from sp_api.api import Reports
from sp_api.base import Marketplaces, ReportType
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




# Amazon Mexico Marketplace ID
MARKETPLACE_ID = "A1AM78C64UM0Y8"

def fetch_fba_fees_report(access_token, selling_partner_id):
    """Request the FBA Fees Report from Amazon SP-API using ReportType Enum."""
    
    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    payload = {
        "reportType": ReportType.FEE_DISCOUNTS_REPORT,  # ✅ Using Enum Instead of Hardcoded String
        "dataStartTime": (datetime.utcnow() - timedelta(days=365)).isoformat(),  # Last Year’s Data
        "marketplaceIds": [MARKETPLACE_ID]
    }

    print(f"📤 Requesting FBA Fees Report: {payload}")

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 202:
        report_id = response.json().get("reportId")
        print(f"✅ Report requested successfully, Report ID: {report_id}")
        return report_id
    else:
        print(f"❌ Error requesting report: {response.status_code} - {response.text}")
        return None
    
    


def get_fba_report_status(access_token, report_id):
    """Check the status of the FBA Fee Report."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"

    headers = {"x-amz-access-token": access_token}

    print(f"📡 Checking status for report ID: {report_id}")

    for attempt in range(15):  # Retry up to 15 times
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            processing_status = data.get("processingStatus")
            document_id = data.get("reportDocumentId")

            print(f"🔍 Report Status: {processing_status}")

            if processing_status == "DONE":
                print(f"✅ Report Ready! Document ID: {document_id}")
                return document_id
            elif processing_status in ["FATAL", "CANCELLED"]:
                print("❌ Report generation failed.")
                return None
            else:
                print("⏳ Report is still processing, retrying in 30 seconds...")
                time.sleep(30)
        else:
            print(f"❌ Error checking report status: {response.text}")
            return None

    print("❌ Report processing did not complete in time.")
    return None


def download_fba_fees_report(access_token, document_id):
    """Download the FBA Fee Report from Amazon."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    print(f"📥 Downloading FBA Fees Report with Document ID: {document_id}")

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        report_url = response.json().get("url")
        if not report_url:
            print("❌ No download URL found!")
            return None

        print(f"🔗 Report Download URL: {report_url}")

        # Download the actual report
        report_response = requests.get(report_url)
        if report_response.status_code == 200:
            file_path = f"fba_fees_report_{document_id}.txt"
            with open(file_path, "wb") as f:
                f.write(report_response.content)

            print(f"✅ FBA Fees Report saved at {file_path}")
            return file_path
        else:
            print(f"❌ Error downloading report: {report_response.status_code}")
            return None
    else:
        print(f"❌ Failed to retrieve document metadata: {response.status_code}")
        return None


def process_fba_fees_report(file_path, selling_partner_id):
    """Process the FBA Fees Report and store data in the database."""
    print(f"📂 Processing FBA Fees Report: {file_path}")

    with open(file_path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    if not lines:
        print("❌ FBA Fees Report is empty!")
        return

    # Process CSV data (Assuming the report is CSV formatted)
    import csv
    reader = csv.reader(lines)
    headers = next(reader)

    print(f"📝 Headers: {headers}")

    data_to_store = []
    for row in reader:
        fee_data = dict(zip(headers, row))
        data_to_store.append(
            AmazonSettlementData(
                selling_partner_id=selling_partner_id,
                order_id=fee_data.get("Order ID"),
                type=fee_data.get("Fee Type"),
                amount=float(fee_data.get("Fee Amount", 0)),
                amazon_fee=float(fee_data.get("Amazon Fee", 0)),
                shipping_fee=float(fee_data.get("Shipping Fee", 0)),
                total_amount=float(fee_data.get("Total Amount", 0)),
                created_at=datetime.utcnow()
            )
        )

    # Store data in the database
    db.session.bulk_save_objects(data_to_store)
    db.session.commit()
    print(f"✅ Successfully stored {len(data_to_store)} FBA fee records.")





def detect_encoding(file_path):
    """Detect file encoding before reading."""
    with open(file_path, "rb") as f:
        raw_data = f.read(10000)  # Read first 10KB
    result = chardet.detect(raw_data)
    return result['encoding']





def process_settlement_report(file_path, selling_partner_id):
    """Process the FBA Fees Report and store data in the database."""
    file_path = decompress_gzip(file_path)  # ✅ Decompress if needed

    detected_encoding = detect_encoding(file_path)
    print(f"🔍 Detected file encoding: {detected_encoding}")

    try:
        with open(file_path, "r", encoding=detected_encoding, errors="replace") as file:
            lines = file.readlines()
    except Exception as e:
        print(f"❌ Error reading report file: {e}")
        return

    if not lines:
        print("❌ FBA Fees Report is empty!")
        return

    # Process CSV data
    import csv
    reader = csv.reader(lines)
    headers = next(reader, None)  # Get column names
    if not headers:
        print("❌ No headers found in file!")
        return

    print(f"📝 Headers: {headers}")

    data_to_store = []
    for row in reader:
        fee_data = dict(zip(headers, row))
        data_to_store.append(
            AmazonSettlementData(
                selling_partner_id=selling_partner_id,
                order_id=fee_data.get("Order ID"),
                type=fee_data.get("Fee Type"),
                amount=float(fee_data.get("Fee Amount", 0)),
                amazon_fee=float(fee_data.get("Amazon Fee", 0)),
                shipping_fee=float(fee_data.get("Shipping Fee", 0)),
                total_amount=float(fee_data.get("Total Amount", 0)),
                created_at=datetime.utcnow()
            )
        )

    db.session.bulk_save_objects(data_to_store)
    db.session.commit()
    print(f"✅ Successfully stored {len(data_to_store)} settlement records.")




def decompress_gzip(file_path):
    """Decompress GZIP file if needed."""
    if file_path.endswith(".gz"):
        decompressed_path = file_path.replace(".gz", ".txt")
        with gzip.open(file_path, "rb") as f_in:
            with open(decompressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        print(f"✅ Decompressed file saved as {decompressed_path}")
        return decompressed_path
    return file_path
