"""
seed_ecommerce.py
-----------------
Loads the Brazilian E-Commerce (Olist) Kaggle dataset into SQLite.

Creates ecommerce.db with 9 tables from the CSV files.
This gives the agent a real-world dataset with 100K+ orders.

Usage:
    python db/seed_ecommerce.py
"""

import os
import sqlite3
import csv

DB_PATH = os.path.join(os.path.dirname(__file__), "ecommerce.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "ecommerce_data")

CSV_TO_TABLE = {
    "olist_customers_dataset.csv":              "customers",
    "olist_orders_dataset.csv":                 "orders",
    "olist_order_items_dataset.csv":            "order_items",
    "olist_products_dataset.csv":               "products",
    "olist_sellers_dataset.csv":                "sellers",
    "olist_order_payments_dataset.csv":         "payments",
    "olist_order_reviews_dataset.csv":          "reviews",
    "olist_geolocation_dataset.csv":            "geolocation",
    "product_category_name_translation.csv":    "category_translation",
}


def seed():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Removed existing: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for csv_file, table_name in CSV_TO_TABLE.items():
        csv_path = os.path.join(DATA_DIR, csv_file)

        if not os.path.exists(csv_path):
            print(f"⚠️  Skipping {csv_file} — file not found")
            continue

        print(f"Loading {csv_file} → {table_name}...", end=" ")

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)
            clean_headers = [h.strip().replace(" ", "_").lower() for h in headers]
            columns_def = ", ".join([f'"{col}" TEXT' for col in clean_headers])
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            cur.execute(f'CREATE TABLE "{table_name}" ({columns_def})')
            placeholders = ", ".join(["?" for _ in clean_headers])
            rows = list(reader)
            cur.executemany(
                f'INSERT INTO "{table_name}" VALUES ({placeholders})',
                rows
            )
            print(f"{len(rows)} rows")

    print("\nCreating views...")

    cur.execute("""
        CREATE VIEW IF NOT EXISTS order_summary AS
        SELECT
            o.order_id,
            o.customer_id,
            o.order_status,
            o.order_purchase_timestamp,
            COUNT(oi.order_item_id) AS item_count,
            ROUND(SUM(CAST(oi.price AS REAL)), 2) AS total_price,
            ROUND(SUM(CAST(oi.freight_value AS REAL)), 2) AS total_freight
        FROM orders o
        LEFT JOIN order_items oi ON o.order_id = oi.order_id
        GROUP BY o.order_id
    """)
    print("  ✓ order_summary view")

    cur.execute("""
        CREATE VIEW IF NOT EXISTS product_performance AS
        SELECT
            p.product_id,
            p.product_category_name,
            ct.product_category_name_english,
            COUNT(oi.order_item_id) AS times_ordered,
            ROUND(SUM(CAST(oi.price AS REAL)), 2) AS total_revenue,
            ROUND(AVG(CAST(oi.price AS REAL)), 2) AS avg_price
        FROM products p
        LEFT JOIN order_items oi ON p.product_id = oi.product_id
        LEFT JOIN category_translation ct ON p.product_category_name = ct.product_category_name
        GROUP BY p.product_id
    """)
    print("  ✓ product_performance view")

    cur.execute("""
        CREATE VIEW IF NOT EXISTS seller_performance AS
        SELECT
            s.seller_id,
            s.seller_city,
            s.seller_state,
            COUNT(DISTINCT oi.order_id) AS total_orders,
            ROUND(SUM(CAST(oi.price AS REAL)), 2) AS total_revenue,
            ROUND(AVG(CAST(r.review_score AS REAL)), 2) AS avg_review_score
        FROM sellers s
        LEFT JOIN order_items oi ON s.seller_id = oi.seller_id
        LEFT JOIN orders o ON oi.order_id = o.order_id
        LEFT JOIN reviews r ON o.order_id = r.order_id
        GROUP BY s.seller_id
    """)
    print("  ✓ seller_performance view")

    conn.commit()

    print(f"\n{'=' * 50}")
    print("DATABASE SUMMARY")
    print(f"{'=' * 50}")
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = cur.fetchall()
    for (table,) in tables:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        count = cur.fetchone()[0]
        print(f"  {table:30s} → {count:>10,} rows")

    cur.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
    views = cur.fetchall()
    print(f"\n  Views: {[v[0] for v in views]}")

    conn.close()
    print(f"\n✅ E-commerce database created: {DB_PATH}")


if __name__ == "__main__":
    seed()
