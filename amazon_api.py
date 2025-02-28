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
import threading



def fetch_orders_from_amazon(selling_partner_id, access_token, created_after):
    url = "https://sellingpartnerapi-na.amazon.com/orders/v0/orders"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    params = {
        "MarketplaceIds": ["A1AM78C64UM0Y8"],
        "CreatedAfter": created_after,
        "OrderStatuses": ["Shipped", "Unshipped", "Canceled"],
        "OptionalFields": ["AmazonFees", "ShippingPrice"]  # ✅ Include these fields in API response
    }

    print(f"🔍 Fetching orders for seller {selling_partner_id} since {params['CreatedAfter']}")

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        orders = response.json()
        print(f"✅ Amazon API Response: {orders}")

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
        "reportType": ReportType.GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL,  # ✅ Using Enum Instead of Hardcoded String
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
    

import time
import requests

def get_fba_report_status(access_token, report_id):
    """Check the status of an Amazon FBA Fees Report and wait until it's ready."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    retries = 0
    max_retries = 20  # Maximum attempts (adjust as needed)
    wait_time = 30  # Start with 30 seconds wait

    while retries < max_retries:
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            report_data = response.json()
            status = report_data.get("processingStatus")

            print(f"🔍 Report Status: {status}")

            if status == "DONE":
                document_id = report_data.get("reportDocumentId")
                print(f"✅ Report is ready! Document ID: {document_id}")
                return document_id  # Now the report is ready, return the document ID

            elif status in ["CANCELLED", "FATAL"]:
                print("❌ Report failed to process.")
                return None

        else:
            print(f"❌ Error checking report status: {response.status_code} - {response.text}")

        retries += 1
        print(f"⏳ Report is still processing, retrying in {wait_time} seconds...")
        time.sleep(wait_time)
        wait_time *= 1.5  # Increase wait time for each retry

    print("🚨 Report did not finish in time. Try again later.")
    return None








def download_fba_fees_report(access_token, document_id):
    """Download and decode the FBA Fees Report from Amazon SP-API."""
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}"
    headers = {"x-amz-access-token": access_token}

    print(f"📥 Downloading FBA Fees Report with Document ID: {document_id}")

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"❌ Error fetching report metadata: {response.status_code} - {response.text}")
        return None

    report_url = response.json().get("url")
    if not report_url:
        print("❌ No download URL found!")
        return None

    print(f"🔗 Report Download URL: {report_url}")

    # Download the actual report
    report_response = requests.get(report_url)
    if report_response.status_code != 200:
        print(f"❌ Error downloading report: {report_response.status_code}")
        return None

    # Save to temporary file
    temp_file_path = f"fba_fees_report_{document_id}.txt"
    with open(temp_file_path, "wb") as f:
        f.write(report_response.content)

    print(f"✅ FBA Fees Report downloaded and saved at {temp_file_path}")

    # Ensure it's decompressed if needed
    decompressed_file = decompress_gzip(temp_file_path)

    return decompressed_file  # Return the final file path



def save_settlement_data(fee_data, selling_partner_id):
    """Saves settlement data to the database."""
    try:
        data_entry = AmazonSettlementData(
            selling_partner_id=selling_partner_id,
            order_id=fee_data.get("Order ID"),
            type=fee_data.get("Fee Type"),
            amount=float(fee_data.get("Fee Amount", 0) or 0),
            amazon_fee=float(fee_data.get("Amazon Fee", 0) or 0),
            shipping_fee=float(fee_data.get("Shipping Fee", 0) or 0),
            total_amount=float(fee_data.get("Total Amount", 0) or 0),
            created_at=datetime.utcnow()
        )
        db.session.add(data_entry)
        db.session.commit()
        print(f"✅ Saved settlement data for Order ID: {fee_data.get('Order ID')}")
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error saving settlement data: {e}")




def process_fba_fees_report(report_path, selling_partner_id):
    """Process the downloaded FBA Fees Report and save data."""
    try:
        with open(report_path, mode="r", encoding="utf-8") as file:
            reader = csv.reader(file)
            headers = next(reader)  # Read the first row as headers
            
            print(f"📂 Report Headers: {headers}")  # ✅ Print headers

            for row in reader:
                fee_data = dict(zip(headers, row))  # Map row data to headers
                
                print(f"🔍 Extracted Data: {fee_data}")  # ✅ Print extracted data

                # ✅ Ensure `save_settlement_data` is called correctly
                save_settlement_data(fee_data, selling_partner_id)

        print("✅ Report processing complete.")
    
    except Exception as e:
        print(f"❌ Error processing FBA Fees Report: {e}")








def detect_encoding(file_path):
    """Detect file encoding before reading."""
    with open(file_path, "rb") as f:
        raw_data = f.read(100000)  # ✅ Increase to 100KB for better detection
    result = chardet.detect(raw_data)
    detected_encoding = result["encoding"]

    if not detected_encoding:
        detected_encoding = "utf-8"  # ✅ Default to UTF-8 if detection fails

    print(f"🔍 Detected Encoding: {detected_encoding}")
    return detected_encoding





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
    return file_path  # ✅ Return original if no decompression needed


