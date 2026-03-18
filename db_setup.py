"""
MANN-FILTER Scraper — Database Setup
Run this once to create the database and tables.
"""

import mysql.connector

# ── Connection Config (same as scraper_pro.py) ───────────────────────────────
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "",       # <-- set your MySQL root password here
    "use_pure": True,     # avoid C-extension segfault on Python 3.14+
}

DB_NAME = "mann_filter_db"


def setup():
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # Create database
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                   "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    print(f"Database '{DB_NAME}' created (or already exists).")

    cursor.execute(f"USE `{DB_NAME}`")

    # ── Products (cross-reference data) ──────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            search_term     INT            NOT NULL,
            external_number VARCHAR(255)   NOT NULL DEFAULT '',
            manufacturer    VARCHAR(255)   NOT NULL DEFAULT '',
            mann_filter     VARCHAR(255)   NOT NULL DEFAULT '',
            status          VARCHAR(100)   NOT NULL DEFAULT 'Unknown',
            filter_type     VARCHAR(255)   NOT NULL DEFAULT '',
            url             TEXT           NOT NULL,
            created_at      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,

            INDEX idx_mann_filter (mann_filter),
            INDEX idx_search_term (search_term),
            INDEX idx_manufacturer (manufacturer)
        ) ENGINE=InnoDB
    """)
    print("Table 'products' ready.")

    # ── Dimensions ───────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dimensions (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            product_id      INT            NOT NULL,
            dimension_name  VARCHAR(255)   NOT NULL,
            dimension_value VARCHAR(255)   NOT NULL,

            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            INDEX idx_product_id (product_id)
        ) ENGINE=InnoDB
    """)
    print("Table 'dimensions' ready.")

    # ── Vehicles (applications) ──────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id                  INT AUTO_INCREMENT PRIMARY KEY,
            product_id          INT            NOT NULL,
            brand               VARCHAR(255)   NOT NULL DEFAULT '',
            model               VARCHAR(255)   NOT NULL DEFAULT '',
            model_type          VARCHAR(255)   NOT NULL DEFAULT '',
            filter_type         VARCHAR(255)   NOT NULL DEFAULT '',
            engine_code         VARCHAR(255)   NOT NULL DEFAULT '',
            ccm                 VARCHAR(50)    NOT NULL DEFAULT '',
            kw                  VARCHAR(50)    NOT NULL DEFAULT '',
            hp                  VARCHAR(50)    NOT NULL DEFAULT '',
            year_of_manufacture VARCHAR(100)   NOT NULL DEFAULT '',

            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            INDEX idx_product_id (product_id),
            INDEX idx_brand (brand)
        ) ENGINE=InnoDB
    """)
    print("Table 'vehicles' ready.")

    # ── OEM Numbers ──────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS oem_numbers (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            product_id      INT            NOT NULL,
            manufacturer    VARCHAR(255)   NOT NULL DEFAULT '',
            oem_number      VARCHAR(255)   NOT NULL DEFAULT '',

            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            INDEX idx_product_id (product_id),
            INDEX idx_oem_number (oem_number)
        ) ENGINE=InnoDB
    """)
    print("Table 'oem_numbers' ready.")

    conn.commit()
    cursor.close()
    conn.close()
    print("\nDatabase setup complete!")


if __name__ == "__main__":
    setup()
