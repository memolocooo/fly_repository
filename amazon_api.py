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


def fetch_financial_events(selling_partner_id, access_token, posted_after):
    """Fetch financial events from Amazon SP-API"""
    logging.info(f"📡 Fetching financial events for seller: {selling_partner_id} since {posted_after}")

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    url = f"{BASE_FINANCE_URL}/financialEvents?PostedAfter={posted_after}"
    response = requests.get(url, headers=headers)

    try:
        if response.status_code == 200:
            data = response.json()
            logging.info(f"✅ Amazon API Response: {json.dumps(data, indent=2)[:500]}")  # ✅ Log first 500 chars
            return data  # ✅ Ensure we return the full data, not an empty list

        else:
            logging.error(f"❌ Error fetching financial events: {response.status_code} - {response.text}")
            return None
    except json.JSONDecodeError:
        logging.error("❌ JSON Parsing Error - Invalid JSON Response")
        return None
    except Exception as e:
        logging.error(f"❌ Unexpected Exception: {str(e)}")
        return None


BASE_SHIPPING_URL = "https://sellingpartnerapi-na.amazon.com/shipping/v1"

def fetch_shipping_data(selling_partner_id, access_token):
    """Fetch shipping data from Amazon SP-API."""
    logging.info(f"📡 Fetching shipping data for seller: {selling_partner_id}")

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    url = f"{BASE_SHIPPING_URL}/shipments"

    response = requests.get(url, headers=headers)

    try:
        if response.status_code == 200:
            data = response.json()
            logging.info(f"✅ Amazon API Response: {json.dumps(data, indent=2)[:500]}")  # Log first 500 chars
            return data

        else:
            logging.error(f"❌ Error fetching shipping data: {response.status_code} - {response.text}")
            return None
    except json.JSONDecodeError:
        logging.error("❌ JSON Parsing Error - Invalid JSON Response")
        return None
    except Exception as e:
        logging.error(f"❌ Unexpected Exception: {str(e)}")
        return None


BASE_FEES_URL = "https://sellingpartnerapi-na.amazon.com/fees/v0"

def fetch_fees_data(selling_partner_id, access_token):
    """Fetch fees data from Amazon SP-API."""
    logging.info(f"📡 Fetching fees data for seller: {selling_partner_id}")

    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json"
    }

    url = f"{BASE_FEES_URL}/listings/fees"

    response = requests.get(url, headers=headers)

    try:
        if response.status_code == 200:
            data = response.json()
            logging.info(f"✅ Amazon API Response: {json.dumps(data, indent=2)[:500]}")  # Log first 500 chars
            return data

        else:
            logging.error(f"❌ Error fetching fees data: {response.status_code} - {response.text}")
            return None
    except json.JSONDecodeError:
        logging.error("❌ JSON Parsing Error - Invalid JSON Response")
        return None
    except Exception as e:
        logging.error(f"❌ Unexpected Exception: {str(e)}")
        return None



