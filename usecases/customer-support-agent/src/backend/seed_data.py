# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Generate realistic seed data for TechGadgets.com."""
import json
import random
import uuid
from datetime import datetime, timedelta

from backend.database import init_db, get_db

# Dedicated, seeded generator: synthetic demo data must be reproducible.
_rng = random.Random(42)  # nosec B311 - deterministic demo data, not security-sensitive


def _uid():
    return uuid.uuid4().hex[:8]


def _date(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _future_date(days_ahead: int) -> str:
    return (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S")


# ─── Products ────────────────────────────────────────────────────────────────

PRODUCTS = [
    # Laptops
    {"name": "ProBook X1 Ultra", "category": "laptops", "brand": "TechGadgets", "base_price": 1899.99, "description": "14-inch ultrabook with Intel Core i9, 32GB RAM, 1TB SSD", "specs": {"processor": "Intel Core i9-13900H", "ram": "32GB DDR5", "storage": "1TB NVMe SSD", "display": "14\" 2.8K OLED", "battery": "72Wh", "weight": "1.4kg"}},
    {"name": "GameForce RTX Pro", "category": "laptops", "brand": "TechGadgets", "base_price": 2499.99, "description": "16-inch gaming laptop with RTX 4080, 64GB RAM", "specs": {"processor": "Intel Core i9-13900HX", "ram": "64GB DDR5", "storage": "2TB NVMe SSD", "gpu": "NVIDIA RTX 4080 12GB", "display": "16\" 240Hz QHD", "weight": "2.5kg"}},
    {"name": "EcoBook Air", "category": "laptops", "brand": "TechGadgets", "base_price": 899.99, "description": "Lightweight everyday laptop, perfect for students", "specs": {"processor": "Intel Core i5-1340P", "ram": "16GB DDR5", "storage": "512GB SSD", "display": "13.3\" FHD IPS", "battery": "56Wh", "weight": "1.1kg"}},
    {"name": "WorkStation Pro 17", "category": "laptops", "brand": "TechGadgets", "base_price": 3299.99, "description": "Professional workstation for creative professionals", "specs": {"processor": "Intel Xeon W-2495X", "ram": "128GB ECC DDR5", "storage": "4TB NVMe SSD", "gpu": "NVIDIA RTX A5000", "display": "17\" 4K DCI-P3", "weight": "3.2kg"}},
    {"name": "ChromeBook Flex", "category": "laptops", "brand": "TechGadgets", "base_price": 349.99, "description": "2-in-1 ChromeBook with touchscreen", "specs": {"processor": "MediaTek Kompanio 1380", "ram": "8GB", "storage": "128GB eMMC", "display": "14\" FHD Touch", "battery": "50Wh", "weight": "1.5kg"}},
    # Smartphones
    {"name": "Pixel Ultra 8 Pro", "category": "smartphones", "brand": "TechGadgets", "base_price": 1199.99, "description": "Flagship smartphone with 200MP camera and AI features", "specs": {"processor": "Snapdragon 8 Gen 3", "ram": "12GB", "storage": "256GB", "display": "6.8\" LTPO AMOLED 120Hz", "camera": "200MP + 50MP + 12MP", "battery": "5500mAh"}},
    {"name": "Pixel Lite 8", "category": "smartphones", "brand": "TechGadgets", "base_price": 499.99, "description": "Mid-range phone with flagship camera", "specs": {"processor": "Snapdragon 7 Gen 2", "ram": "8GB", "storage": "128GB", "display": "6.4\" AMOLED 90Hz", "camera": "108MP + 12MP", "battery": "4500mAh"}},
    {"name": "Pixel Fold 2", "category": "smartphones", "brand": "TechGadgets", "base_price": 1799.99, "description": "Foldable phone with 7.6-inch inner display", "specs": {"processor": "Snapdragon 8 Gen 3", "ram": "12GB", "storage": "512GB", "display": "7.6\" foldable + 6.2\" cover", "camera": "50MP + 12MP + 10MP", "battery": "4400mAh"}},
    # Headphones
    {"name": "SoundWave Pro ANC", "category": "headphones", "brand": "TechGadgets", "base_price": 349.99, "description": "Premium noise-canceling over-ear headphones", "specs": {"driver": "40mm custom", "anc": "Adaptive ANC", "battery": "30 hours", "connectivity": "Bluetooth 5.3", "codec": "LDAC, aptX HD", "weight": "250g"}},
    {"name": "BudsPro Elite", "category": "headphones", "brand": "TechGadgets", "base_price": 199.99, "description": "True wireless earbuds with spatial audio", "specs": {"driver": "11mm dynamic", "anc": "Active ANC", "battery": "8h (28h with case)", "connectivity": "Bluetooth 5.3", "water_resistance": "IPX5", "weight": "5.4g per bud"}},
    {"name": "StudioMonitor X", "category": "headphones", "brand": "TechGadgets", "base_price": 499.99, "description": "Audiophile-grade wired headphones", "specs": {"driver": "50mm planar magnetic", "impedance": "32 ohm", "frequency": "20Hz-40kHz", "cable": "Detachable 3.5mm/6.35mm", "weight": "380g"}},
    # Tablets
    {"name": "TabPro 12 Ultra", "category": "tablets", "brand": "TechGadgets", "base_price": 1099.99, "description": "12.4-inch tablet for creatives with S-Pen", "specs": {"processor": "Snapdragon 8 Gen 2", "ram": "12GB", "storage": "256GB", "display": "12.4\" Super AMOLED 120Hz", "battery": "10090mAh", "stylus": "S-Pen included"}},
    {"name": "TabLite 10", "category": "tablets", "brand": "TechGadgets", "base_price": 329.99, "description": "Affordable entertainment tablet", "specs": {"processor": "Helio G99", "ram": "6GB", "storage": "128GB", "display": "10.4\" LCD", "battery": "7040mAh"}},
    # TVs
    {"name": "CinemaView 65 OLED", "category": "tvs", "brand": "TechGadgets", "base_price": 2199.99, "description": "65-inch 4K OLED TV with Dolby Vision IQ", "specs": {"display": "65\" 4K OLED", "hdr": "Dolby Vision IQ, HDR10+", "refresh_rate": "120Hz", "hdmi": "4x HDMI 2.1", "smart_tv": "WebOS 24", "speakers": "60W 4.2ch"}},
    {"name": "CinemaView 55 QLED", "category": "tvs", "brand": "TechGadgets", "base_price": 899.99, "description": "55-inch 4K QLED TV with 144Hz gaming mode", "specs": {"display": "55\" 4K QLED", "hdr": "HDR10+", "refresh_rate": "144Hz", "hdmi": "4x HDMI 2.1", "smart_tv": "Tizen OS", "speakers": "40W 2.1ch"}},
    {"name": "CinemaView 75 Mini-LED", "category": "tvs", "brand": "TechGadgets", "base_price": 2999.99, "description": "75-inch Mini-LED TV with 2500 dimming zones", "specs": {"display": "75\" 4K Mini-LED", "hdr": "Dolby Vision, HDR10+", "dimming_zones": 2500, "refresh_rate": "120Hz", "hdmi": "4x HDMI 2.1", "speakers": "80W 4.2.2ch"}},
    # Cameras
    {"name": "SnapShot Alpha R5", "category": "cameras", "brand": "TechGadgets", "base_price": 2499.99, "description": "Full-frame mirrorless camera, 45MP, 8K video", "specs": {"sensor": "45MP Full-Frame CMOS", "video": "8K 30fps / 4K 120fps", "af_points": 1053, "iso_range": "100-51200", "stabilization": "5-axis IBIS", "weight": "680g (body)"}},
    {"name": "SnapShot Vlog Z30", "category": "cameras", "brand": "TechGadgets", "base_price": 599.99, "description": "Compact vlogging camera with flip screen", "specs": {"sensor": "20MP APS-C", "video": "4K 60fps", "display": "3\" Flip Touch LCD", "stabilization": "Digital IS", "weight": "295g"}},
    # Smart Home
    {"name": "HomeHub Max 15", "category": "smart_home", "brand": "TechGadgets", "base_price": 299.99, "description": "15-inch smart display with built-in speakers", "specs": {"display": "15\" FHD Touch", "speakers": "30W stereo", "camera": "5MP with privacy shutter", "assistant": "Multi-assistant support", "connectivity": "WiFi 6E, Bluetooth 5.3, Zigbee, Thread"}},
    {"name": "SmartLock Pro 3", "category": "smart_home", "brand": "TechGadgets", "base_price": 249.99, "description": "Biometric smart lock with fingerprint and face ID", "specs": {"unlock_methods": "Fingerprint, Face ID, PIN, App, Key", "battery": "12 months (4xAA)", "connectivity": "WiFi + Bluetooth", "users": "Up to 100 fingerprints", "rating": "ANSI Grade 1"}},
    # Smartwatches
    {"name": "FitTrack Ultra 3", "category": "smartwatches", "brand": "TechGadgets", "base_price": 449.99, "description": "Premium smartwatch with health monitoring suite", "specs": {"display": "1.5\" LTPO AMOLED", "sensors": "ECG, SpO2, Skin Temp, GPS", "battery": "5 days typical", "water_resistance": "10ATM", "connectivity": "LTE, WiFi, BT 5.3", "storage": "32GB"}},
    {"name": "FitTrack Sport", "category": "smartwatches", "brand": "TechGadgets", "base_price": 249.99, "description": "Rugged sports watch with 14-day battery", "specs": {"display": "1.4\" MIP LCD", "sensors": "HR, SpO2, GPS, Altimeter", "battery": "14 days", "water_resistance": "5ATM", "weight": "52g"}},
    # Speakers
    {"name": "BoomBox 360", "category": "speakers", "brand": "TechGadgets", "base_price": 199.99, "description": "360-degree portable Bluetooth speaker", "specs": {"power": "50W", "drivers": "2 tweeters + 2 woofers", "battery": "12 hours", "water_resistance": "IP67", "connectivity": "Bluetooth 5.3", "weight": "960g"}},
    {"name": "SoundBar Cinema X", "category": "speakers", "brand": "TechGadgets", "base_price": 799.99, "description": "Dolby Atmos soundbar with wireless sub and rears", "specs": {"channels": "7.1.4", "power": "800W", "sub": "Wireless 10\" sub", "rears": "2x wireless rear speakers", "connectivity": "HDMI eARC, WiFi, BT", "dolby_atmos": True}},
    # Accessories
    {"name": "MagCharge Trio", "category": "accessories", "brand": "TechGadgets", "base_price": 79.99, "description": "3-in-1 wireless charger (phone + watch + buds)", "specs": {"power": "15W phone + 5W watch + 5W buds", "compatibility": "Qi2, MagSafe, Apple Watch", "cable": "USB-C 1.5m included"}},
    {"name": "ErgoDesk Stand Pro", "category": "accessories", "brand": "TechGadgets", "base_price": 149.99, "description": "Adjustable laptop stand with USB-C hub", "specs": {"ports": "2x USB-A, 1x USB-C PD, 1x HDMI, SD/microSD", "height": "Adjustable 15-40cm", "material": "Aluminum CNC", "max_weight": "8kg"}},
    {"name": "PowerBank 26800 PD", "category": "accessories", "brand": "TechGadgets", "base_price": 69.99, "description": "26800mAh power bank with 100W USB-C PD", "specs": {"capacity": "26800mAh", "output": "100W USB-C PD + 18W USB-A", "input": "65W USB-C", "ports": "2x USB-C + 1x USB-A", "weight": "480g"}},
    {"name": "TypeMaster Mechanical KB", "category": "accessories", "brand": "TechGadgets", "base_price": 159.99, "description": "Low-profile wireless mechanical keyboard", "specs": {"switches": "Low-profile optical", "layout": "75% (84 keys)", "connectivity": "2.4GHz + BT + USB-C", "battery": "2000mAh (200hr)", "backlight": "Per-key RGB"}},
    {"name": "ProMouse Ergonomic", "category": "accessories", "brand": "TechGadgets", "base_price": 89.99, "description": "Ergonomic vertical mouse with programmable buttons", "specs": {"sensor": "26000 DPI optical", "buttons": "8 programmable", "connectivity": "2.4GHz + BT", "battery": "Rechargeable, 3 months", "weight": "82g"}},
    # Gaming
    {"name": "GamePad Elite X", "category": "gaming", "brand": "TechGadgets", "base_price": 179.99, "description": "Pro gaming controller with Hall-effect sticks", "specs": {"sticks": "Hall-effect (no drift)", "triggers": "Analog + digital switch", "connectivity": "2.4GHz + BT + USB-C", "battery": "40 hours", "compatibility": "PC, Switch, Android, iOS"}},
    {"name": "StreamDeck Pro 15", "category": "gaming", "brand": "TechGadgets", "base_price": 249.99, "description": "15-key stream controller with LCD keys", "specs": {"keys": "15 customizable LCD", "display": "Per-key full color LCD", "connectivity": "USB-C", "compatibility": "Windows, macOS, OBS, Twitch"}},
    # Storage
    {"name": "SpeedDrive 4TB External", "category": "storage", "brand": "TechGadgets", "base_price": 199.99, "description": "4TB portable SSD with 2000MB/s read speed", "specs": {"capacity": "4TB", "read_speed": "2000MB/s", "write_speed": "1800MB/s", "interface": "USB 3.2 Gen 2x2", "encryption": "AES-256 hardware", "weight": "45g"}},
    {"name": "NAS Pro 4-Bay", "category": "storage", "brand": "TechGadgets", "base_price": 549.99, "description": "4-bay NAS for home and small office", "specs": {"bays": 4, "max_capacity": "72TB (4x18TB)", "processor": "Intel Celeron J4125", "ram": "4GB DDR4", "connectivity": "2x 2.5GbE", "raid": "RAID 0/1/5/6/10"}},
]

# ─── Customers ────────────────────────────────────────────────────────────────

FIRST_NAMES = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
               "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
               "Christopher", "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra",
               "Raj", "Priya", "Wei", "Yuki", "Carlos", "Maria", "Ahmed", "Fatima", "Omar", "Aisha"]

LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
              "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
              "Patel", "Singh", "Chen", "Tanaka", "Santos", "Kim", "Hassan", "Ali", "Nakamura", "Sharma"]

