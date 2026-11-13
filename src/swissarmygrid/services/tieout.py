"""The tie-out report on a compare's two grids (domain/tieout.py has the rules and the query text).

Each side is read as its grid shows it (after its own steps), totalled by the same columns, and the
two are set side by side: which groups tie and which are off.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.domain import tieout as T
from swissarmygrid.services.ports import SqlSession
from swissarmygrid.services.relationships import Compare, Compares


class TieOut:
    def __init__(self, compares: Compares) -> None:
        self.compares = compares
        self.ws = compares.ws

    async def options(self, cmp: Compare) -> dict[str, Any]:
        """The columns both grids have (and which can be grouped or totalled), with a suggestion."""

        def read(cur: SqlSession) -> dict[str, Any]:
            left, right = self.compares.sides(cur, cmp)
            pairs = T.shared(left.columns, right.columns)
            few: set[str] = set()
            for pair in pairs:
                if T.groupable(pair) and not T.numeric(pair):
                    found = cur.execute(T.distinct_query(left.positioned, pair.left)).fetchone()
                    if found and int(found[0]) <= T.SUGGESTED_GROUP_VALUES:
                        few.add(pair.name)
            return {
                "columns": [
                    {"name": p.name, "type": p.left.type, "group": T.groupable(p), "measure": T.numeric(p)}
                    for p in pairs
                ],
                "suggested": T.suggest(pairs, few),
            }

        return dict(await self.compares._run(read))

    async def run(self, cmp: Compare, body: Any, who: str) -> dict[str, Any]:
        def compute(cur: SqlSession) -> dict[str, Any]:
            left, right = self.compares.sides(cur, cmp)
            request = T.read_request(body, T.shared(left.columns, right.columns))
            rows = cur.execute(T.query(left.positioned, right.positioned, request)).fetchall()
            return T.report(rows, request) | {"left": cmp.left, "right": cmp.right}

        result: dict[str, Any] = await self.compares._run(compute)
        self.ws.audit.write(who, "tie_out", f"{cmp.left}:{cmp.right}", groups=len(result["groups"]))
        return result
