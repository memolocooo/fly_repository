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
import logging



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



BASE_FINANCE_URL = "https://sellingpartnerapi-na.amazon.com/finances/v0"

def fetch_financial_events(selling_partner_id):
    logging.info(f"Fetching financial events for selling_partner_id: {selling_partner_id}")
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        logging.error("No valid access token found")
        return {"error": "No valid access token found"}

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    one_year_ago = (datetime.utcnow() - timedelta(days=365)).isoformat()
    url = f"{BASE_FINANCE_URL}/financialEvents?PostedAfter={one_year_ago}"
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        logging.info("Successfully fetched financial events")
        return response.json()
    else:
        logging.error(f"Error fetching financial events: {response.text}")
        return None