STREETS = ["Oak Ave", "Maple St", "Cedar Blvd", "Pine Dr", "Elm Way", "Birch Ln", "Walnut Ct", "Cherry Rd",
           "Sunset Blvd", "Highland Ave", "Park Place", "River Rd", "Lake View Dr", "Mountain Pass", "Valley Way"]

CITIES = [("New York", "NY", "10001"), ("Los Angeles", "CA", "90001"), ("Chicago", "IL", "60601"),
          ("Houston", "TX", "77001"), ("Phoenix", "AZ", "85001"), ("San Francisco", "CA", "94102"),
          ("Seattle", "WA", "98101"), ("Denver", "CO", "80201"), ("Austin", "TX", "73301"),
          ("Portland", "OR", "97201"), ("Miami", "FL", "33101"), ("Boston", "MA", "02101"),
          ("Atlanta", "GA", "30301"), ("Dallas", "TX", "75201"), ("San Diego", "CA", "92101")]

CARRIERS = ["FedEx", "UPS", "USPS", "DHL"]
PAYMENT_METHODS = ["Visa ending in 4242", "Mastercard ending in 5555", "Amex ending in 1234", "PayPal", "Gift Card"]


def _gen_address():
    city, state, zip_code = _rng.choice(CITIES)
    return {
        "street": f"{_rng.randint(100, 9999)} {_rng.choice(STREETS)}",
        "city": city, "state": state, "zip": zip_code, "country": "US"
    }


