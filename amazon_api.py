import requests
from datetime import datetime, timedelta  
from models import db, AmazonSettlementData, AmazonInventoryItem, MarketplaceParticipation, Client
import gzip
import shutil
import csv
import os
import time
import json
import chardet
from sp_api.api import Reports
from sp_api.base import Marketplaces, ReportType
import threading
import gzip
import io
import pandas as pd
import psycopg2
from io import StringIO
from psycopg2.extras import execute_values
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError
import requests
import os
  




def fetch_orders_from_amazon(selling_partner_id, access_token, created_after):
    url = "https://sellingpartnerapi-na.amazon.com/orders/v0/orders"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    marketplace_id = get_marketplace_id_for_seller(selling_partner_id)

    params = {
        "MarketplaceIds": [marketplace_id],
        "CreatedAfter": created_after,
        "OrderStatuses": ["Shipped", "Unshipped", "Canceled"],
        "OptionalFields": ["AmazonFees", "ShippingPrice", "ShippingAddress"]  # ✅ Include these fields in API response
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





def fetch_order_items(access_token, amazon_order_id):
    """Fetches order item details for a given Amazon order."""
    
    url = f"https://sellingpartnerapi-na.amazon.com/orders/v0/orders/{amazon_order_id}/orderItems"
    
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }
    print(f"🔍 Fetching order items for Order ID: {amazon_order_id}")
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        order_items = data.get("payload", {}).get("OrderItems", [])
        formatted_items = []
        for item in order_items:
            formatted_items.append({
                "ASIN": item.get("ASIN"),
                "SellerSKU": item.get("SellerSKU"),
                "OrderItemId": item.get("OrderItemId"),
                "Title": item.get("Title"),
                "QuantityOrdered": item.get("QuantityOrdered"),
                "QuantityShipped": item.get("QuantityShipped", 0),
                "ItemPrice": item.get("ItemPrice", {}).get("Amount", "0"),
                "ShippingPrice": item.get("ShippingPrice", {}).get("Amount", "0"),
                "ItemTax": item.get("ItemTax", {}).get("Amount", "0"),
                "ShippingTax": item.get("ShippingTax", {}).get("Amount", "0"),
                "IsGift": item.get("IsGift", False),
                "Condition": item.get("ConditionId", "Unknown"),
            })
        print(f"✅ Order items retrieved: {len(formatted_items)} items")
        return formatted_items
    else:
        print(f"❌ Error fetching order items for {amazon_order_id}: {response.status_code} - {response.text}")
        return None








def request_partner_fees_report(selling_partner_id, access_token):
    start_date = (datetime.utcnow() - timedelta(days=365)).isoformat() + "Z"
    end_date = datetime.utcnow().isoformat() + "Z"

    marketplace_id = get_marketplace_id_for_seller(selling_partner_id)

    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    body = {
        "reportType": "GET_FLAT_FILE_PAYMENT_SETTLEMENT_DATA",
        "marketplaceIds": [marketplace_id],
        "dataStartTime": start_date,
        "dataEndTime": end_date,
        "reportOptions": {}
    }

    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 202:
        report_id = response.json().get("reportId")
        print(f"📊 Partner Fee Report requested: {report_id}")
        return report_id
    else:
        print("❌ Failed to request Partner Fees report:", response.text)
        return None







def request_settlement_report(access_token, selling_partner_id):
    start_date = (datetime.utcnow() - timedelta(days=90)).isoformat() + "Z"
    end_date = datetime.utcnow().isoformat() + "Z"

    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    marketplace_id = get_marketplace_id_for_seller(selling_partner_id)

    body = {
        "reportType": "GET_FLAT_FILE_ORDER_REPORT_DATA_INVOICING",
        "marketplaceIds": [marketplace_id],
        "dataStartTime": start_date,
        "dataEndTime": end_date
    }

    response = requests.post(url, headers=headers, json=body)

    if response.status_code == 202:
        report_id = response.json().get("reportId")
        print(f"✅ Report requested: {report_id}")
        return report_id
    else:
        print(f"❌ Report request failed: {response.status_code}")
        print(response.text)
        return None
    

