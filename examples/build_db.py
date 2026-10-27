"""Builds the example SQLite database used for demos, the eval set, and tests.

Domain: a small e-commerce store. Five related tables:
    categories  -> products  -> order_items -> orders -> customers

Running this script (re)creates examples/store.db from scratch with a fixed
random seed, so the data is reproducible but not trivially tiny: enough rows
that aggregate/group-by/join questions have real, checkable answers.
"""

from __future__ import annotations

import datetime
import os
import random
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "store.db")

SCHEMA = """
CREATE TABLE categories (
    category_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT
);

CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    city          TEXT NOT NULL,
    country       TEXT NOT NULL,
    signup_date   TEXT NOT NULL,
    loyalty_tier  TEXT NOT NULL
);

CREATE TABLE products (
    product_id    INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES categories(category_id),
    price         REAL NOT NULL,
    stock         INTEGER NOT NULL,
    supplier      TEXT NOT NULL
);

CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date    TEXT NOT NULL,
    status        TEXT NOT NULL
);

CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL
);
"""

CATEGORIES = [
    ("Electronics", "Gadgets, computers, and accessories"),
    ("Home & Kitchen", "Appliances and household goods"),
    ("Books", "Printed and digital books"),
    ("Sports", "Sporting goods and outdoor equipment"),
    ("Clothing", "Apparel for men and women"),
    ("Toys", "Toys and games for children"),
    ("Beauty", "Cosmetics and personal care"),
    ("Groceries", "Packaged food and beverages"),
]

PRODUCT_NAMES = {
    "Electronics": [
        "Wireless Mouse", "Bluetooth Speaker", "USB-C Hub", "Noise Cancelling Headphones",
        "4K Monitor", "Mechanical Keyboard", "Portable SSD", "Smartwatch", "Webcam", "Power Bank",
    ],
    "Home & Kitchen": [
        "Stand Mixer", "Air Fryer", "Coffee Grinder", "Cast Iron Skillet", "Electric Kettle",
        "Vacuum Cleaner", "Knife Set", "Blender", "Toaster", "Dish Rack",
    ],
    "Books": [
        "Mystery Novel", "Cookbook", "Biography", "Science Fiction Anthology", "History of Rome",
        "Poetry Collection", "Travel Guide", "Programming Handbook", "Children's Picture Book", "Atlas",
    ],
    "Sports": [
        "Yoga Mat", "Running Shoes", "Tennis Racket", "Dumbbell Set", "Camping Tent",
        "Basketball", "Cycling Helmet", "Hiking Backpack", "Swim Goggles", "Resistance Bands",
    ],
    "Clothing": [
        "Denim Jacket", "Cotton T-Shirt", "Wool Sweater", "Running Shorts", "Rain Jacket",
        "Leather Belt", "Summer Dress", "Winter Coat", "Flannel Shirt", "Sneakers",
    ],
    "Toys": [
        "Building Blocks", "Puzzle 500pc", "Remote Control Car", "Board Game", "Action Figure",
        "Stuffed Bear", "Model Train Set", "Art Supply Kit", "Card Game", "Toy Drone",
    ],
    "Beauty": [
        "Moisturizer", "Shampoo", "Lipstick", "Sunscreen SPF50", "Perfume",
        "Face Mask Set", "Hair Dryer", "Nail Polish", "Electric Razor", "Body Lotion",
    ],
    "Groceries": [
        "Organic Honey", "Olive Oil", "Green Tea", "Dark Chocolate Bar", "Pasta Pack",
        "Roasted Almonds", "Sparkling Water", "Granola", "Maple Syrup", "Instant Coffee",
    ],
}

SUPPLIERS = ["Northwind Traders", "Acme Supply Co", "Global Goods Ltd", "Pacific Distributors", "Everline Inc"]

CITIES = [
    ("Chennai", "India"), ("Mumbai", "India"), ("Bengaluru", "India"), ("Delhi", "India"),
    ("San Diego", "USA"), ("Austin", "USA"), ("Seattle", "USA"), ("New York", "USA"),
    ("London", "UK"), ("Manchester", "UK"), ("Toronto", "Canada"), ("Singapore", "Singapore"),
]