def _gen_tracking():
    prefix = _rng.choice(["1Z", "92", "78", "JD"])
    return f"{prefix}{_rng.randint(100000000, 999999999)}"


# ─── Knowledge Base Articles ─────────────────────────────────────────────────

KB_ARTICLES = [
    {"title": "Return Policy", "category": "policies", "tags": ["returns", "refund", "exchange"],
     "content": "TechGadgets.com offers a 30-day return policy for most items. Items must be in original packaging with all accessories. Opened software, personal care items, and custom-built products are non-returnable. Refunds are processed within 5-7 business days after we receive the returned item. Shipping costs for returns due to customer preference are the customer's responsibility. Defective items qualify for free return shipping."},
    {"title": "Shipping Information", "category": "policies", "tags": ["shipping", "delivery", "tracking"],
     "content": "Standard shipping (5-7 business days): Free on orders over $50. Express shipping (2-3 business days): $12.99. Next-day delivery: $24.99. All orders are shipped from our warehouses in California and Texas. Tracking information is provided via email within 24 hours of shipment. International shipping is available to select countries with customs fees paid by the recipient."},
    {"title": "Warranty Coverage", "category": "policies", "tags": ["warranty", "coverage", "repair"],
     "content": "All TechGadgets products come with a 1-year standard warranty covering manufacturing defects. Extended warranty plans (2-year, 3-year) are available at purchase. Warranty covers: hardware failures, battery defects, screen defects (dead pixels >5). Warranty does NOT cover: physical damage, water damage (unless rated), normal wear, software issues. To file a claim, contact support with your order number and description of the issue."},
    {"title": "Price Match Guarantee", "category": "policies", "tags": ["price", "match", "guarantee"],
     "content": "We match prices from authorized retailers including Amazon, Best Buy, and Walmart. The item must be identical (same model, color, condition). Price match requests must be made within 14 days of purchase. We do not match: marketplace sellers, auction sites, membership-only prices, out-of-stock items, or clearance/closeout prices. Submit a price match request through our support chat with a link to the competitor's listing."},
    {"title": "How to Track Your Order", "category": "how_to", "tags": ["tracking", "order", "status"],
     "content": "1. Log into your TechGadgets account. 2. Go to 'My Orders'. 3. Click on the order you want to track. 4. Click 'Track Package' to see real-time status. You can also track directly using the tracking number emailed to you: FedEx (fedex.com/tracking), UPS (ups.com/track), USPS (usps.com/track)."},
    {"title": "How to Set Up a Return", "category": "how_to", "tags": ["return", "rma", "label"],
     "content": "1. Go to 'My Orders' and select the order. 2. Click 'Return Items' and select the items to return. 3. Choose a reason for the return. 4. Select refund method (original payment or store credit). 5. Print the prepaid return label. 6. Pack items securely in original packaging. 7. Drop off at the designated carrier location. Allow 5-7 business days for refund processing after receipt."},
    {"title": "Troubleshooting: Laptop Won't Turn On", "category": "troubleshooting", "tags": ["laptop", "power", "boot"],
     "content": "Step 1: Ensure the charger is connected and the LED indicator is on. Step 2: Hold the power button for 15 seconds to force reset. Step 3: Remove all external devices (USB, monitors). Step 4: If battery is removable, remove it, hold power for 30 seconds, reinsert. Step 5: Try a different power outlet. Step 6: Connect to an external monitor to check if screen is the issue. If none of these work, the device may need repair — contact our support team."},
    {"title": "Troubleshooting: Bluetooth Connectivity Issues", "category": "troubleshooting", "tags": ["bluetooth", "headphones", "connection"],
     "content": "Step 1: Ensure Bluetooth is enabled on your device. Step 2: Put headphones in pairing mode (hold power button 5 seconds until LED flashes blue). Step 3: Remove the device from your phone's Bluetooth list and re-pair. Step 4: Restart both devices. Step 5: Ensure you're within 10 meters with no obstacles. Step 6: Update firmware via the TechGadgets app. Step 7: Reset headphones to factory settings (hold power + volume down for 10 seconds)."},
    {"title": "Loyalty Program Guide", "category": "account", "tags": ["loyalty", "points", "rewards"],
     "content": "TechGadgets Rewards Program: Earn 10 points per $1 spent. Tiers: Bronze (0-4999 pts), Silver (5000-19999 pts, 1.5x points), Gold (20000-49999 pts, 2x points + free express shipping), Platinum (50000+ pts, 3x points + free next-day + early access). Redeem: 1000 points = $10 off. Points expire after 12 months of inactivity. Birthday bonus: 500 points. Referral bonus: 2000 points per friend."},
    {"title": "Payment Methods Accepted", "category": "account", "tags": ["payment", "credit card", "paypal"],
     "content": "We accept: Visa, Mastercard, American Express, Discover, PayPal, Apple Pay, Google Pay, TechGadgets Gift Cards, Affirm (buy now pay later, 0% APR for 12 months on purchases over $200). Gift cards can be purchased in denominations of $25, $50, $100, $200, $500. Corporate purchase orders available for business accounts."},
    {"title": "Privacy Policy Summary", "category": "policies", "tags": ["privacy", "data", "gdpr"],
     "content": "We collect: name, email, address, phone, payment info, order history, browsing behavior (with consent). We use data for: order processing, customer support, personalization, fraud prevention. We do NOT sell personal data. Data retention: active accounts indefinitely, closed accounts 3 years. You can request: data export, data deletion, opt-out of marketing. GDPR/CCPA compliant. Full policy at techgadgets.com/privacy."},
    {"title": "How to Cancel an Order", "category": "how_to", "tags": ["cancel", "order"],
     "content": "Orders can be cancelled within 1 hour of placement if they haven't entered processing. To cancel: 1. Go to 'My Orders'. 2. Find the order and click 'Cancel Order'. 3. Select reason for cancellation. 4. Confirm cancellation. If the order has already been shipped, you'll need to refuse delivery or initiate a return. Refunds for cancelled orders are processed within 1-2 business days."},
    {"title": "Extended Warranty Plans", "category": "products", "tags": ["warranty", "extended", "protection"],
     "content": "TechGadgets Care Plans: 2-Year Plan (standard warranty + 1 extra year): 12% of product price. 3-Year Plan (standard warranty + 2 extra years): 18% of product price. Coverage includes: mechanical/electrical failures, battery degradation >20%, display defects. Accidental damage protection add-on: +5% of product price (covers drops, spills, cracked screens). Plans must be purchased within 30 days of product purchase."},
    {"title": "Business & Bulk Orders", "category": "account", "tags": ["business", "bulk", "corporate"],
     "content": "TechGadgets Business Program: Volume discounts starting at 10+ units (5-15% off). Dedicated account manager for orders over $10,000. NET-30 payment terms available for approved businesses. Tax-exempt purchasing available with valid documentation. Custom configuration available for bulk laptop orders. Contact business@techgadgets.com or call 1-800-TECH-BIZ."},
    {"title": "Troubleshooting: Slow WiFi on Smart Home Devices", "category": "troubleshooting", "tags": ["wifi", "smart_home", "connectivity"],
     "content": "Step 1: Check if device supports your WiFi band (2.4GHz vs 5GHz — most smart home devices need 2.4GHz). Step 2: Move device closer to router or add a WiFi extender. Step 3: Restart your router. Step 4: Check for firmware updates in the TechGadgets Home app. Step 5: Reduce interference (microwave, baby monitors use 2.4GHz). Step 6: If using mesh WiFi, ensure device connects to nearest node. Step 7: Factory reset device and re-setup."},
]