def get_report_document_id(report_id, access_token):
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"
    headers = {
        "x-amz-access-token": access_token
    }

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        report_data = response.json()
        if report_data['processingStatus'] == "DONE":
            return report_data['reportDocumentId']
        else:
            print(f"⏳ Report not ready: {report_data['processingStatus']}")
            return None
    else:
        print("❌ Failed to get report status:", response.text)
        return None




def download_settlement_report(report_document_id, access_token, save_path='settlement_report.csv'):
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{report_document_id}"
    headers = {
        "x-amz-access-token": access_token
    }

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print("❌ Failed to get document download URL:", response.text)
        return None

    document_url = response.json().get("url")
    is_gzip = response.json().get("compressionAlgorithm") == "GZIP"

    raw_data = requests.get(document_url).content

    if is_gzip:
        with gzip.open(io.BytesIO(raw_data), 'rt', encoding='utf-8') as f:
            content = f.read()
    else:
        content = raw_data.decode('utf-8')

    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"✅ Settlement report saved to {save_path}")
    return save_path


def sync_settlement_report(selling_partner_id, access_token):
    report_id = request_settlement_report(access_token)
    if not report_id:
        return None
    
    time.sleep(30)  # Optional: initial delay to let Amazon start processing

    for _ in range(20):  # Retry up to ~5 mins (adjust as needed)
        document_id = get_report_document_id(report_id, access_token)
        if document_id:
            return download_settlement_report(document_id, access_token)
        time.sleep(15)

    print("❌ Timeout waiting for report to become available.")
    return None





EXPECTED_COLUMNS = [
    "settlement-id", "settlement-start-date", "settlement-end-date", "deposit-date",
    "total-amount", "currency", "transaction-type", "order-id", "merchant-order-id",
    "adjustment-id", "shipment-id", "marketplace-name", "amount-type", "amount-description",
    "amount", "fulfillment-id", "posted-date", "posted-date-time", "order-item-code",
    "merchant-order-item-id", "merchant-adjustment-item-id", "sku", "quantity-purchased",
    "promotion-id"
]



def preview_settlement_report_from_url(presigned_url):
    response = requests.get(presigned_url)
    if response.status_code != 200:
        print(f"❌ Failed to download report from {presigned_url}")
        return None

    content = response.content.decode("utf-8")

    # Split into lines and clean manually
    lines = content.strip().split("\n")
    header = lines[0].split("\t")
    rows = [line.split("\t") for line in lines[1:] if line.strip()]

    # Fill missing columns with None to avoid mismatch
    padded_rows = []
    for row in rows:
        if len(row) < len(header):
            row += [None] * (len(header) - len(row))
        padded_rows.append(row)

    df = pd.DataFrame(padded_rows, columns=header)

    # Clean up column names
    df.columns = [col.strip().lower().replace(" ", "-") for col in df.columns]

    # Optional: only return top rows
    return df.head(50).to_dict(orient="records")




def get_presigned_settlement_url(access_token, document_id):
    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/" + document_id
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()["url"]
    else:
        print("❌ Failed to get presigned URL", response.status_code)
        return None

def download_and_parse_settlement_report(presigned_url):
    response = requests.get(presigned_url)
    if response.status_code == 200:
        content = response.content.decode("utf-8")
        df = pd.read_csv(StringIO(content), sep="\t")  # << FIXED: treat it as tab-delimited
        df = df.dropna(axis=1, how="all")
        return df.head(10).to_dict(orient="records")
    else:
        print("❌ Failed to download settlement report", response.status_code)
        return None

def fetch_and_preview_latest_settlement_report(access_token, document_id):
    url = get_presigned_settlement_url(access_token, document_id)
    if url:
        return download_and_parse_settlement_report(url)
    return None



