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
from utils import get_stored_tokens  # Ensure correct import

BASE_FINANCE_URL = "https://sellingpartnerapi-na.amazon.com/finances/v0"

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
    }
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        return response.json().get("Orders", [])
    else:
        print(f"❌ Error fetching orders: {response.status_code} - {response.text}")
        return []

def fetch_financial_events(selling_partner_id):
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No valid access token found"}

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }
    url = f"{BASE_FINANCE_URL}/financialEvents"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Error fetching financial events: {response.text}")
        return None

def store_financial_data(selling_partner_id, financial_events):
    if not financial_events or "FinancialEvents" not in financial_events:
        print("❌ No financial events to store.")
        return

    for event in financial_events.get("FinancialEvents", {}).get("ShipmentEventList", []):
        new_settlement = AmazonSettlementData(
            selling_partner_id=selling_partner_id,
            settlement_id=event.get("AmazonOrderId"),
            date_time=datetime.utcnow(),
            order_id=event.get("AmazonOrderId"),
            type=event.get("EventType", "UNKNOWN"),
            amount=event.get("Amount", {}).get("CurrencyAmount", 0),
            amazon_fee=event.get("FeeAmount", {}).get("CurrencyAmount", 0),
            shipping_fee=event.get("ShippingAmount", {}).get("CurrencyAmount", 0),
            total_amount=event.get("TotalAmount", {}).get("CurrencyAmount", 0)
        )
        db.session.add(new_settlement)
    
    db.session.commit()
    print("✅ Financial transactions saved successfully!")