# ─── Promotions ──────────────────────────────────────────────────────────────

PROMOTIONS = [
    {"code": "SAVE20", "description": "20% off your order", "discount_type": "percentage", "discount_value": 20, "min_order": 100, "categories": ["all"], "usage_limit": 1000},
    {"code": "LAPTOP10", "description": "$100 off laptops over $999", "discount_type": "fixed", "discount_value": 100, "min_order": 999, "categories": ["laptops"], "usage_limit": 500},
    {"code": "FREESHIP", "description": "Free express shipping", "discount_type": "fixed", "discount_value": 12.99, "min_order": 0, "categories": ["all"], "usage_limit": 5000},
    {"code": "NEWUSER15", "description": "15% off for new customers", "discount_type": "percentage", "discount_value": 15, "min_order": 50, "categories": ["all"], "usage_limit": 10000},
    {"code": "BUNDLE25", "description": "25% off when buying 3+ accessories", "discount_type": "percentage", "discount_value": 25, "min_order": 150, "categories": ["accessories"], "usage_limit": 2000},
    {"code": "SUMMER30", "description": "Summer sale — 30% off headphones", "discount_type": "percentage", "discount_value": 30, "min_order": 0, "categories": ["headphones"], "usage_limit": 1000},
    {"code": "LOYALTY500", "description": "500 bonus loyalty points", "discount_type": "fixed", "discount_value": 0, "min_order": 200, "categories": ["all"], "usage_limit": 3000},
    {"code": "TV150", "description": "$150 off any TV", "discount_type": "fixed", "discount_value": 150, "min_order": 500, "categories": ["tvs"], "usage_limit": 300},
    {"code": "GAMEON", "description": "10% off gaming gear", "discount_type": "percentage", "discount_value": 10, "min_order": 0, "categories": ["gaming", "laptops"], "usage_limit": 2000},
    {"code": "EXPIRED10", "description": "Expired coupon for testing", "discount_type": "percentage", "discount_value": 10, "min_order": 0, "categories": ["all"], "usage_limit": 100},
]


