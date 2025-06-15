import os
import pandas as pd  # Add this at the top if you haven't
import csv
import uuid
import requests
from io import StringIO
import redis
import logging
import glob
import json
import time
from flask import Flask, session, redirect, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from datetime import timedelta, datetime  # Instead of `import datetime`
from flask_cors import CORS
from models import db, AmazonOAuthTokens, AmazonOrders, AmazonSettlementData,AmazonOrderItem, AmazonFinancialShipmentEvent, AmazonInventoryItem  # Use the correct class name
import psycopg2
from psycopg2.extras import execute_values
from amazon_api import fetch_orders_from_amazon,  fetch_order_items, sync_settlement_report, fetch_and_preview_latest_settlement_report, get_presigned_settlement_url, save_settlement_report_to_v2_table, request_all_listings_report, download_listings_report, list_financial_event_groups, fetch_order_address, download_inventory_report_file, save_unsuppressed_inventory_to_db, get_report_document_id, save_unsuppressed_inventory_report, generate_merchant_listings_report,request_returns_report, get_marketplace_id_for_seller  #Adjust module name if needed
from dateutil.relativedelta import relativedelta

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")
redis_client = redis.StrictRedis(host="localhost", port=6379, decode_responses=True)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL")
redis_client = redis.StrictRedis.from_url(REDIS_URL, decode_responses=True)

# Initialize database
db.init_app(app)
migrate = Migrate(app, db)

# Amazon OAuth Variables
LWA_APP_ID = os.getenv("LWA_APP_ID")
LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
AUTH_URL = os.getenv("AUTH_URL")
TOKEN_URL = os.getenv("TOKEN_URL")
SP_API_BASE_URL = os.getenv("SP_API_BASE_URL")
APP_ID = os.getenv("APP_ID")

# Ensure database connection
try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.close()
    print("✅ Database connection successful!")
except Exception as e:
    print(f"❌ Database connection failed: {e}")

one_year_ago = datetime.utcnow() - timedelta(days=365)

def refresh_access_token(selling_partner_id):
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()
    if not token_entry:
        return None

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token_entry.refresh_token,
        "client_id": LWA_APP_ID,
        "client_secret": LWA_CLIENT_SECRET
    }

    response = requests.post(TOKEN_URL, data=payload)
    data = response.json()

    if "access_token" in data:
        token_entry.access_token = data["access_token"]
        token_entry.expires_at = datetime.utcnow() + timedelta(seconds=data["expires_in"])
        db.session.commit()
        return data["access_token"]

    print("❌ Failed to refresh token:", data)
    return None