FIRST_NAMES = ["Ava", "Liam", "Maya", "Noah", "Zara", "Ethan", "Priya", "Arjun", "Isla", "Mason",
               "Rhea", "Leo", "Nadia", "Omar", "Sana", "Kabir", "Elena", "Diego", "Ingrid", "Yusuf"]
LAST_NAMES = ["Sharma", "Patel", "Nguyen", "Kim", "Garcia", "Smith", "Johnson", "Khan", "Rossi",
              "Mueller", "Tanaka", "Silva", "Brown", "Davies", "Iyer", "Lopez"]

LOYALTY_TIERS = ["bronze", "silver", "gold", "platinum"]
ORDER_STATUSES = ["completed", "completed", "completed", "completed", "shipped", "pending", "cancelled"]


def build(seed: int = 42) -> None:
    rng = random.Random(seed)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    # categories
    for i, (name, desc) in enumerate(CATEGORIES, start=1):
        conn.execute(
            "INSERT INTO categories (category_id, name, description) VALUES (?, ?, ?)",
            (i, name, desc),
        )

    # customers
    used_names = set()
    customer_rows = []
    for cid in range(1, 41):
        while True:
            full_name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if full_name not in used_names:
                used_names.add(full_name)
                break
        city, country = rng.choice(CITIES)
        email = full_name.lower().replace(" ", ".") + f"{cid}@example.com"
        start = datetime.date(2023, 1, 1)
        signup = start + datetime.timedelta(days=rng.randint(0, 900))
        tier = rng.choices(LOYALTY_TIERS, weights=[40, 30, 20, 10])[0]
        customer_rows.append((cid, full_name, email, city, country, signup.isoformat(), tier))
    conn.executemany(
        "INSERT INTO customers (customer_id, name, email, city, country, signup_date, loyalty_tier) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        customer_rows,
    )

    # products
    product_rows = []
    pid = 1
    for cat_idx, (cat_name, _) in enumerate(CATEGORIES, start=1):
        for pname in PRODUCT_NAMES[cat_name]:
            price = round(rng.uniform(4.99, 299.99), 2)
            stock = rng.randint(0, 500)
            supplier = rng.choice(SUPPLIERS)
            product_rows.append((pid, pname, cat_idx, price, stock, supplier))
            pid += 1
    conn.executemany(
        "INSERT INTO products (product_id, name, category_id, price, stock, supplier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        product_rows,
    )
    num_products = pid - 1

    # orders + order_items
    order_rows = []
    item_rows = []
    oid = 1
    item_id = 1
    order_start = datetime.date(2024, 1, 1)
    for _ in range(220):
        cid = rng.randint(1, 40)
        odate = order_start + datetime.timedelta(days=rng.randint(0, 660))
        status = rng.choice(ORDER_STATUSES)
        order_rows.append((oid, cid, odate.isoformat(), status))

        n_items = rng.randint(1, 5)
        chosen_products = rng.sample(range(1, num_products + 1), n_items)
        for prod_id in chosen_products:
            qty = rng.randint(1, 4)
            base_price = product_rows[prod_id - 1][3]
            item_rows.append((item_id, oid, prod_id, qty, base_price))
            item_id += 1
        oid += 1

    conn.executemany(
        "INSERT INTO orders (order_id, customer_id, order_date, status) VALUES (?, ?, ?, ?)",
        order_rows,
    )
    conn.executemany(
        "INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price) "
        "VALUES (?, ?, ?, ?, ?)",
        item_rows,
    )

    conn.commit()
    conn.close()
    print(f"Built {DB_PATH}")
    print(f"  categories={len(CATEGORIES)} customers={len(customer_rows)} "
          f"products={num_products} orders={len(order_rows)} order_items={len(item_rows)}")


if __name__ == "__main__":
    build()