def seed_all():
    """Populate the database with seed data."""
    init_db()
    with get_db() as conn:
        # Check if already seeded
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count > 0:
            print(f"Database already seeded ({count} products). Skipping.")
            return

        # --- Products ---
        for i, p in enumerate(PRODUCTS):
            pid = f"PROD-{i+1:04d}"
            conn.execute(
                "INSERT INTO products (id, name, category, brand, base_price, description, specs, rating_avg, review_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, p["name"], p["category"], p["brand"], p["base_price"], p["description"],
                 json.dumps(p["specs"]), round(_rng.uniform(3.5, 4.9), 1), _rng.randint(20, 500))
            )
            # Create 1-3 variants per product
            for vi in range(_rng.randint(1, 3)):
                vid = f"VAR-{pid}-{vi+1}"
                color = _rng.choice(["Black", "Silver", "White", "Blue", "Space Gray"])
                price_adj = _rng.choice([-50, 0, 0, 50, 100])
                stock = _rng.randint(0, 150)
                conn.execute(
                    "INSERT INTO product_variants (id, product_id, sku, options, price, stock_quantity, is_available) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (vid, pid, f"SKU-{_uid()}", json.dumps({"color": color}),
                     p["base_price"] + price_adj, stock, 1 if stock > 0 else 0)
                )

        # --- Customers ---
        customers = []
        for i in range(100):
            cid = f"CUST-{i+1:04d}"
            fname = _rng.choice(FIRST_NAMES)
            lname = _rng.choice(LAST_NAMES)
            email = f"{fname.lower()}.{lname.lower()}{_rng.randint(1,99)}@{'gmail.com' if _rng.random() > 0.3 else 'outlook.com'}"
            tier = _rng.choices(["bronze", "silver", "gold", "platinum"], weights=[50, 30, 15, 5])[0]
            points = {"bronze": _rng.randint(0, 4999), "silver": _rng.randint(5000, 19999),
                      "gold": _rng.randint(20000, 49999), "platinum": _rng.randint(50000, 80000)}[tier]
            conn.execute(
                "INSERT INTO customers (id, email, first_name, last_name, phone, shipping_address, billing_address, loyalty_tier, loyalty_points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, email, fname, lname, f"+1-{_rng.randint(200,999)}-{_rng.randint(100,999)}-{_rng.randint(1000,9999)}",
                 json.dumps(_gen_address()), json.dumps(_gen_address()), tier, points,
                 _date(_rng.randint(30, 730)))
            )
            customers.append(cid)

        # --- Orders ---
        statuses = ["pending", "processing", "shipped", "delivered", "delivered", "delivered",
                    "delivered", "cancelled", "returned"]
        for i in range(500):
            oid = f"ORD-{2024 + i // 250}-{i+1:06d}"
            cid = _rng.choice(customers)
            status = _rng.choice(statuses)
            days_ago = _rng.randint(1, 180)
            order_date = _date(days_ago)
            num_items = _rng.randint(1, 4)
            items = _rng.sample(range(len(PRODUCTS)), min(num_items, len(PRODUCTS)))
            total = sum(PRODUCTS[idx]["base_price"] for idx in items)
            carrier = _rng.choice(CARRIERS)
            tracking = _gen_tracking() if status in ("shipped", "delivered") else None
            est_delivery = _future_date(_rng.randint(1, 7)) if status in ("shipped", "processing") else (
                _date(days_ago - _rng.randint(1, 5)) if status == "delivered" else None
            )

            conn.execute(
                "INSERT INTO orders (id, customer_id, status, order_date, total, shipping_address, payment_method, tracking_number, carrier, estimated_delivery) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, cid, status, order_date, round(total, 2), json.dumps(_gen_address()),
                 _rng.choice(PAYMENT_METHODS), tracking, carrier, est_delivery)
            )

            for item_idx in items:
                iid = f"ITEM-{_uid()}"
                product = PRODUCTS[item_idx]
                conn.execute(
                    "INSERT INTO order_items (id, order_id, product_id, product_name, quantity, unit_price, item_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (iid, oid, f"PROD-{item_idx+1:04d}", product["name"],
                     _rng.randint(1, 2), product["base_price"],
                     "returned" if status == "returned" else "active")
                )

            # --- Shipping events for shipped/delivered orders ---
            if tracking:
                events = [
                    ("order_placed", "Warehouse, CA", 0, "Order placed and confirmed"),
                    ("processing", "Warehouse, CA", 1, "Order being prepared"),
                    ("shipped", "Distribution Center, CA", 2, "Package picked up by carrier"),
                    ("in_transit", "Regional Hub, NV", 3, "Package in transit"),
                ]
                if status == "delivered":
                    events.append(("out_for_delivery", "Local Facility", 4, "Out for delivery"))
                    events.append(("delivered", "Customer Address", 5, "Delivered - Left at front door"))

                for evt_type, loc, day_offset, desc in events:
                    conn.execute(
                        "INSERT INTO shipping_events (id, order_id, tracking_number, event_type, location, timestamp, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (_uid(), oid, tracking, evt_type, loc, _date(days_ago - day_offset), desc)
                    )

        # --- Knowledge Base ---
        for i, article in enumerate(KB_ARTICLES):
            conn.execute(
                "INSERT INTO knowledge_base (id, title, content, category, tags, helpful_count, view_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"KB-{i+1:04d}", article["title"], article["content"], article["category"],
                 json.dumps(article["tags"]), _rng.randint(10, 500), _rng.randint(100, 5000))
            )

        # --- Promotions ---
        for i, promo in enumerate(PROMOTIONS):
            valid_from = _date(30)
            valid_until = _future_date(60) if promo["code"] != "EXPIRED10" else _date(5)
            conn.execute(
                "INSERT INTO promotions (id, code, description, discount_type, discount_value, min_order, valid_from, valid_until, applicable_categories, usage_limit, usage_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"PROMO-{i+1:04d}", promo["code"], promo["description"], promo["discount_type"],
                 promo["discount_value"], promo["min_order"], valid_from, valid_until,
                 json.dumps(promo["categories"]), promo["usage_limit"], _rng.randint(0, promo["usage_limit"] // 2))
            )

        # --- Product Reviews ---
        for pid_idx in range(len(PRODUCTS)):
            pid = f"PROD-{pid_idx+1:04d}"
            for _ in range(_rng.randint(3, 8)):
                rating = _rng.choices([5, 4, 3, 2, 1], weights=[40, 30, 15, 10, 5])[0]
                titles = {5: "Excellent!", 4: "Great product", 3: "Decent", 2: "Disappointed", 1: "Terrible"}
                contents = {
                    5: "Absolutely love this product. Works perfectly and exceeded my expectations.",
                    4: "Good product overall. Minor issues but nothing major. Would recommend.",
                    3: "It's okay. Does what it's supposed to but nothing special.",
                    2: "Not what I expected. Quality feels cheap and had issues within a week.",
                    1: "Do not buy. Stopped working after 3 days. Returning immediately."
                }
                conn.execute(
                    "INSERT INTO product_reviews (id, product_id, customer_id, rating, title, content, verified_purchase, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_uid(), pid, _rng.choice(customers), rating, titles[rating],
                     contents[rating], _rng.random() > 0.2, _date(_rng.randint(1, 120)))
                )

    print(f"Database seeded: {len(PRODUCTS)} products, 100 customers, 500 orders, {len(KB_ARTICLES)} KB articles, {len(PROMOTIONS)} promotions")


if __name__ == "__main__":
    seed_all()
