"""Push a made-up orders table to the running server and print its grid address.

Run the server first (`uv run swissarmygrid`), then `uv run python examples/push_demo.py`.
Run this again to see hot reload: the open grid refreshes in place with new values and keeps
its sort. All data here is random; nothing real.

    uv run python examples/push_demo.py --rows 1000000
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from swissarmygrid.client import push


def make_orders(rows: int, seed: int | None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    regions = np.array(["North", "South", "East", "West", "  Central ", "north"])
    return pd.DataFrame(
        {
            "order_id": np.arange(1, rows + 1),
            "region": rng.choice(regions, rows),
            "product": rng.choice(np.array(["Widget", "Gadget", "Gizmo", "Doohickey"]), rows),
            "quantity": rng.integers(1, 50, rows),
            "unit_price": rng.uniform(1, 500, rows).round(2),
            "order_date": pd.Timestamp("2026-01-01") + pd.to_timedelta(rng.integers(0, 365, rows), unit="D"),
            # Every 17th note is blank, so the grid shows how blanks sort (last).
            "note": [None if i % 17 == 0 else f"note {i}" for i in range(rows)],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--name", default="orders")
    parser.add_argument("--new-grid", action="store_true", help="Open an extra grid on it.")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    print(push(make_orders(args.rows, args.seed), name=args.name, new_grid=args.new_grid))


if __name__ == "__main__":
    main()