def save_settlement_report_to_v2_table(content, selling_partner_id):
    from io import StringIO
    import pandas as pd
    import psycopg2
    from psycopg2.extras import execute_values
    import os
    from dotenv import load_dotenv

    load_dotenv()

    df = pd.read_csv(StringIO(content), sep="\t", dtype=str)

    # Ensure all expected columns exist
    expected_cols = [
        "settlement-id", "settlement-start-date", "settlement-end-date", "deposit-date",
        "total-amount", "currency", "transaction-type", "order-id", "merchant-order-id",
        "adjustment-id", "shipment-id", "marketplace-name", "amount-type", "amount-description",
        "amount", "fulfillment-id", "posted-date", "posted-date-time", "order-item-code",
        "merchant-order-item-id", "merchant-adjustment-item-id", "sku", "quantity-purchased",
        "promotion-id"
    ]
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None

    df = df[expected_cols]

    # Convert dates
    for date_col in ["settlement-start-date", "settlement-end-date", "deposit-date", "posted-date", "posted-date-time"]:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # ✅ Replace NaN and NaT with None for PostgreSQL
    df = df.astype(object).where(pd.notnull(df), None)

    # Save to database
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS amazon_settlement_reports_v2 (
            id SERIAL PRIMARY KEY,
            selling_partner_id TEXT,
            settlement_id BIGINT,
            settlement_start_date TIMESTAMP,
            settlement_end_date TIMESTAMP,
            deposit_date TIMESTAMP,
            total_amount NUMERIC,
            currency TEXT,
            transaction_type TEXT,
            order_id TEXT,
            merchant_order_id TEXT,
            adjustment_id TEXT,
            shipment_id TEXT,
            marketplace_name TEXT,
            amount_type TEXT,
            amount_description TEXT,
            amount NUMERIC,
            fulfillment_id TEXT,
            posted_date DATE,
            posted_date_time TIMESTAMP,
            order_item_code TEXT,
            merchant_order_item_id TEXT,
            merchant_adjustment_item_id TEXT,
            sku TEXT,
            quantity_purchased INTEGER,
            promotion_id TEXT
        )
    """)

    execute_values(cur, """
        INSERT INTO amazon_settlement_reports_v2 (
            selling_partner_id, settlement_id, settlement_start_date, settlement_end_date,
            deposit_date, total_amount, currency, transaction_type, order_id,
            merchant_order_id, adjustment_id, shipment_id, marketplace_name,
            amount_type, amount_description, amount, fulfillment_id, posted_date,
            posted_date_time, order_item_code, merchant_order_item_id,
            merchant_adjustment_item_id, sku, quantity_purchased, promotion_id
        ) VALUES %s
    """, [
        (
            selling_partner_id,
            row["settlement-id"],
            row["settlement-start-date"],
            row["settlement-end-date"],
            row["deposit-date"],
            row["total-amount"],
            row["currency"],
            row["transaction-type"],
            row["order-id"],
            row["merchant-order-id"],
            row["adjustment-id"],
            row["shipment-id"],
            row["marketplace-name"],
            row["amount-type"],
            row["amount-description"],
            row["amount"],
            row["fulfillment-id"],
            row["posted-date"],
            row["posted-date-time"],
            row["order-item-code"],
            row["merchant-order-item-id"],
            row["merchant-adjustment-item-id"],
            row["sku"],
            row["quantity-purchased"],
            row["promotion-id"]
        ) for _, row in df.iterrows()
    ])

    conn.commit()
    cur.close()
    conn.close()







def request_all_listings_report(access_token):
    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"
    
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    payload = {
        "reportType": "GET_MERCHANT_LISTINGS_ALL_DATA",
        "marketplaceIds": ["A1AM78C64UM0Y8"],  # Amazon Mexico
        "reportOptions": {"custom": "false"}
    }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 202:
        print("✅ Report requested successfully:", response.json())
        return response.json()
    else:
        print("❌ Failed to request listings report", response.status_code)
        print(response.text)
        return None



def download_listings_report(report_id, access_token, max_wait=300, interval=15):
    """
    Polls for the report status until it is DONE, then downloads and decompresses
    (if needed) the report file associated with GET_MERCHANT_LISTINGS_ALL_DATA.
    """
    headers = {"x-amz-access-token": access_token}
    status_url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"
    
    waited = 0
    while waited < max_wait:
        status_resp = requests.get(status_url, headers=headers)
        if status_resp.status_code != 200:
            print("❌ Failed to fetch report status:", status_resp.text)
            return None
        
        data = status_resp.json()
        processing_status = data.get("processingStatus")
        
        if processing_status == "DONE":
            document_id = data.get("reportDocumentId")
            print(f"📄 Report is ready with document ID: {document_id}")
            break
        elif processing_status in ["CANCELLED", "FATAL"]:
            print(f"❌ Report was {processing_status}. Exiting.")
            return None
        
        print(f"⏳ Report not ready yet (status: {processing_status}), waiting {interval}s...")
        time.sleep(interval)
        waited += interval
    else:
        print("❌ Report did not become ready in time.")
        return None

    # Retrieve the document URL using the document ID
    doc_url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}"
    doc_response = requests.get(doc_url, headers=headers)
    if doc_response.status_code != 200:
        print("❌ Failed to fetch document URL:", doc_response.text)
        return None

    download_url = doc_response.json().get("url")
    if not download_url:
        print("❌ Download URL not found in document response.")
        return None

    # Download the file from the URL
    file_response = requests.get(download_url)
    if file_response.status_code != 200:
        print("❌ Failed to download report file:", file_response.text)
        return None

    compressed_stream = io.BytesIO(file_response.content)
    file_path = f"{report_id}.csv"
    
    try:
        # Attempt to decompress if the file is GZIP-compressed
        with gzip.GzipFile(fileobj=compressed_stream, mode='rb') as gz:
            with open(file_path, 'wb') as f_out:
                f_out.write(gz.read())
    except OSError:
        print("⚠️ Report is not GZIP, saving as-is.")
        with open(file_path, 'wb') as f_out:
            f_out.write(file_response.content)
    
    print(f"✅ Report saved to {file_path}")
    return file_path



# 1. Get Financial Events by Group ID
def get_financial_events_by_group(access_token, group_id):
    url = f"https://sellingpartnerapi-na.amazon.com/finances/v0/financialEventGroups/{group_id}/financialEvents"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Error fetching financial events for group {group_id}: {response.text}")
        return None


# 2. Get Financial Events for an Order
def get_financial_events_by_order(access_token, order_id):
    url = f"https://sellingpartnerapi-na.amazon.com/finances/v0/orders/{order_id}/financialEvents"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Error fetching financial events for order {order_id}: {response.text}")
        return None


# 3. Get Financial Events by Date Range
def get_financial_events_by_date_range(access_token, start_date, end_date):
    url = "https://sellingpartnerapi-na.amazon.com/finances/v0/financialEvents"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }
    params = {
        "PostedAfter": start_date,
        "PostedBefore": end_date
    }
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Error fetching financial events by date: {response.text}")
        return None




def list_financial_event_groups(access_token, start_date=None, end_date=None):
    url = "https://sellingpartnerapi-na.amazon.com/finances/v0/financialEventGroups"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    if not start_date:
        start_date = (datetime.utcnow() - timedelta(days=360)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not end_date:
        end_date = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "MaxResultsPerPage": 20,
        "FinancialEventGroupStartedAfter": start_date,
        "FinancialEventGroupStartedBefore": end_date
    }

    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        print("❌ Failed to list financial event groups:", response.text)
        return None





from sqlalchemy import and_
from flask import current_app
from datetime import datetime
from models import db, AmazonFinancialShipmentEvent


def save_shipment_events_by_group(access_token, selling_partner_id, group_id):
    data = get_financial_events_by_group(access_token, group_id)
    financial_events = data.get("payload", {}).get("FinancialEvents", {})

    shipment_events = financial_events.get("ShipmentEventList", [])
    service_events = financial_events.get("ServiceFeeEventList", [])
    ad_events = financial_events.get("ProductAdsPaymentEventList", [])
    refund_events = financial_events.get("RefundEventList", [])

    with current_app.app_context():

        for event in shipment_events:
            order_id = event.get("AmazonOrderId")
            marketplace = event.get("MarketplaceName")
            posted_date_raw = event.get("PostedDate")
            posted_date = datetime.fromisoformat(posted_date_raw.replace("Z", "")) if posted_date_raw else None

            for item in event.get("ShipmentItemList", []):
                sku = item.get("SellerSKU")

                # Skip if already exists
                if AmazonFinancialShipmentEvent.query.filter(
                    and_(
                        AmazonFinancialShipmentEvent.order_id == order_id,
                        AmazonFinancialShipmentEvent.posted_date == posted_date,
                        AmazonFinancialShipmentEvent.sku == sku
                    )
                ).first():
                    continue

                db.session.add(AmazonFinancialShipmentEvent(
                    selling_partner_id=selling_partner_id,
                    group_id=group_id,
                    order_id=order_id,
                    marketplace=marketplace,
                    posted_date=posted_date,
                    sku=sku,
                    quantity=item.get("QuantityShipped"),
                    principal=next((c["ChargeAmount"]["CurrencyAmount"] for c in item.get("ItemChargeList", []) if c["ChargeType"] == "Principal"), 0),
                    tax=next((c["ChargeAmount"]["CurrencyAmount"] for c in item.get("ItemChargeList", []) if c["ChargeType"] == "Tax"), 0),
                    shipping=next((c["ChargeAmount"]["CurrencyAmount"] for c in item.get("ItemChargeList", []) if c["ChargeType"] == "ShippingCharge"), 0),
                    fba_fee=next((f["FeeAmount"]["CurrencyAmount"] for f in item.get("ItemFeeList", []) if f["FeeType"] == "FBAPerUnitFulfillmentFee"), 0),
                    commission=next((f["FeeAmount"]["CurrencyAmount"] for f in item.get("ItemFeeList", []) if f["FeeType"] == "Commission"), 0),
                    ads_fee=sum(promo.get("PromotionAmount", {}).get("CurrencyAmount", 0) for promo in item.get("PromotionList", []))
                ))

        for event in service_events:
            posted_date = datetime.fromisoformat(event.get("PostedDate").replace("Z", "")) if event.get("PostedDate") else None
            order_id = event.get("AmazonOrderId")

            if AmazonFinancialShipmentEvent.query.filter_by(order_id=order_id, posted_date=posted_date).first():
                continue

            total_service_fee = 0
            storage_fee = 0
            for fee in event.get("FeeList", []):
                amount = float(fee["FeeAmount"]["CurrencyAmount"])
                total_service_fee += amount
                if fee["FeeType"] == "FBAStorageFee":
                    storage_fee += amount

            db.session.add(AmazonFinancialShipmentEvent(
                selling_partner_id=selling_partner_id,
                group_id=group_id,
                order_id=order_id,
                marketplace=event.get("MarketplaceId"),
                posted_date=posted_date,
                service_fee=total_service_fee,
                storage_fee=storage_fee
            ))

        for event in ad_events:
            posted_date = datetime.fromisoformat(event.get("PostedDate").replace("Z", "")) if event.get("PostedDate") else None
            order_id = event.get("AmazonOrderId")

            if AmazonFinancialShipmentEvent.query.filter_by(order_id=order_id, posted_date=posted_date).first():
                continue

            db.session.add(AmazonFinancialShipmentEvent(
                selling_partner_id=selling_partner_id,
                group_id=group_id,
                order_id=order_id,
                marketplace=event.get("MarketplaceId"),
                posted_date=posted_date,
                ads_fee=float(event.get("transactionValue", {}).get("CurrencyAmount", 0))
            ))

        for event in refund_events:
            posted_date = datetime.fromisoformat(event.get("PostedDate").replace("Z", "")) if event.get("PostedDate") else None
            order_id = event.get("AmazonOrderId")

            for item in event.get("ShipmentItemAdjustmentList", []):
                sku = item.get("SellerSKU")

                if AmazonFinancialShipmentEvent.query.filter(
                    and_(
                        AmazonFinancialShipmentEvent.order_id == order_id,
                        AmazonFinancialShipmentEvent.posted_date == posted_date,
                        AmazonFinancialShipmentEvent.sku == sku
                    )
                ).first():
                    continue

                db.session.add(AmazonFinancialShipmentEvent(
                    selling_partner_id=selling_partner_id,
                    group_id=group_id,
                    order_id=order_id,
                    marketplace=event.get("MarketplaceName"),
                    posted_date=posted_date,
                    sku=sku,
                    quantity=item.get("QuantityShipped"),
                    refund_fee=sum(fee["FeeAmount"]["CurrencyAmount"] for fee in item.get("ItemFeeAdjustmentList", []))
                ))

        db.session.commit()
        print(f"✅ Saved shipment, service, storage, ads, and refund fees for group {group_id}")





def request_unsuppressed_inventory_report(access_token, selling_partner_id):
    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    marketplace_id = get_marketplace_id_for_seller(selling_partner_id)

    body = {
        "reportType": "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
        "marketplaceIds": [marketplace_id]
    }

    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 202:
        return response.json().get("reportId")
    else:
        print(f"❌ Error requesting inventory report: {response.status_code} - {response.text}")
        return None





def save_unsuppressed_inventory_to_db(file_path, selling_partner_id):
    import csv
    from sqlalchemy.exc import SQLAlchemyError

    try:
        rows = None

        # First try UTF-8
        try:
            with open(file_path, mode='r', newline='', encoding='utf-8-sig') as csvfile:
                reader = csv.DictReader(csvfile)
                rows = [dict((k.lower(), v) for k, v in row.items()) for row in reader]
        except UnicodeDecodeError:
            print("⚠️ utf-8 decoding failed, trying latin-1...")
            with open(file_path, mode='r', newline='', encoding='latin-1') as csvfile:
                reader = csv.DictReader(csvfile)
                rows = [dict((k.lower(), v) for k, v in row.items()) for row in reader]

        if not rows:
            print("❌ No rows read from the CSV file.")
            return

        for row in rows:
            sku = row.get("seller-sku") or row.get("sku")
            if not sku:
                print("⚠️ Skipping row with missing SKU:", row)
                continue

            item = AmazonInventoryItem.query.filter_by(selling_partner_id=selling_partner_id, sku=sku).first()
            if not item:
                item = AmazonInventoryItem(
                    selling_partner_id=selling_partner_id,
                    sku=sku
                )
                db.session.add(item)

            item.asin = row.get("asin")
            item.fnsku = row.get("fnsku")
            item.product_name = row.get("product-name")
            item.condition = row.get("condition")
            item.fulfillment_center_id = row.get("fulfillment-center-id")
            item.detailed_disposition = row.get("detailed-disposition")
            item.inventory_country = row.get("country")
            item.inventory_status = row.get("status")
            try:
                item.quantity_available = int((row.get("quantity-available") or "0").strip())
            except ValueError:
                item.quantity_available = 0

        db.session.commit()
        print("✅ Unsuppressed inventory saved to AmazonInventoryItem table.")

    except FileNotFoundError:
        print(f"❌ File not found: {file_path}")
    except SQLAlchemyError as e:
        db.session.rollback()
        print(f"❌ Database error: {e}")
    except Exception as e:
        db.session.rollback()
        print(f"❌ Unexpected error: {e}")




def fetch_order_address(access_token, order_id):
    """Fetches shipping address for a specific Amazon order ID."""
    url = f"https://sellingpartnerapi-na.amazon.com/orders/v0/orders/{order_id}/address"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("payload", {}).get("ShippingAddress", {})
    else:
        print(f"❌ Failed to fetch address for order {order_id}: {response.status_code} - {response.text}")
        return {}




def download_inventory_report_file(report_id, access_token):
    try:
        url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{report_id}"
        headers = {
            "x-amz-access-token": access_token
        }
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"❌ Failed to fetch inventory report document: {response.status_code} - {response.text}")
            return None

        download_url = response.json().get("url")
        if not download_url:
            print("❌ Download URL not found in response.")
            return None

        download_response = requests.get(download_url)
        if download_response.status_code != 200:
            print(f"❌ Failed to download inventory file: {download_response.status_code} - {download_response.text}")
            return None

        file_name = f"{int(time.time())}.csv"
        with open(file_name, "wb") as f:
            f.write(download_response.content)

        print(f"✅ Inventory report saved to {file_name}")
        return file_name

    except Exception as e:
        print(f"❌ Unexpected error downloading inventory report: {e}")
        return None



def get_report_document_id(report_id, access_token):
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"
    headers = {
        "x-amz-access-token": access_token
    }

    while True:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"❌ Failed to check report status: {response.status_code} - {response.text}")
            return None

        report_data = response.json()
        processing_status = report_data.get("processingStatus")

        if processing_status == "DONE":
            return report_data.get("reportDocumentId")
        elif processing_status in ("CANCELLED", "FATAL"):
            print(f"❌ Report processing failed with status: {processing_status}")
            return None
        else:
            print(f"⏳ Report still {processing_status}, waiting 15 seconds...")
            time.sleep(15)





def save_marketplaces(selling_partner_id, access_token, client_id):
    try:
        url = "https://sellingpartnerapi-na.amazon.com/sellers/v1/marketplaceParticipations"
        headers = {
            "x-amz-access-token": access_token,
            "Authorization": f"Bearer {access_token}"
        }
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            print(f"❌ Failed to fetch marketplaces: {response.status_code} - {response.text}")
            return False

        data = response.json()
        marketplaces = data.get("payload", [])

        for mkt in marketplaces:
            marketplace_id = mkt.get("marketplace", {}).get("id")
            country_code = mkt.get("marketplace", {}).get("countryCode")
            is_participating = mkt.get("participation", {}).get("isParticipating", False)

            if marketplace_id and country_code:
                mp = MarketplaceParticipation(
                    client_id=client_id,
                    selling_partner_id=selling_partner_id,
                    marketplace_id=marketplace_id,
                    country_code=country_code,
                    is_participating=is_participating
                )
                db.session.add(mp)

        db.session.commit()
        print("✅ Marketplaces saved successfully.")
        return True

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error saving marketplaces: {e}")
        return False




def get_marketplace_id_for_seller(selling_partner_id):
    try:
        client = Client.query.filter_by(selling_partner_id=selling_partner_id).first()
        if not client:
            print(f"❌ No client found for selling_partner_id: {selling_partner_id}")
            return None

        marketplace = MarketplaceParticipation.query.filter_by(client_id=client.id, is_participating=True).first()
        if marketplace:
            return marketplace.marketplace_id
        else:
            print(f"❌ No active marketplace found for client_id: {client.id}")
            return None

    except Exception as e:
        print(f"❌ Error retrieving marketplace_id: {e}")
        return None



def fetch_marketplaces_for_seller(access_token):
    """Fetch seller's marketplace participations."""
    url = "https://sellingpartnerapi-na.amazon.com/sellers/v1/marketplaceParticipations"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        participations = response.json().get("payload", [])
        marketplaces = []
        for p in participations:
            marketplace = p.get("marketplace", {})
            if marketplace:
                marketplaces.append({
                    "marketplace_id": marketplace.get("id"),
                    "country_code": marketplace.get("countryCode")
                })
        return marketplaces
    else:
        print(f"❌ Failed to fetch marketplaces: {response.status_code} - {response.text}")
        return []




