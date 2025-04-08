import requests
from datetime import datetime, timedelta  
from models import db, AmazonSettlementData
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





def request_sales_and_fees_report(selling_partner_id, access_token):
    start_date = (datetime.utcnow() - timedelta(days=365)).isoformat() + "Z"
    end_date = datetime.utcnow().isoformat() + "Z"

    url = "https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports"

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    body = {
        "reportType": ReportType.GET_SALES_AND_FEE_DETAILS_REPORT.value,
        "marketplaceIds": ["A1AM78C64UM0Y8"],
        "dataStartTime": start_date,
        "dataEndTime": end_date,
        "reportOptions": {}
    }

    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 202:
        report_id = response.json().get("reportId")
        print(f"📊 Report requested: {report_id}")
        return report_id
    else:
        print("❌ Failed to request report:", response.text)
        return None

def download_fba_fee_report(report_id, access_token):
    # Get document ID
    url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/reports/{report_id}"
    headers = {"x-amz-access-token": access_token}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print("❌ Failed to get report document:", response.text)
        return

    document_id = response.json().get("reportDocumentId")
    if not document_id:
        print("❌ No documentId found.")
        return

    # Get the download URL
    doc_url = f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}"
    download_url = requests.get(doc_url, headers=headers).json()["url"]

    # Download the report
    file_response = requests.get(download_url)
    file_path = f"{report_id}.csv"
    with open(file_path, "wb") as f:
        f.write(file_response.content)
    print(f"✅ Report saved to {file_path}")
    return file_path

def sync_fba_fees_last_year(selling_partner_id, access_token):
    report_id = request_sales_and_fees_report(selling_partner_id, access_token)
    if report_id:
        print("⏳ Waiting 60 seconds before downloading...")
        import time
        time.sleep(60)  # You can improve this with async polling
        return download_fba_fee_report(report_id, access_token)