def exchange_auth_code_for_tokens(auth_code):
    """Exchanges auth code for access & refresh tokens from Amazon SP-API."""
    payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": LWA_APP_ID,
        "client_secret": LWA_CLIENT_SECRET,
        "redirect_uri": os.getenv("REDIRECT_URI"),
    }
    
    response = requests.post(TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Error fetching tokens: {response.text}")
        return None
 
def get_stored_tokens(selling_partner_id):
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()
    if not token_entry:
        return None

    if token_entry.expires_at and datetime.utcnow() >= token_entry.expires_at:
        print(f"🔄 Token expired for {selling_partner_id}, refreshing...")
        return refresh_access_token(selling_partner_id)

    return token_entry.access_token

def save_oauth_tokens(selling_partner_id, access_token, refresh_token, expires_in):
    try:
        print(f"🔄 Attempting to save OAuth Tokens for {selling_partner_id}")  # Debug log
        
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Ensure table exists (Replace 'amazon_tokens' with 'amazon_oauth_tokens' if incorrect)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS amazon_oauth_tokens (
            id SERIAL PRIMARY KEY,
            selling_partner_id TEXT UNIQUE,
            access_token TEXT,
            refresh_token TEXT,
            expires_in INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
        """)

        # Calculate token expiration time
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        # Insert or update the token
        cur.execute("""
        INSERT INTO amazon_oauth_tokens (selling_partner_id, access_token, refresh_token, expires_in, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (selling_partner_id) DO UPDATE 
        SET access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            expires_in = EXCLUDED.expires_in,
            expires_at = EXCLUDED.expires_at
        """, (selling_partner_id, access_token, refresh_token, expires_in, expires_at))

        # Commit the transaction
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Tokens saved successfully for {selling_partner_id}")

    except psycopg2.Error as e:
        print(f"❌ Database Error while saving tokens: {e}")
        if conn:
            conn.rollback()  # Ensure rollback on failure

    finally:
        if conn:
            conn.close()


def store_orders_in_db(selling_partner_id, orders):
    """Save Amazon order fields into PostgreSQL, including fees, shipping price, asin, and sku."""

    saved_count = 0

    for order in orders:
        # ✅ Skip FBM (MFN) orders, only keep FBA (AFN)
        if order.get("FulfillmentChannel") != "AFN":
            continue

        order_id = order.get("AmazonOrderId")
        marketplace_id = order.get("MarketplaceId")
        amazon_order_id = order.get("AmazonOrderId")
        number_of_items_shipped = order.get("NumberOfItemsShipped", 0)
        order_status = order.get("OrderStatus", "UNKNOWN")
        total_amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
        currency = order.get("OrderTotal", {}).get("CurrencyCode")
        purchase_date = order.get("PurchaseDate")

        amazon_fees = float(order.get("AmazonFees", {}).get("Amount", 0) or 0)
        shipping_price = float(order.get("ShippingPrice", {}).get("Amount", 0) or 0)

        # ✅ Default ASIN and SKU values
        asin = None
        seller_sku = None

        # ✅ Extract shipping address
        shipping_address = order.get("ShippingAddress", {})
        city = shipping_address.get("City")
        state = shipping_address.get("StateOrRegion")
        postal_code = shipping_address.get("PostalCode")
        country_code = shipping_address.get("CountryCode")

        # ✅ Try to get ASIN and SKU from DB if the items exist
        first_item = AmazonOrderItem.query.filter_by(amazon_order_id=amazon_order_id).first()
        if first_item:
            asin = first_item.asin
            seller_sku = first_item.seller_sku

        # ✅ Check for duplicates
        existing_order = AmazonOrders.query.filter_by(order_id=order_id).first()

        if not existing_order:
            new_order = AmazonOrders(
                order_id=order_id,
                amazon_order_id=amazon_order_id,
                marketplace_id=marketplace_id,
                selling_partner_id=selling_partner_id,
                number_of_items_shipped=number_of_items_shipped,
                order_status=order_status,
                total_amount=total_amount,
                currency=currency,
                purchase_date=datetime.strptime(purchase_date, "%Y-%m-%dT%H:%M:%SZ"),
                amazon_fees=amazon_fees,
                shipping_price=shipping_price,
                asin=asin,
                seller_sku=seller_sku,
                created_at=datetime.utcnow(),
                city=city,
                state=state,
                postal_code=postal_code,
                country_code=country_code

            )

            db.session.add(new_order)
            saved_count += 1

    db.session.commit()
    print(f"✅ {saved_count} FBA orders saved to database with Amazon fees, shipping, ASIN & SKU.")

 


import base64

@app.route("/start-oauth")
def start_oauth():
    # ✅ ID real de tu app registrada en Amazon Seller Central
    application_id = "amzn1.sp.solution.7c0c9136-f04c-46e2-b4f0-0c18a7996a24"
    redirect_uri = "https://render-webflow-restless-river-7629.fly.dev/callback"

    marketplace_id = request.args.get("marketplace_id")
    if not marketplace_id:
        return "Missing marketplace_id", 400

    # Codifica el estado como JSON en base64 para incluir el marketplace_id
    state_data = {
        "uuid": str(uuid.uuid4()),
        "marketplace_id": marketplace_id
    }
    encoded_state = base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()

    # Redirige al usuario al consentimiento de Amazon
    redirect_url = (
        f"https://sellercentral.amazon.com/apps/authorize/consent?"
        f"application_id={application_id}&"
        f"state={encoded_state}&"
        f"redirect_uri={redirect_uri}&"
        f"scope=sellingpartnerapi::notifications&version=beta"
    )

    return redirect(redirect_url)





@app.route("/callback")
def callback():
    from models import db, Client, MarketplaceParticipation
    from amazon_api import fetch_marketplaces_for_seller
    import requests
    import base64
    import json

    try:
        auth_code = request.args.get("spapi_oauth_code")
        selling_partner_id = request.args.get("selling_partner_id")
        state_encoded = request.args.get("state")

        print(f"🚀 Received auth_code: {auth_code}")
        print(f"🔍 Received selling_partner_id: {selling_partner_id}")

        # Decode the state to retrieve marketplace_id
        if state_encoded:
            try:
                decoded_state = base64.urlsafe_b64decode(state_encoded).decode()
                state_data = json.loads(decoded_state)
                marketplace_id = state_data.get("marketplace_id")
                print(f"🌍 Received marketplace_id: {marketplace_id}")
            except Exception as decode_err:
                print(f"❌ Failed to decode state: {decode_err}")
                marketplace_id = None
        else:
            print("⚠️ No state parameter provided")
            marketplace_id = None

        if not auth_code or not selling_partner_id:
            return jsonify({"error": "Missing required parameters."}), 400

        # Step 1: Exchange auth_code for access_token
        token_data = exchange_auth_code_for_tokens(auth_code)
        if not token_data:
            return jsonify({"error": "Failed to exchange authorization code."}), 500

        # Step 2: Save tokens
        save_oauth_tokens(
            selling_partner_id,
            token_data["access_token"],
            token_data["refresh_token"],
            token_data["expires_in"]
        )
        print(f"✅ Tokens saved successfully for {selling_partner_id}")

        # Step 3: Create or update Client
        client = Client.query.filter_by(selling_partner_id=selling_partner_id).first()
        if not client:
            client = Client(
                selling_partner_id=selling_partner_id,
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                region="na"
            )
            db.session.add(client)
            db.session.commit()
            print(f"✅ New Client created for {selling_partner_id}")
        else:
            print(f"✅ Existing Client found for {selling_partner_id}")

        # Step 4: Fetch and Save Marketplaces
        try:
            marketplaces = fetch_marketplaces_for_seller(token_data["access_token"])
            if marketplaces:
                for marketplace in marketplaces:
                    exists = MarketplaceParticipation.query.filter_by(
                        client_id=client.id,
                        selling_partner_id=selling_partner_id,
                        marketplace_id=marketplace["marketplace_id"]
                    ).first()

                    if not exists:
                        db.session.add(MarketplaceParticipation(
                            client_id=client.id,
                            selling_partner_id=selling_partner_id,
                            marketplace_id=marketplace["marketplace_id"],
                            country_code=marketplace["country_code"],
                            is_participating=True
                        ))
                db.session.commit()
                print(f"✅ Saved {len(marketplaces)} marketplaces for {selling_partner_id}")
            else:
                print("⚠️ No marketplaces returned")
        except Exception as e:
            print(f"❌ Error fetching marketplaces: {e}")

        # Step 5: Trigger data fetch (includes selling_partner_id and marketplace_id)
        try:
            payload = {
                "selling_partner_id": selling_partner_id,
                "marketplace_id": marketplace_id
            }
            requests.post(
                "https://render-webflow-restless-river-7629.fly.dev/api/start-data-fetch",
                json=payload,
                timeout=5
            )
            print("✅ Data fetching triggered")
        except Exception as e:
            print(f"⚠️ Failed to trigger data fetch: {e}")

        # Step 6: Redirect to dashboard with both identifiers
        return redirect(f"https://guillermos-amazing-site-b0c75a.webflow.io/dashboard?selling_partner_id={selling_partner_id}&marketplace_id={marketplace_id}")

    except Exception as e:
        print(f"❌ Error in callback: {e}")
        return jsonify({"error": "Internal server error."}), 500





@app.route('/api/start-data-fetch', methods=["POST"])
def start_data_fetch():
    from amazon_api import (
        fetch_orders_from_amazon,
        fetch_order_items,
        save_shipment_events_by_group,
        list_financial_event_groups
    )
    from models import db, AmazonOrders, AmazonOrderItem, AmazonFinancialShipmentEvent
    from app import store_order_items_in_db  # ensure path is correct

    data = request.get_json()
    selling_partner_id = data.get("selling_partner_id")
    marketplace_id = data.get("marketplace_id")

    if not selling_partner_id or not marketplace_id:
        return jsonify({"error": "Missing selling_partner_id or marketplace_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    try:
        print(f"🚀 Starting full data fetch for {selling_partner_id} - {marketplace_id}")

        one_year_ago = (datetime.utcnow() - timedelta(days=365)).isoformat()

        # 1️⃣ Fetch and store orders
        orders = fetch_orders_from_amazon(
            selling_partner_id, access_token, one_year_ago, marketplace_id
        )
        if orders:
            for order in orders:
                amazon_order_id = order["AmazonOrderId"]
                existing = AmazonOrders.query.filter_by(amazon_order_id=amazon_order_id).first()
                if not existing:
                    db.session.add(AmazonOrders(
                        selling_partner_id=selling_partner_id,
                        order_id=amazon_order_id,
                        amazon_order_id=amazon_order_id,
                        purchase_date=order.get("PurchaseDate"),
                        order_status=order.get("OrderStatus"),
                        marketplace_id=order.get("MarketplaceId"),
                        number_of_items_shipped=order.get("NumberOfItemsShipped", 0),
                        total_amount=order.get("OrderTotal", {}).get("Amount"),
                        currency=order.get("OrderTotal", {}).get("CurrencyCode"),
                        city=order.get("ShippingAddress", {}).get("City"),
                        state=order.get("ShippingAddress", {}).get("StateOrRegion"),
                        postal_code=order.get("ShippingAddress", {}).get("PostalCode"),
                        country_code=order.get("ShippingAddress", {}).get("CountryCode")
                    ))
            db.session.commit()
            print(f"✅ Stored {len(orders)} orders.")
        else:
            print("⚠️ No orders found.")

        # 2️⃣ Fetch order items for recent orders
        one_year_ago_dt = datetime.utcnow() - timedelta(days=365)
        recent_orders = AmazonOrders.query.filter(
            AmazonOrders.selling_partner_id == selling_partner_id,
            AmazonOrders.purchase_date >= one_year_ago_dt,
            AmazonOrders.marketplace_id == marketplace_id
        ).all()

        item_count = 0
        for order in recent_orders:
            amazon_order_id = order.amazon_order_id
            print(f"📦 Fetching items for order {amazon_order_id}")
            try:
                order_items = fetch_order_items(access_token, amazon_order_id)
                if order_items:
                    store_order_items_in_db(selling_partner_id, {
                        "amazon_order_id": amazon_order_id,
                        "order_items": order_items
                    })
                    item_count += len(order_items)
                else:
                    print(f"⚠️ No items returned for {amazon_order_id}")
            except Exception as e:
                print(f"❌ Failed to fetch items for {amazon_order_id}: {e}")
                continue

        print(f"✅ Saved {item_count} order items.")

        # 3️⃣ Fetch shipment events
        financial_groups = list_financial_event_groups(access_token)
        if financial_groups:
            for group in financial_groups.get("payload", {}).get("FinancialEventGroupList", []):
                group_id = group.get("FinancialEventGroupId")
                if group_id:
                    save_shipment_events_by_group(
                        access_token, selling_partner_id, group_id
                    )

        print(f"✅ Finished full data sync for {selling_partner_id} in {marketplace_id}")
        return jsonify({"message": "✅ Data fetching and saving completed."})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error during start-data-fetch: {e}")
        return jsonify({"error": "Failed to fetch data."}), 500




@app.route("/get-orders", methods=["GET"])
def get_orders():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # ✅ Step 1: Check Redis Cache First (Optimize Performance)
    cache_key = f"orders:{selling_partner_id}"
    cached_orders = redis_client.get(cache_key)
    if cached_orders:
        return jsonify(json.loads(cached_orders))

    # ✅ Step 2: Fetch Orders from Database First
    one_year_ago = datetime.utcnow() - timedelta(days=365)
    orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,  
        AmazonOrders.purchase_date >= one_year_ago
    ).all()

    if orders:
        orders_data = [order.to_dict() for order in orders]
        redis_client.setex(cache_key, 900, json.dumps(orders_data))
        return jsonify(orders_data)

    # ✅ Step 3: If No Orders in DB, Fetch from Amazon API
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No valid access token found"}), 400

    created_after = one_year_ago.isoformat()
    fetched_orders = fetch_orders_from_amazon(selling_partner_id, access_token, created_after)

    if not fetched_orders:
        return jsonify({"error": "No orders found in Amazon API"}), 404

    # ✅ Step 4: Save Orders to Database
    store_orders_in_db(selling_partner_id, fetched_orders)

    # ✅ Step 5: Return the Newly Fetched Orders
    orders_data = [order.to_dict() for order in fetched_orders]
    redis_client.setex(cache_key, 900, json.dumps(orders_data))  # Cache results
    return jsonify(orders_data), 200




@app.route("/api/orders", methods=["GET"])
def get_amazon_orders():
    orders = AmazonOrders.query.all()
    orders_data = [
        {
            "marketplace_id": order.marketplace_id,
            "total_amount": float(order.total_amount) if order.total_amount else 0,
            "order_status": order.order_status,
            "purchase_date": order.purchase_date.strftime('%Y-%m-%d') if order.purchase_date else None,
        }
        for order in orders
    ]
    return jsonify(orders_data)






@app.route("/")
def home():
    return "Flask is running on Fly.io!"



# ✅ Ensure logs go to stdout for Fly.io
logging.basicConfig(level=logging.INFO)

@app.before_request
def log_request():
    """Log incoming HTTP requests."""
    logging.info(f"📥 Request: {request.method} {request.url}")



@app.after_request
def log_response(response):
    """Log outgoing HTTP responses."""
    logging.info(f"📤 Response: {response.status_code} {request.url}")
    return response



def store_order_items_in_db(selling_partner_id, order_data):
    """Save order items using SQLAlchemy ORM with marketplace name."""
    amazon_order_id = order_data.get("amazon_order_id")
    order_items = order_data.get("order_items", [])

    if not amazon_order_id or not order_items:
        print("⚠️ No order items to save.")
        return

    # ✅ Fetch the related order to get purchase_date and marketplace_id
    order_record = AmazonOrders.query.filter_by(amazon_order_id=amazon_order_id).first()
    purchase_date = order_record.purchase_date if order_record else None
    marketplace_id = order_record.marketplace_id if order_record else None

    # ✅ Map marketplace_id to human-friendly name
    marketplace_map = {
        "A1AM78C64UM0Y8": "Amazon.com.mx",
        "ATVPDKIKX0DER": "Amazon.com",
        "A1PA6795UKMFR9": "Amazon.de",
        "A1RKKUPIHCS9HS": "Amazon.es",
        "A1F83G8C2ARO7P": "Amazon.co.uk",
        "A21TJRUUN4KGV": "Amazon.in",
        "A1VC38T7YXB528": "Amazon.co.jp",
        "AAHKV2X7AFYLW": "Amazon.cn",
        "APJ6JRA9NG5V4": "Amazon.ca"
    }
    marketplace_name = marketplace_map.get(marketplace_id, marketplace_id)

    try:
        for item in order_items:
            order_item_id = item.get("OrderItemId")

            existing_item = AmazonOrderItem.query.filter_by(order_item_id=order_item_id).first()
            if existing_item:
                print(f"🔁 Skipping duplicate item: {order_item_id}")
                continue

            new_item = AmazonOrderItem(
                selling_partner_id=selling_partner_id,
                amazon_order_id=amazon_order_id,
                order_item_id=order_item_id,
                asin=item.get("ASIN"),
                title=item.get("Title"),
                seller_sku=item.get("SellerSKU"),
                condition=item.get("Condition", "Unknown"),
                is_gift=item.get("IsGift", "false") == "true",
                quantity_ordered=int(item.get("QuantityOrdered", 0)),
                quantity_shipped=int(item.get("QuantityShipped", 0)),
                item_price=float(item.get("ItemPrice", 0) or 0),
                item_tax=float(item.get("ItemTax", 0) or 0),
                shipping_price=float(item.get("ShippingPrice", 0) or 0),
                shipping_tax=float(item.get("ShippingTax", 0) or 0),
                purchase_date=purchase_date,
                created_at=datetime.utcnow(),
                marketplace=marketplace_name  # ✅ Human-readable marketplace name
            )

            db.session.add(new_item)

        db.session.commit()
        print(f"✅ Saved {len(order_items)} item(s) for order {amazon_order_id}")

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error saving order items to DB: {e}")




@app.route("/api/update-cogs", methods=["POST"])
def update_cogs():
    data = request.json
    seller_sku = data.get("seller_sku")
    cogs = data.get("cogs")

    if not seller_sku or cogs is None:
        return jsonify({"error": "Missing seller_sku or cogs"}), 400

    items = AmazonOrderItem.query.filter_by(seller_sku=seller_sku).all()

    if not items:
        return jsonify({"error": f"No items found for seller_sku: {seller_sku}"}), 404

    for item in items:
        item.cogs = cogs

    db.session.commit()
    return jsonify({"message": f"✅ COGS updated for seller_sku {seller_sku}"}), 200



@app.route("/api/update-safety-stock", methods=["POST"])
def update_safety_stock():
    data = request.json
    seller_sku = data.get("seller_sku")
    safety_stock = data.get("safety_stock")

    if not seller_sku or safety_stock is None:
        return jsonify({"error": "Missing seller_sku or safety_stock"}), 400

    items = AmazonOrderItem.query.filter_by(seller_sku=seller_sku).all()

    if not items:
        return jsonify({"error": f"No items found for seller_sku: {seller_sku}"}), 404

    for item in items:
        item.safety_stock = safety_stock

    db.session.commit()
    return jsonify({"message": f"✅ Safety Stock updated for seller_sku {seller_sku}"}), 200



@app.route("/api/update-time-delivery", methods=["POST"])
def update_time_delivery():
    data = request.json
    seller_sku    = data.get("seller_sku")
    time_delivery = data.get("time_delivery")

    if not seller_sku or time_delivery is None:
        return jsonify({"error": "Missing seller_sku or time_delivery"}), 400

    items = AmazonOrderItem.query.filter_by(seller_sku=seller_sku).all()
    if not items:
        return jsonify({"error": f"No items found for seller_sku: {seller_sku}"}), 404

    for item in items:
        item.time_delivery = time_delivery

    db.session.commit()
    return jsonify({
        "message": f"✅ Time Delivery updated for seller_sku {seller_sku}"
    }), 200




@app.route("/get-all-order-items", methods=["GET"])
def get_all_order_items():
    """Fetch and store order items for all orders in the last year."""
    from datetime import datetime, timedelta

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    print(f"🔍 Fetching order items for all orders of {selling_partner_id}...")

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    # ✅ Get all orders in the last year for this selling_partner_id
    one_year_ago = datetime.utcnow() - timedelta(days=365)
    recent_orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,
        AmazonOrders.purchase_date >= one_year_ago
    ).all()

    if not recent_orders:
        return jsonify({"message": "No orders found for the last year"}), 200

    saved_count = 0
    for order in recent_orders:
        amazon_order_id = order.amazon_order_id
        print(f"📦 Fetching items for order {amazon_order_id}")

        try:
            order_items = fetch_order_items(access_token, amazon_order_id)
            if not order_items:
                print(f"⚠️ No items found for {amazon_order_id}")
                continue

            store_order_items_in_db(
                selling_partner_id=selling_partner_id,
                order_data={
                    "amazon_order_id": amazon_order_id,
                    "order_items": order_items
                }
            )
            saved_count += len(order_items)
        except Exception as e:
            print(f"❌ Failed to process {amazon_order_id}: {e}")
            continue

    return jsonify({
        "status": "✅ Complete",
        "orders_processed": len(recent_orders),
        "order_items_saved": saved_count
    })




@app.route("/api/order-items", methods=["GET"])
def get_order_items_for_dashboard():
    selling_partner_id = request.args.get("selling_partner_id")
    marketplace_id = request.args.get("marketplace_id")  # ← get the ID
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # Map from ID to readable name
    MARKETPLACE_ID_TO_NAME = {
        "A1AM78C64UM0Y8": "Amazon.com.mx",
        "ATVPDKIKX0DER": "Amazon.com",
        "APJ6JRA9NG5V4": "Amazon.ca",
    }
    marketplace_name = MARKETPLACE_ID_TO_NAME.get(marketplace_id)

    query = AmazonOrderItem.query.filter_by(selling_partner_id=selling_partner_id)
    if marketplace_name:
        query = query.filter_by(marketplace=marketplace_name)

    items = query.all()
    seen_asins = set()
    unique_items = []

    def compute_margin(item):
        cogs = float(item.cogs or 0)
        fees = float(item.item_tax or 0) + float(item.shipping_tax or 0)
        revenue = float(item.item_price or 0) + float(item.shipping_price or 0)
        return round(revenue - (cogs + fees), 2)

    for item in items:
        if item.asin not in seen_asins:
            seen_asins.add(item.asin)
            unique_items.append({
    "order_item_id": item.order_item_id,
    "title": item.title,
    "seller_sku": item.seller_sku,
    "asin": item.asin,
    "item_price": float(item.item_price or 0),
    "shipping_price": float(item.shipping_price or 0),
    "amazon_fees": float(item.item_tax or 0) + float(item.shipping_tax or 0),
    "cogs": float(item.cogs or 0),
    "safety_stock": int(item.safety_stock or 0),
    "time_delivery": int(item.time_delivery or 0),
    "margin": compute_margin(item),
    "image_url": item.image_url or f"https://render-webflow-restless-river-7629.fly.dev/images/{item.asin}.jpg",
    "marketplace": item.marketplace or "Unknown"
})


    return jsonify(unique_items)






@app.route("/api/dashboard-data", methods=["GET"])
def get_dashboard_data():
    selling_partner_id = request.args.get("selling_partner_id")
    marketplace_id     = (request.args.get("marketplace_id") or "").strip()
    marketplace_name   = (request.args.get("marketplace") or "").strip()
    start_date         = request.args.get("start_date")
    end_date           = request.args.get("end_date")

    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    apply_marketplace_id_filter = marketplace_id and marketplace_id.lower() != "all"
    apply_marketplace_name_filter = marketplace_name and marketplace_name.lower() != "all"

    from sqlalchemy import func

    MARKETPLACE_ID_TO_NAME = {
        "A1AM78C64UM0Y8": "Amazon.com.mx",
        "ATVPDKIKX0DER": "Amazon.com",
        "APJ6JRA9NG5V4": "Amazon.ca",
        # Add more as needed
    }

    # ── Orders query ───────────────────────────────────────────────────────────
    orders_query = AmazonOrders.query.filter_by(selling_partner_id=selling_partner_id)

    if apply_marketplace_id_filter:
        orders_query = orders_query.filter_by(marketplace_id=marketplace_id)
    elif apply_marketplace_name_filter:
        marketplace_id_from_name = next((k for k, v in MARKETPLACE_ID_TO_NAME.items() if v == marketplace_name), None)
        if marketplace_id_from_name:
            orders_query = orders_query.filter_by(marketplace_id=marketplace_id_from_name)

    if start_date:
        try:
            start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            orders_query = orders_query.filter(AmazonOrders.purchase_date >= start_date_obj)
        except ValueError:
            return jsonify({"error": "Invalid start_date format"}), 400

    if end_date:
        try:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
            orders_query = orders_query.filter(AmazonOrders.purchase_date <= end_date_obj)
        except ValueError:
            return jsonify({"error": "Invalid end_date format"}), 400

    orders = orders_query.all()

    if not orders:
        return jsonify({
            "cards": {
                "total_sales": 0,
                "revenue":     0,
                "amazon_fees": 0,
                "roi":         0
            },
            "orders": [],
            "shipment_events": []
        })

    # ── Compute shipping, totals, ROI ─────────────────────────────────────────
    shipping_map = {}
    for order in orders:
        shipping_total = sum(
            float(item.shipping_price or 0)
            for item in AmazonOrderItem.query.filter_by(amazon_order_id=order.amazon_order_id).all()
        )
        shipping_map[order.amazon_order_id] = shipping_total

    order_total_sales = sum(float(order.total_amount or 0) for order in orders)
    shipping_total    = sum(shipping_map.values())
    total_sales       = order_total_sales + shipping_total
    total_fees        = sum(float(order.amazon_fees or 0) for order in orders)
    total_cogs        = sum(
        float(item.cogs or 0)
        for order in orders
        for item in AmazonOrderItem.query.filter_by(amazon_order_id=order.amazon_order_id).all()
    )
    revenue = total_sales
    roi     = ((revenue - total_cogs - total_fees) / (total_cogs + total_fees + 1e-5)) * 100

    # ── Joined shipment events + order items ──────────────────────────────────
    joined_q = db.session.query(
        AmazonFinancialShipmentEvent,
        AmazonOrderItem.asin,
        AmazonOrderItem.title,
        AmazonOrderItem.cogs
    ).outerjoin(
        AmazonOrderItem,
        (AmazonFinancialShipmentEvent.order_id == AmazonOrderItem.amazon_order_id) &
        (AmazonFinancialShipmentEvent.sku      == AmazonOrderItem.seller_sku)
    ).filter(
        AmazonFinancialShipmentEvent.selling_partner_id == selling_partner_id,
        AmazonFinancialShipmentEvent.posted_date.isnot(None)
    )

    if apply_marketplace_id_filter:
        marketplace_name_normalized = MARKETPLACE_ID_TO_NAME.get(marketplace_id, "").strip()
        if marketplace_name_normalized:
            joined_q = joined_q.filter(AmazonFinancialShipmentEvent.marketplace == marketplace_name_normalized)
    elif apply_marketplace_name_filter:
        joined_q = joined_q.filter(AmazonFinancialShipmentEvent.marketplace == marketplace_name)

    joined_data = joined_q.all()

    shipment_events = []
    for event, asin, title, cogs in joined_data:
        data = event.to_dict()
        data["asin"]  = asin
        data["title"] = title
        data["cogs"]  = float(cogs) if cogs is not None else None
        shipment_events.append(data)

    return jsonify({
        "cards": {
            "total_sales": round(total_sales, 2),
            "revenue":     round(revenue,    2),
            "amazon_fees": round(total_fees,  2),
            "roi":         round(roi,         2)
        },
        "orders": [
            {
                **order.to_dict(),
                "shipping_price": round(shipping_map.get(order.amazon_order_id, 0), 2)
            }
            for order in orders
        ],
        "shipment_events": shipment_events
    })





@app.route("/api/settlement-report")
def run_settlement_report():
    selling_partner_id = request.args.get("selling_partner_id")
    
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)

    if not access_token:
        return jsonify({"message": "❌ No access token found. Please check your credentials."}), 401

    file_path = sync_settlement_report(selling_partner_id, access_token)
    if file_path:
        try:
            with open(file_path, "r") as f:
                preview = f.read(2000)  # Preview first 2000 characters
            return jsonify({
                "message": "✅ Report downloaded",
                "preview": preview,
                "file_path": file_path
            })
        except Exception as e:
            return jsonify({"error": f"Failed to read file: {str(e)}"}), 500
    else:
        return jsonify({"message": "❌ Failed to download settlement report"}), 500



@app.route("/api/access-token")
def get_access_token_for_partner():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found or token is invalid"}), 404

    return jsonify({"access_token": access_token})





@app.route("/api/preview-settlement-report")
def preview_settlement():
    url = request.args.get("url")
    if not url:
        return {"message": "❌ Missing pre-signed URL"}

    from amazon_api import preview_settlement_report_from_url
    preview = preview_settlement_report_from_url(url)

    if preview is None:
        return {"message": "❌ Could not preview report"}

    return preview.to_json(orient="records")




@app.route("/api/settlement-preview")
def preview_settlement_report():
    selling_partner_id = request.args.get("selling_partner_id")
    document_id = request.args.get("document_id")

    if not selling_partner_id or not document_id:
        return {"message": "❌ Missing parameters"}, 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"message": "❌ No access token found"}, 400

    preview = fetch_and_preview_latest_settlement_report(access_token, document_id)
    if preview:
        return jsonify(preview)
    else:
        return {"message": "❌ Could not preview report"}, 500



@app.route("/api/settlement-save", methods=["POST"])
def save_settlement_report():
    selling_partner_id = request.args.get("selling_partner_id")
    document_id = request.args.get("document_id")

    if not selling_partner_id or not document_id:
        return {"message": "❌ Missing parameters"}, 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"message": "❌ No access token found"}, 400

    url = get_presigned_settlement_url(access_token, document_id)
    if not url:
        return {"message": "❌ Failed to fetch document URL"}, 500

    response = requests.get(url)
    if response.status_code != 200:
        return {"message": "❌ Failed to download settlement report"}, 500

    save_settlement_report_to_v2_table(response.content.decode("utf-8"), selling_partner_id)
    return {"message": "✅ Settlement report saved"}




@app.route("/api/request-listings-report", methods=["GET"])
def trigger_all_listings_report():
    from amazon_api import request_all_listings_report

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return {"message": "❌ Missing selling_partner_id"}, 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"message": "❌ No access token found"}, 401

    report = request_all_listings_report(access_token)
    if report:
        return jsonify(report)
    else:
        return {"message": "❌ Failed to request listings report"}, 500
    


@app.route("/api/download-listings-report", methods=["GET"])
def download_listings_report_csv():
    """
    Downloads the Merchant Listings All Data report (using the reportId) and sends
    it as a downloadable CSV file.
    """
    selling_partner_id = request.args.get("selling_partner_id")
    report_id = request.args.get("report_id")
    
    if not selling_partner_id or not report_id:
        return jsonify({"error": "Missing selling_partner_id or report_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 400

    from amazon_api import download_listings_report
    file_path = download_listings_report(report_id, access_token)
    if not file_path:
        return jsonify({"error": "Failed to download listings report"}), 500

    return send_file(file_path, as_attachment=True)



@app.route("/api/simulate-fba-fee", methods=["GET"])
def simulate_fba_fulfillment_fee():
    from amazon_api import get_dimensions_by_asin

    selling_partner_id = request.args.get("selling_partner_id")
    asin = request.args.get("asin")

    if not selling_partner_id or not asin:
        return jsonify({"error": "Missing asin or selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "Access token not found"}), 401

    # Step 1: Get product dimensions and weight
    dimensions = get_dimensions_by_asin(access_token, asin)
    if not dimensions:
        return jsonify({"error": "Could not fetch dimensions from Amazon"}), 500

    length = dimensions.get("length_cm")
    width = dimensions.get("width_cm")
    height = dimensions.get("height_cm")
    weight_kg = dimensions.get("weight_kg")

    if not all([length, width, height, weight_kg]):
        return jsonify({
            "error": "Missing one or more values for length, width, height, or weight",
            "dimensions": dimensions
        }), 400

    # Step 2: Estimate dimensional weight
    dim_weight = (length * width * height) / 5000
    billable_weight = max(weight_kg, dim_weight)

    # Step 3: Estimate FBA fulfillment fee (MX rough tiers)
    if billable_weight <= 0.2:
        fba_fee = 57
        tier = "Small Standard"
    elif billable_weight <= 0.5:
        fba_fee = 72
        tier = "Large Standard"
    elif billable_weight <= 1.0:
        fba_fee = 90
        tier = "Standard Heavy"
    else:
        fba_fee = 90 + ((billable_weight - 1.0) * 20)
        tier = "Oversize or Heavy"

    return jsonify({
        "asin": asin,
        "tier": tier,
        "fba_fee_mxn": round(fba_fee, 2),
        "billable_weight_kg": round(billable_weight, 3),
        "actual_weight_kg": round(weight_kg, 3),
        "dim_weight_kg": round(dim_weight, 3),
        "dimensions_cm": {
            "length": length,
            "width": width,
            "height": height
        }
    })



@app.route("/api/financial-events/group", methods=["GET"])
def financial_events_by_group():
    selling_partner_id = request.args.get("selling_partner_id")
    group_id = request.args.get("group_id")

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import get_financial_events_by_group
    events = get_financial_events_by_group(access_token, group_id)
    return jsonify(events or {"error": "Failed to retrieve events"})



@app.route("/download/financial-group/<group_id>", methods=["GET"])
def download_financial_group(group_id):
    file_path = f"financial_event_reports/group_{group_id}.json"
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return {"error": "File not found"}, 404



# Endpoint: Financial Events by Order
@app.route("/api/financial-events/order", methods=["GET"])
def financial_events_by_order():
    selling_partner_id = request.args.get("selling_partner_id")
    order_id = request.args.get("order_id")

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import get_financial_events_by_order
    events = get_financial_events_by_order(access_token, order_id)
    return jsonify(events or {"error": "Failed to retrieve events"})



# Endpoint: Financial Events by Date Range
@app.route("/api/financial-events/date-range", methods=["GET"])
def financial_events_by_date_range():
    selling_partner_id = request.args.get("selling_partner_id")
    start_date = request.args.get("start_date")  # Example: 2024-12-01T00:00:00Z
    end_date = request.args.get("end_date")      # Example: 2024-12-31T23:59:59Z

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import get_financial_events_by_date_range
    events = get_financial_events_by_date_range(access_token, start_date, end_date)
    return jsonify(events or {"error": "Failed to retrieve events"})



@app.route("/api/financial-events/groups", methods=["GET"])
def list_event_groups():
    selling_partner_id = request.args.get("selling_partner_id")
    start_date = request.args.get("start_date")  # Optional: 2024-12-01T00:00:00Z
    end_date = request.args.get("end_date")      # Optional: 2024-12-31T23:59:59Z

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import list_financial_event_groups
    groups = list_financial_event_groups(access_token, start_date, end_date)
    return jsonify(groups or {"error": "Failed to fetch event groups"})




@app.route("/api/fetch-and-save-fba-events", methods=["GET"])
def fetch_and_save_fba_events():
    from amazon_api import list_financial_event_groups, save_shipment_events_by_group

    selling_partner_id = request.args.get("selling_partner_id")
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "Access token not found"}, 401

    # Obtener los grupos de los últimos 365 días
    start_date = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_date = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    group_data = list_financial_event_groups(access_token, start_date, end_date)
    group_list = group_data.get("payload", {}).get("FinancialEventGroupList", [])

    saved_groups = 0

    for group in group_list:
        group_id = group.get("FinancialEventGroupId")
        if group_id:
            save_shipment_events_by_group(access_token, selling_partner_id, group_id)
            saved_groups += 1

    return {"message": f"✅ Guardado exitoso de eventos de envío para {saved_groups} grupos"}




from sqlalchemy.orm import aliased
from models import AmazonFinancialShipmentEvent, AmazonOrderItem

@app.route("/api/joined-financial-events", methods=["GET"])
def get_joined_financial_data():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # Perform the join
    results = (
        db.session.query(
            AmazonFinancialShipmentEvent,
            AmazonOrderItem.asin,
            AmazonOrderItem.title,
            AmazonOrderItem.cogs
        )
        .outerjoin(
            AmazonOrderItem,
            (AmazonFinancialShipmentEvent.order_id == AmazonOrderItem.amazon_order_id) &
            (AmazonFinancialShipmentEvent.sku == AmazonOrderItem.seller_sku)
        )
        .filter(AmazonFinancialShipmentEvent.selling_partner_id == selling_partner_id)
        .all()
    )

    # Format results
    joined_data = []
    for event, asin, title, cogs in results:
        row = event.to_dict()
        row["asin"] = asin
        row["title"] = title
        row["cogs"] = float(cogs) if cogs else None
        joined_data.append(row)

    return jsonify(joined_data)




@app.route("/update-order-addresses", methods=["POST"])
def update_order_addresses():
    selling_partner_id = request.json.get("selling_partner_id")
    access_token = get_stored_tokens(selling_partner_id)

    orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,
        AmazonOrders.state == None  # Only update orders with missing state
    ).all()

    updated = 0
    for order in orders:
        address = fetch_order_address(access_token, order.order_id)
        if address:
            order.state = address.get("StateOrRegion")
            order.city = address.get("City")
            order.postal_code = address.get("PostalCode")
            order.country_code = address.get("CountryCode")
            updated += 1

    db.session.commit()
    return jsonify({"updated": updated})


@app.route("/api/order-locations", methods=["GET"])
def get_order_locations():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    orders = AmazonOrders.query.filter_by(selling_partner_id=selling_partner_id).all()

    location_counts = {}
    for order in orders:
        state = order.state or "Unknown"
        location_counts[state] = location_counts.get(state, 0) + 1

    return jsonify([
        {"location": state, "order_count": count}
        for state, count in location_counts.items()
    ])



import requests
from flask import Response

@app.route("/proxy-image/<asin>")
def proxy_image(asin):
    url = f"https://ws-na.amazon-adsystem.com/widgets/q?_encoding=UTF8&ASIN={asin}&Format=_SL75_&ID=AsinImage"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return Response(res.content, content_type=res.headers['Content-Type'])
        else:
            return "Image not found", 404
    except Exception as e:
        print(f"❌ Error fetching image for ASIN {asin}: {e}")
        return "Error fetching image", 500





@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    inventory_items = AmazonInventoryItem.query.filter_by(selling_partner_id=selling_partner_id).all()
    items = []
    for item in inventory_items:
        items.append({
            "sku": item.sku,
            "asin": item.asin,
            "product_name": item.product_name,
            "quantity_available": item.quantity_available,
            "image_url": f"https://render-webflow-restless-river-7629.fly.dev/images/{item.asin}.jpg" if item.asin else None
        })
    return jsonify(items)



from sqlalchemy import func, extract
from datetime import datetime, timedelta
from flask import request, jsonify
from dateutil.relativedelta import relativedelta

@app.route("/api/inventory-summary", methods=["GET"])
def inventory_summary():
    seller = request.args.get("selling_partner_id")
    marketplace_id = request.args.get("marketplace_id", "all")

    if not seller:
        return jsonify([]), 400

    # Marketplace ID to readable name mapping
    marketplace_name_map = {
        "A1AM78C64UM0Y8": "Amazon.com.mx",
        "ATVPDKIKX0DER": "Amazon.com",
        # Add others if needed
    }
    marketplace_name = marketplace_name_map.get(marketplace_id)

    # Filter inventory items by seller and optional marketplace_id
    item_query = AmazonInventoryItem.query.filter_by(selling_partner_id=seller)
    if marketplace_id != "all":
        item_query = item_query.filter_by(marketplace_id=marketplace_id)
    items = item_query.all()

    today = datetime.utcnow()
    out = []

    for inv in items:
        # Sales in the last 3 individual months
        sales_by_month = []
        for i in range(3):
            month_start = (today.replace(day=1) - relativedelta(months=i)).replace(day=1)
            month_end = month_start + relativedelta(months=1)

            total = db.session.query(
                db.func.sum(AmazonFinancialShipmentEvent.quantity)
            ).filter(
                AmazonFinancialShipmentEvent.selling_partner_id == seller,
                AmazonFinancialShipmentEvent.sku == inv.sku,
                AmazonFinancialShipmentEvent.posted_date >= month_start,
                AmazonFinancialShipmentEvent.posted_date < month_end
            ).scalar() or 0

            if total > 0:
                sales_by_month.append(total)

        avg_monthly_sales = round(sum(sales_by_month) / len(sales_by_month), 2) if sales_by_month else 0

        # Historic data for fallback unit price and COGS using ASIN instead of SKU
        hist_query = AmazonOrderItem.query.filter(
            AmazonOrderItem.selling_partner_id == seller,
            AmazonOrderItem.asin == inv.asin
        )
        if marketplace_name:
            hist_query = hist_query.filter(AmazonOrderItem.marketplace == marketplace_name)

        all_hist = hist_query.all()
        hist_cogs = [float(o.cogs or 0) for o in all_hist if o.cogs]
        hist_prices = [float(o.item_price or 0) for o in all_hist if o.item_price]

        avg_price = round(sum(hist_prices) / len(hist_prices), 2) if hist_prices else 0
        avg_cogs = round(sum(hist_cogs) / len(hist_cogs), 2) if hist_cogs else 0

        # Get latest record for safety_stock and time_delivery
        latest_order_item = hist_query.order_by(AmazonOrderItem.purchase_date.desc()).first()
        safety_stock = latest_order_item.safety_stock if latest_order_item else 0
        time_delivery = latest_order_item.time_delivery if latest_order_item else 0

        qty_avail = inv.quantity_available or 0

        out.append({
            "asin": inv.asin,
            "sku": inv.sku,
            "name": inv.product_name,
            "quantity_available": qty_avail,
            "avg_monthly_sales": avg_monthly_sales,
            "unit_price": avg_price,
            "cogs": avg_cogs,
            "stock_value_unit_price": round(qty_avail * avg_price, 2),
            "stock_value_cogs": round(qty_avail * avg_cogs, 2),
            "safety_stock": safety_stock,
            "time_delivery": time_delivery,
            "image_url": (
                f"https://ws-na.amazon-adsystem.com/widgets/q?_encoding=UTF8"
                f"&ASIN={inv.asin}&Format=_SL75_&ID=AsinImage"
            )
        })

    return jsonify(out)




@app.route("/api/save-unsuppressed-inventory", methods=["GET"])
def trigger_save_unsuppressed_inventory():
    from amazon_api import save_unsuppressed_inventory_report

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    try:
        # This function should already exist and use the access_token internally
        save_unsuppressed_inventory_report(access_token, selling_partner_id)
        return jsonify({"message": f"✅ Inventory report saved for {selling_partner_id}"}), 200

    except Exception as e:
        print(f"❌ Error saving inventory report: {e}")
        return jsonify({"error": str(e)}), 500






@app.route("/api/download-inventory-report", methods=["GET"])
def download_inventory_report():
    try:
        selling_partner_id = request.args.get("selling_partner_id")
        access_token = request.args.get("access_token")

        if not selling_partner_id or not access_token:
            return {"error": "Missing selling_partner_id or access_token"}, 400

        print(f"📥 Generating report for {selling_partner_id}...")

        # Step 1: Get reportDocumentId
        document_id = generate_merchant_listings_report(access_token, selling_partner_id)

        # Step 2: Get download URL
        headers = {
            "Authorization": f"Bearer {access_token}",
            "x-amz-access-token": access_token
        }

        doc_res = requests.get(
            f"https://sellingpartnerapi-na.amazon.com/reports/2021-06-30/documents/{document_id}",
            headers=headers
        )
        if doc_res.status_code != 200:
            raise Exception(f"❌ Failed to get document: {doc_res.text}")

        download_url = doc_res.json()["url"]
        print("📎 Got download URL.")

        download = requests.get(download_url)
        try:
            content = download.content.decode("utf-8")
        except UnicodeDecodeError:
            content = download.content.decode("latin-1")

        # Step 3: Save as CSV on server
        file_path = f"/tmp/inventory_{selling_partner_id}.csv"
        with open(file_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)

        print(f"✅ Report ready at {file_path}")
        return send_file(file_path, as_attachment=True)

    except Exception as e:
        print(f"❌ Error in download_inventory_report: {e}")
        return {"error": str(e)}, 500




@app.route("/api/save-merged-inventory", methods=["GET"])
def save_merged_inventory():
    try:
        selling_partner_id = request.args.get("selling_partner_id")
        access_token = request.args.get("access_token")
        marketplace_id = request.args.get("marketplace_id")  # 👈 added

        if not selling_partner_id or not access_token or not marketplace_id:
            return {"error": "Missing selling_partner_id, access_token, or marketplace_id"}, 400

        from amazon_api import save_merged_inventory_report
        save_merged_inventory_report(access_token, selling_partner_id, marketplace_id)  # 👈 pass it to function
        return {"message": "✅ Inventory merged and saved successfully!"}

    except Exception as e:
        print(f"❌ Error in /api/save-merged-inventory: {e}")
        return {"error": str(e)}, 500





from flask import send_from_directory


@app.route('/images/<asin>.jpg')
def serve_image(asin):
    return send_from_directory('static/images', f"{asin}.jpg")




@app.route("/api/fetch-return-report", methods=["GET"])
def fetch_return_report():
    from amazon_api import get_report_document_id, get_presigned_settlement_url
    import xml.etree.ElementTree as ET
    from collections import defaultdict
    from models import AmazonReturnStats

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    marketplace_id = get_marketplace_id_for_seller(selling_partner_id)
    if not marketplace_id:
        return jsonify({"error": "No marketplace ID found"}), 400

    # Step 1: Request the report
    report_id = request_returns_report(access_token, marketplace_id)
    if not report_id:
        return jsonify({"error": "Failed to request report"}), 500

    # Step 2: Wait for report to finish and get document ID
    document_id = get_report_document_id(report_id, access_token)
    if not document_id:
        return jsonify({"error": "❌ Report processing failed (FATAL or CANCELLED)"}), 500


    # Step 3: Get the download URL
    url = get_presigned_settlement_url(access_token, document_id)
    if not url:
        return jsonify({"error": "Failed to get presigned URL"}), 500

    xml_data = requests.get(url).content.decode("utf-8")

    # Step 4: Parse XML and summarize
    root = ET.fromstring(xml_data)
    monthly_data = defaultdict(lambda: {"total_returns": 0, "order_ids": set()})

    for return_event in root.findall(".//ReturnItemDetails"):
        order_id = return_event.findtext("OrderId")
        return_date = return_event.findtext("ReturnDate")
        if order_id and return_date:
            month = return_date[:7]
            monthly_data[month]["total_returns"] += 1
            monthly_data[month]["order_ids"].add(order_id)

    # Step 5: Save to DB
    for month, data in monthly_data.items():
        total_orders = len(data["order_ids"])
        total_returns = data["total_returns"]
        return_rate = total_returns / total_orders if total_orders else 0

        existing = AmazonReturnStats.query.filter_by(
            selling_partner_id=selling_partner_id,
            month=month
        ).first()

        if existing:
            existing.total_orders = total_orders
            existing.total_returns = total_returns
            existing.return_rate = return_rate
            existing.last_updated = datetime.utcnow()
        else:
            db.session.add(AmazonReturnStats(
                selling_partner_id=selling_partner_id,
                marketplace=marketplace_id,
                month=month,
                total_orders=total_orders,
                total_returns=total_returns,
                return_rate=return_rate
            ))

    db.session.commit()
    return jsonify({"message": f"✅ Saved return stats for {len(monthly_data)} month(s)"}), 200




@app.route("/api/view-fba-financial-events", methods=["GET"])
def view_fba_financial_events():
    from amazon_api import list_financial_event_groups, get_financial_events_by_group

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "Access token not found"}), 401

    start_date = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_date = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    group_data = list_financial_event_groups(access_token, start_date, end_date)
    group_list = group_data.get("payload", {}).get("FinancialEventGroupList", [])

    all_events = []

    for group in group_list:
        group_id = group.get("FinancialEventGroupId")
        if group_id:
            print(f"📦 Fetching events for group: {group_id}")
            group_events = get_financial_events_by_group(access_token, group_id)
            if group_events and "payload" in group_events:
                all_events.append({
                    "group_id": group_id,
                    "events": group_events["payload"]
                })

    return jsonify({
        "selling_partner_id": selling_partner_id,
        "group_count": len(group_list),
        "fetched_event_groups": len(all_events),
        "data": all_events
    })



@app.route("/api/return-rate-summary", methods=["GET"])
def return_rate_summary():
    from models import AmazonOrderItem, AmazonFinancialShipmentEvent
    from sqlalchemy import func
    from collections import defaultdict
    from datetime import datetime, timedelta

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    today = datetime.utcnow()
    twelve_months_ago = today - timedelta(days=365)

    order_items = AmazonOrderItem.query.filter(
        AmazonOrderItem.selling_partner_id == selling_partner_id,
        AmazonOrderItem.purchase_date != None,
        AmazonOrderItem.purchase_date >= twelve_months_ago
    ).all()

    refund_events = AmazonFinancialShipmentEvent.query.filter(
        AmazonFinancialShipmentEvent.selling_partner_id == selling_partner_id,
        AmazonFinancialShipmentEvent.refund_fee != None
    ).all()

    refunded_order_ids = set(e.order_id for e in refund_events if e.order_id)

    month_data = defaultdict(lambda: {"orders": 0, "returns": 0})

    for item in order_items:
        month = item.purchase_date.strftime("%Y-%m")
        month_data[month]["orders"] += 1
        if item.amazon_order_id in refunded_order_ids:
            month_data[month]["returns"] += 1

    total_returns = sum(d["returns"] for d in month_data.values())
    total_orders = sum(d["orders"] for d in month_data.values())
    overall_rate = round((total_returns / total_orders) * 100, 1) if total_orders else 0

    return jsonify({
        "total_returns": total_returns,
        "return_rate_overall": overall_rate,
        "monthly_return_counts": {
            month: d["returns"]
            for month, d in sorted(month_data.items())
        },
        "monthly_return_rates": {
            month: round((d["returns"] / d["orders"]) * 100, 1) if d["orders"] else 0
            for month, d in sorted(month_data.items())
        }
    })



@app.route("/api/inventory-planner", methods=["GET"])
def get_inventory_planner_data():
    from sqlalchemy import func
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    # Query params
    selling_partner_id = request.args.get("selling_partner_id")
    marketplace_param = request.args.get("marketplace", "all")

    # Map IDs to names
    marketplace_name_map = {
        "A1AM78C64UM0Y8": "Amazon.com.mx",
        "ATVPDKIKX0DER": "Amazon.com",
        "APJ6JRA9NG5V4": "Amazon.ca",
    }
    reverse_map = {v: k for k, v in marketplace_name_map.items()}

    # Normalize marketplace input to readable name
    if marketplace_param == "all":
        marketplace = "all"
    elif marketplace_param in marketplace_name_map:
        marketplace = marketplace_name_map[marketplace_param]
    elif marketplace_param in reverse_map:
        marketplace = marketplace_param  # already a readable name
    else:
        marketplace = "all"  # fallback

    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # 1) Inventory items
    inv_q = AmazonInventoryItem.query.filter_by(selling_partner_id=selling_partner_id)
    if marketplace != "all":
        inv_q = inv_q.filter_by(marketplace_id=reverse_map.get(marketplace, marketplace))  # use ID
    inventory_items = inv_q.all()

    # 2) Avg monthly sales from shipment events
    sales_map = {}
    today = datetime.utcnow()
    for item in inventory_items:
        if not item.asin:
            continue
        sales_by_month = []
        for i in range(3):
            month_start = (today.replace(day=1) - relativedelta(months=i)).replace(day=1)
            month_end = month_start + relativedelta(months=1)
            total_query = db.session.query(
                func.sum(AmazonFinancialShipmentEvent.quantity)
            ).filter(
                AmazonFinancialShipmentEvent.selling_partner_id == selling_partner_id,
                AmazonFinancialShipmentEvent.sku == item.sku,
                AmazonFinancialShipmentEvent.posted_date >= month_start,
                AmazonFinancialShipmentEvent.posted_date < month_end
            )
            if marketplace != "all":
                total_query = total_query.filter(
                    AmazonFinancialShipmentEvent.marketplace == marketplace
                )
            total = total_query.scalar() or 0
            if total > 0:
                sales_by_month.append(total)

        avg_monthly = round(sum(sales_by_month) / len(sales_by_month), 2) if sales_by_month else 0
        sales_map[item.asin] = avg_monthly

    # 3) Build planner output
    results = []
    for item in inventory_items:
        if not item.asin:
            continue
        oi_q = AmazonOrderItem.query.filter_by(
            selling_partner_id=selling_partner_id,
            asin=item.asin
        )
        if marketplace != "all":
            oi_q = oi_q.filter(AmazonOrderItem.marketplace == marketplace)
        oi = oi_q.order_by(AmazonOrderItem.purchase_date.desc()).first()

        safety_stock = oi.safety_stock if oi and oi.safety_stock is not None else 0
        time_delivery = oi.time_delivery if oi and oi.time_delivery is not None else 0
        qty_avail = item.quantity_available or 0
        avg_sales = sales_map.get(item.asin, 0)

        daily_sales = (avg_sales / 30) or 0.01
        coverage_days = int(qty_avail / daily_sales)

        results.append({
            "asin": item.asin,
            "title": item.product_name or "Untitled",
            "quantity_available": qty_avail,
            "avg_monthly_sales": avg_sales,
            "safety_stock": safety_stock,
            "time_delivery": time_delivery,
            "coverage_days": coverage_days
        })

    return jsonify(results)