def save_inventory_items(data_list, selling_partner_id):
    for row in data_list:
        sku = row.get("sku")
        if not sku:
            print("⚠️ Missing SKU in row, skipping:", row)
            continue

        item = AmazonInventoryItem.query.filter_by(selling_partner_id=selling_partner_id, sku=sku).first()

        if not item:
            item = AmazonInventoryItem(selling_partner_id=selling_partner_id, sku=sku)
            db.session.add(item)

        # Update fields
        item.asin = row.get("asin")
        item.fnsku = row.get("fnsku")
        item.product_name = row.get("product_name")
        item.condition = row.get("condition")
        item.fulfillment_center_id = row.get("fulfillment_center_id")  # Optional if you add it later
        item.detailed_disposition = row.get("detailed_disposition")    # Optional if present
        item.inventory_country = row.get("inventory_country")          # Optional if present
        item.inventory_status = row.get("inventory_status")            # Optional if present
        item.quantity_available = int(row.get("afn_total_quantity", "0") or 0)
        item.last_updated = datetime.utcnow()

    try:
        db.session.commit()
        print(f"✅ Saved {len(data_list)} inventory items for seller {selling_partner_id}")
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error saving inventory items: {e}")



def sync_unsuppressed_inventory_report(selling_partner_id):
    from app import get_stored_tokens
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        print(f"❌ No access token found for {selling_partner_id}")
        return None

    # Step 1: Request the report
    report_id = request_unsuppressed_inventory_report(access_token, selling_partner_id)
    if not report_id:
        print("❌ Could not request inventory report.")
        return None

    # Step 2: Wait for report document ID
    document_id = get_report_document_id(report_id, access_token)
    if not document_id:
        print("❌ Could not get document ID for report.")
        return None

    # Step 3: Download the file
    file_path = download_inventory_report_file(document_id, access_token)
    if not file_path:
        print("❌ Could not download inventory report file.")
        return None

    # Step 4: Parse and save to DB
    save_unsuppressed_inventory_to_db(file_path, selling_partner_id)
    return file_path

