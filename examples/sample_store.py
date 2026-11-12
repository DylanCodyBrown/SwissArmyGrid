"""A made-up store, for testing workspaces, relationships and their autodetection. Nothing here is real.

Nine tables shaped like a small sales data model (a star schema with a snowflake arm), generated from a
seed so every run is identical:

    regions <- stores <- customers <- orders -> calendar (twice: order_date active, ship_date inactive)
                  ^                     ^
                  +---------------------+        orders <- order_lines -> products -> categories
                                                 order_lines <- returns (a two-column key)

`TRUE_RELATIONSHIPS` is the answer key: every real relationship, so a test can score what an autodetector
finds (did it find them all, and nothing else?). `DECOYS` lists column pairs built to look related and not
be: an autodetector that suggests one of them is fooled.

The traps, on purpose:
- Names that do not match: orders.customer_id joins customers.cust_key; customers.home_store joins
  stores.store_id.
- Types that do not match: customers.home_store holds text ("17") while stores.store_id is a whole number.
- A two-column key: returns (order_id, line_no) joins order_lines (order_id, line_no).
- Two paths between the same tables: orders.order_date and orders.ship_date both join calendar.date_key.
  Power BI keeps one active and the other inactive; the answer key does the same.
- Orphans: about 1% of orders name a customer who is not in customers (deleted accounts), so a real key
  is not 100% contained.
- Decoys: small whole numbers that overlap by accident (status codes, quantities, years, line numbers)
  and a true/false flag.

Run it to push every table to the running server, or to write Parquet or CSV files:

    python examples/sample_store.py --push
    python examples/sample_store.py --parquet sample_data/
    python examples/sample_store.py --csv sample_data/ --scale 5
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SEED: int = 20261112


@dataclass(frozen=True)
class Relationship:
    """One relationship: the many side (`from_*`) points at the one side (`to_*`), Power BI style."""

    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    cardinality: str = "many-to-one"
    active: bool = True
    note: str = ""


TRUE_RELATIONSHIPS: tuple[Relationship, ...] = (
    Relationship("stores", ("region_code",), "regions", ("region_code",)),
    Relationship(
        "customers", ("home_store",), "stores", ("store_id",), note="text on one side, a number on the other"
    ),
    Relationship("orders", ("customer_id",), "customers", ("cust_key",), note="about 1% orphans"),
    Relationship("orders", ("store_id",), "stores", ("store_id",)),
    Relationship("orders", ("order_date",), "calendar", ("date_key",)),
    Relationship(
        "orders", ("ship_date",), "calendar", ("date_key",), active=False, note="second path to calendar"
    ),
    Relationship("order_lines", ("order_id",), "orders", ("order_id",)),
    Relationship("order_lines", ("sku",), "products", ("sku",)),
    Relationship("products", ("category_id",), "categories", ("category_id",)),
    Relationship(
        "returns", ("order_id", "line_no"), "order_lines", ("order_id", "line_no"), note="two-column key"
    ),
)

# (table, column, table, column) pairs that share values by accident.
DECOYS: tuple[tuple[str, str, str, str], ...] = (
    ("orders", "status_code", "customers", "status_code"),
    ("order_lines", "quantity", "categories", "category_id"),
    ("order_lines", "line_no", "categories", "category_id"),
    ("customers", "signup_year", "calendar", "year"),
    ("returns", "reason_code", "orders", "status_code"),
    ("categories", "is_active", "products", "discontinued"),
)

_REGIONS: tuple[tuple[str, str], ...] = (
    ("NE", "Northeast"),
    ("SE", "Southeast"),
    ("MW", "Midwest"),
    ("SW", "Southwest"),
    ("NW", "Northwest"),
    ("CA", "Coastal"),
)
_CATEGORIES: tuple[str, ...] = (
    "Widgets", "Gadgets", "Gizmos", "Doohickeys", "Thingamajigs", "Whatsits", "Sprockets", "Cogs",
    "Knick-knacks", "Odds and ends", "Bits", "Bobs",
)  # fmt: skip
_FIRST: tuple[str, ...] = (
    "Avery",
    "Blake",
    "Casey",
    "Drew",
    "Emery",
    "Finley",
    "Gray",
    "Harper",
    "Indigo",
    "Jules",
)
_LAST: tuple[str, ...] = (
    "Ashford",
    "Brightwater",
    "Copperfield",
    "Dunmore",
    "Elmswood",
    "Fairbanks",
    "Glenrock",
)


def make_store(scale: int = 1, seed: int = SEED) -> dict[str, pd.DataFrame]:
    """Every table of the made-up store, by name. `scale` multiplies the row counts (1: 20,000 orders)."""
    rng = np.random.default_rng(seed)
    n_stores, n_customers, n_products, n_orders = 40, 2_000 * scale, 300, 20_000 * scale

    regions = pd.DataFrame({"region_code": [c for c, _ in _REGIONS], "region_name": [n for _, n in _REGIONS]})

    store_ids = np.arange(1, n_stores + 1, dtype=np.int64)
    stores = pd.DataFrame(
        {
            "store_id": store_ids,
            "store_name": [f"Store {i:03d}" for i in store_ids],
            "region_code": rng.choice(regions["region_code"].to_numpy(), n_stores),
            "opened": pd.Timestamp("2015-01-01") + pd.to_timedelta(rng.integers(0, 3000, n_stores), unit="D"),
        }
    )

    categories = pd.DataFrame(
        {
            "category_id": np.arange(1, len(_CATEGORIES) + 1, dtype=np.int64),
            "category_name": list(_CATEGORIES),
            "is_active": rng.random(len(_CATEGORIES)) > 0.2,
        }
    )

    products = pd.DataFrame(
        {
            "sku": [f"SKU-{i:05d}" for i in range(1, n_products + 1)],
            "product_name": [f"{rng.choice(_CATEGORIES)} model {i}" for i in range(1, n_products + 1)],
            "category_id": rng.integers(1, len(_CATEGORIES) + 1, n_products),
            "list_price": rng.uniform(2, 400, n_products).round(2),
            "discontinued": rng.random(n_products) > 0.9,
        }
    )

    cust_keys = np.arange(100_001, 100_001 + n_customers, dtype=np.int64)
    customers = pd.DataFrame(
        {
            "cust_key": cust_keys,
            "customer_name": [f"{rng.choice(_FIRST)} {rng.choice(_LAST)}" for _ in range(n_customers)],
            # Text, while stores.store_id is a number: the type trap.
            "home_store": rng.choice(store_ids, n_customers).astype(str),
            "status_code": rng.integers(1, 6, n_customers),
            "signup_year": rng.integers(2018, 2027, n_customers),
        }
    )

    days = pd.date_range("2024-01-01", "2026-12-31", freq="D")
    calendar = pd.DataFrame(
        {
            "date_key": days,
            "year": days.year.astype(np.int64),
            "month": days.month.astype(np.int64),
            "month_name": days.strftime("%B"),
            "weekday": days.strftime("%A"),
        }
    )

    order_ids = np.arange(5_000_001, 5_000_001 + n_orders, dtype=np.int64)
    customer_ids = rng.choice(cust_keys, n_orders)
    orphans = rng.random(n_orders) < 0.01  # deleted accounts: keys no longer in customers
    customer_ids[orphans] = rng.integers(900_001, 999_999, int(orphans.sum()))
    order_dates = days[rng.integers(0, len(days) - 14, n_orders)]
    orders = pd.DataFrame(
        {
            "order_id": order_ids,
            "customer_id": customer_ids,
            "store_id": rng.choice(store_ids, n_orders),
            "order_date": order_dates,
            "ship_date": order_dates + pd.to_timedelta(rng.integers(0, 14, n_orders), unit="D"),
            "status_code": rng.integers(1, 6, n_orders),
        }
    )

    lines_per_order = rng.integers(1, 6, n_orders)
    line_order = np.repeat(order_ids, lines_per_order)
    line_no = np.concatenate([np.arange(1, k + 1) for k in lines_per_order]).astype(np.int64)
    n_lines = len(line_order)
    order_lines = pd.DataFrame(
        {
            "order_id": line_order,
            "line_no": line_no,
            "sku": rng.choice(products["sku"].to_numpy(), n_lines),
            "quantity": rng.integers(1, 13, n_lines),
            "unit_price": rng.uniform(2, 400, n_lines).round(2),
        }
    )

    returned = order_lines.sample(frac=0.04, random_state=seed % (2**32)).sort_values(["order_id", "line_no"])
    returns = pd.DataFrame(
        {
            "return_id": np.arange(1, len(returned) + 1, dtype=np.int64),
            "order_id": returned["order_id"].to_numpy(),
            "line_no": returned["line_no"].to_numpy(),
            "reason_code": rng.integers(1, 6, len(returned)),
            "quantity_returned": np.minimum(
                rng.integers(1, 4, len(returned)), returned["quantity"].to_numpy()
            ),
        }
    )

    return {
        "regions": regions,
        "stores": stores,
        "customers": customers,
        "categories": categories,
        "products": products,
        "calendar": calendar,
        "orders": orders,
        "order_lines": order_lines,
        "returns": returns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scale", type=int, default=1, help="Multiply the row counts (default 1: 20,000 orders)."
    )
    parser.add_argument("--push", action="store_true", help="Push every table to the running server.")
    parser.add_argument("--parquet", type=Path, help="Write one Parquet file per table into this folder.")
    parser.add_argument("--csv", type=Path, help="Write one CSV file per table into this folder.")
    args = parser.parse_args()
    tables = make_store(args.scale)
    for name, frame in tables.items():
        print(f"{name}: {len(frame):,} rows x {len(frame.columns)} columns")
        if args.parquet:
            args.parquet.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(args.parquet / f"{name}.parquet", index=False)
        if args.csv:
            args.csv.mkdir(parents=True, exist_ok=True)
            frame.to_csv(args.csv / f"{name}.csv", index=False)
        if args.push:
            from swissarmygrid.client import push

            print("  ", push(frame, name=name))


if __name__ == "__main__":
    main()
