"""Show the math: every analysis result as a proof a person can follow and redo by hand.

Each proof says which test or model it is, writes every formula with the result's own numbers put in,
works through them to the values the window showed, and ends with how to check it elsewhere (a few
lines of Python with scipy or statsmodels). It is built from the result alone, so it proves exactly
what is on screen. Markdown: headings, tables, `code`, and Unicode for the symbols (χ², Σ, √, ±).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from swissarmygrid.domain.regression import Z95, t_quantile
from swissarmygrid.domain.stats import normal_quantile

MAX_ROWS: int = 40  # table rows in a proof; the rest are summed and said so


class ProofError(ValueError):
    """A result that can't be proven as sent (the message says why)."""


def n(value: Any, digits: int = 4) -> str:
    """A number as a person reads it: thousands separated, `digits` significant figures, no clutter."""
    if value is None:
        return "(blank)"
    if isinstance(value, bool):
        return str(value)
    if not isinstance(value, int | float):
        return str(value)
    x = float(value)
    if not math.isfinite(x):
        return "∞" if x > 0 else "−∞" if x < 0 else "not a number"
    if isinstance(value, int) or (x.is_integer() and abs(x) < 1e15):
        return f"{int(x):,}"
    if x != 0 and (abs(x) < 1e-4 or abs(x) >= 1e12):
        return f"{x:.{digits - 1}e}"
    whole = len(str(int(abs(x)))) if abs(x) >= 1 else 0
    decimals = max(0, digits - whole) if abs(x) >= 1 else digits + max(0, -math.floor(math.log10(abs(x))) - 1)
    text = f"{x:,.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def p_text(p: Any) -> str:
    if p is None:
        return "(blank)"
    return "< 0.0001" if p < 0.0001 else n(p)


def _cell(v: Any) -> str:
    text = n(v) if isinstance(v, int | float) and not isinstance(v, bool) else str(v)
    return text.replace("|", "\\|")


def table(headers: list[str], rows: list[list[Any]], numeric: set[int] | None = None) -> str:
    """A markdown table; columns in `numeric` line up right."""
    numeric = numeric if numeric is not None else set(range(1, len(headers)))
    cell = _cell
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---:" if i in numeric else "---" for i in range(len(headers))) + "|",
        *("| " + " | ".join(cell(v) for v in row) + " |" for row in rows[:MAX_ROWS]),
    ]
    if len(rows) > MAX_ROWS:
        lines.append(f"\n*The first {MAX_ROWS} of {len(rows):,} rows; the sums below use them all.*")
    return "\n".join(lines)


def _sum_text(parts: list[float], total: float) -> str:
    shown = " + ".join(n(x) for x in parts[:8])
    return f"{shown}{' + …' if len(parts) > 8 else ''} = **{n(total)}**"


# ===== the fit test and the test of two columns together =====


def _fit_one(r: dict[str, Any], expected: str) -> list[str]:
    cats = r.get("categories") or []
    rows = [[c["value"], c["observed"], c["expected_share"], c["expected"], c["contribution"]] for c in cats]
    k = len(cats)
    total = sum(float(c["contribution"]) for c in cats)
    out = [
        f"### {r['column']}",
        "",
        f"**Test:** Pearson's chi-square goodness of fit: does {r['column']} follow the expected shares "
        f"({expected})?",
        "",
        f"1. Count each value: O. Its expected count is E = n × p, with n = {n(r['n'])} rows and p its "
        "expected share.",
        "",
        table(["Value", "O (observed)", "p (expected share)", "E = n × p", "(O − E)² / E"], rows),
        "",
        f"2. χ² = Σ (O − E)² / E = {_sum_text([float(c['contribution']) for c in cats], total)} "
        f"(reported {n(r['statistic'])}).",
        f"3. Degrees of freedom: df = k − 1 = {k} − 1 = **{r['df']}** (k = {k} values).",
        f"4. p = P(χ² with {r['df']} df ≥ {n(r['statistic'])}) = **{p_text(r['p'])}**: the chance of "
        "differences at least this large if the column really follows the expected shares.",
        f"5. Effect size: Cohen's w = √(χ² / n) = √({n(r['statistic'])} / {n(r['n'])}) = "
        f"**{n(math.sqrt(float(r['statistic']) / max(1, float(r['n']))))}** ({r.get('effect', '')}).",
    ]
    if r.get("conformity_mad") is not None:
        out.append(
            f"6. Nigrini's conformity: the mean of |observed share − expected share| = "
            f"**{n(r['conformity_mad'])}** ({r.get('conformity', '')})."
        )
    if r.get("cells"):
        out += ["", _together_cells(r)]
    obs = ", ".join(str(c["observed"]) for c in cats[:12])
    exp = ", ".join(n(c["expected"]) for c in cats[:12])
    if cats and len(cats) <= 12:
        out += [
            "",
            "**Check it yourself:**",
            "",
            "```python",
            "from scipy.stats import chisquare",
            f"chisquare([{obs}], [{exp}])",
            "```",
        ]
    return out


def _together_cells(t: dict[str, Any]) -> str:
    cells = t.get("cells") or []
    rows = [[c["a"], c["b"], c["observed"], c["expected"], c["contribution"]] for c in cells]
    r_, c_ = int(t.get("first_values") or 0), int(t.get("second_values") or 0)
    small = max(1, min(r_, c_) - 1)
    total = sum(float(c["contribution"]) for c in cells)
    return "\n".join(
        [
            "**Test:** Pearson's chi-square test of independence: is one column's mix the same for each "
            "value of the other?",
            "",
            "1. Count each pair: O. If the columns were unrelated, E = (its row's total × its column's "
            "total) / n.",
            "",
            table(["First", "Second", "O", "E", "(O − E)² / E"], rows, {2, 3, 4}),
            "",
            f"2. χ² = Σ (O − E)² / E = {_sum_text([float(c['contribution']) for c in cells], total)}.",
            f"3. df = (rows − 1) × (columns − 1) = ({r_} − 1) × ({c_} − 1) = **{t['df']}**.",
            f"4. p = P(χ² with {t['df']} df ≥ {n(t['statistic'])}) = **{p_text(t['p'])}**.",
            f"5. Cramér's V = √(χ² / (n × (min(rows, columns) − 1))) = √({n(t['statistic'])} / "
            f"({n(t['n'])} × {small})) = **{n(t['effect_size'])}** ({t.get('effect', '')}).",
        ]
    )


def fit_test(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for r in result.get("results") or []:
        if r.get("cells") and not r.get("categories"):
            out += [f"### {r.get('column', 'Together')}", "", _together_cells(r), ""]
        else:
            out += [*_fit_one(r, str(result.get("expected"))), ""]
    if not out:
        raise ProofError("This fit test has no results to prove.")
    return out


# ===== outliers =====


def outliers(result: dict[str, Any]) -> list[str]:
    column, t = result["column"], float(result["threshold"])
    if result.get("look") == "frequency":
        m, scale = result.get("log_median"), result.get("log_scale")
        out: list[str] = [
            f"**Method:** how often each value of {column} appears, judged on two scales; a value is "
            "flagged only when its count is unusual on both, and its score is the smaller of the two.",
            "",
            f"1. Count each value (c). The typical count is the median: **{n(result.get('typical'))}**, over "
            f"{n(result['distinct'])} different values.",
            f"2. Against the other counts, on the log scale: z₁ = |ln c − {n(m)}| / {n(scale)} (the median "
            "of ln c, and its spread: the median absolute deviation ÷ 0.6745).",
            "3. Against counting noise (Anscombe): z₂ = |2√(c + 3/8) − 2√(typical + 3/8)|.",
            f"4. Score = ± min(z₁, z₂), flagged beyond **{n(t)}**.",
        ]
        flagged = result.get("outliers") or []
        if flagged and m is not None and scale:
            rows = []
            typical = float(result.get("typical") or 0)
            for o in flagged:
                c = float(o["count"])
                z1 = abs(math.log(c) - float(m)) / float(scale)
                z2 = abs(2 * math.sqrt(c + 0.375) - 2 * math.sqrt(typical + 0.375))
                rows.append([o["value"], o["count"], z1, z2, o["score"]])
            out += ["", table(["Value", "Count c", "z₁ (log scale)", "z₂ (noise)", "Score"], rows)]
        else:
            out += ["", "No value passed both scales, so none is flagged."]
        return out
    method = result.get("method")
    out = []
    for f in result.get("fences") or []:
        where = f" (group {f['group']})" if f.get("group") is not None else ""
        out.append(f"### Fences{where}: {n(f['rows'])} values")
        if method == "iqr":
            q1, q3 = f.get("q1"), f.get("q3")
            iqr = (q3 - q1) if q1 is not None and q3 is not None else None
            out += [
                f"Quartiles: Q1 = {n(q1)}, Q3 = {n(q3)}; IQR = Q3 − Q1 = **{n(iqr)}**.",
                f"Low fence = Q1 − {n(t)} × IQR = {n(q1)} − {n(t)} × {n(iqr)} = **{n(f['low_fence'])}**.",
                f"High fence = Q3 + {n(t)} × IQR = {n(q3)} + {n(t)} × {n(iqr)} = **{n(f['high_fence'])}**.",
                "Score = how many IQRs beyond the nearer quartile.",
            ]
        elif method == "zscore":
            out += [
                f"Mean = {n(f.get('mean'))}, standard deviation = {n(f.get('sd'))}.",
                f"Fences = mean ± {n(t)} × sd = {n(f.get('mean'))} ± {n(t)} × {n(f.get('sd'))} = "
                f"**{n(f['low_fence'])}** to **{n(f['high_fence'])}**.",
                "Score (z) = (value − mean) / sd.",
            ]
        else:
            mad = f.get("mad")
            scale = float(mad) / 0.6745 if mad else None
            note = "" if mad else " (it is 0, so the mean absolute deviation × 1.2533 stands in)"
            out += [
                f"Median = **{n(f.get('median'))}**; median absolute deviation (MAD) = **{n(mad)}**{note}.",
                f"Robust spread = MAD ÷ 0.6745 = {n(scale)} (0.6745 makes it match a standard deviation "
                "on normal data).",
                f"Fences = median ± {n(t)} × spread = {n(f.get('median'))} ± {n(t)} × {n(scale)} = "
                f"**{n(f['low_fence'])}** to **{n(f['high_fence'])}**.",
                "Score (robust z) = (value − median) / spread.",
            ]
        out.append(f"Outside the fences: {n(f['low'])} below, {n(f['high'])} above.")
        out.append("")
    top = result.get("outliers") or []
    if top:
        out += [
            "### The values furthest out",
            "",
            table(["Value", "Score"], [[o["value"], o["score"]] for o in top[:15]]),
        ]
    return [
        f"**Method:** {METHODS.get(str(method), METHODS['robust'])} on {column}, flagged beyond {n(t)}.",
        "",
        *out,
    ]


METHODS: dict[str, str] = {
    "iqr": "Tukey fences (quartiles)",
    "zscore": "z-scores (mean and standard deviation)",
    "robust": "robust z (median and median absolute deviation)",
}


# ===== odds =====


def odds(result: dict[str, Any]) -> list[str]:
    values = result.get("values") or []
    total = int(result.get("rows") or sum(int(v["rows"]) for v in values))
    column = result["column"]
    if not result.get("outcome"):
        rows = [[v["value"], v["rows"], v["share"], v["one_in"], v["odds_against"]] for v in values]
        return [
            f"**Plain odds of each value of {column}**, over n = {n(total)} rows.",
            "",
            "Share = count / n · 1 in = n / count · odds against = (n − count) / count.",
            "",
            table(["Value", "Count", "Share", "1 in", "Odds against"], rows),
        ]
    yes_total = int(result.get("outcome_rows") or 0)
    rows = []
    for v in values:
        a = int(v["outcome_rows"])
        b, c = int(v["rows"]) - a, yes_total - a
        d = (total - int(v["rows"])) - c
        cells = [a, b, c, d]
        half = 0 in cells
        a_, b_, c_, d_ = (x + (0.5 if half else 0.0) for x in cells)
        ratio = (a_ * d_) / (b_ * c_)
        se = math.sqrt(1 / a_ + 1 / b_ + 1 / c_ + 1 / d_)
        rows.append(
            [
                v["value"],
                a,
                b,
                c,
                d,
                f"({n(a_)}×{n(d_)}) / ({n(b_)}×{n(c_)}) = {n(ratio)}",
                se,
                f"{n(ratio * math.exp(-Z95 * se))} to {n(ratio * math.exp(Z95 * se))}",
            ]
        )
    out = [
        f"**Odds ratios for {result['outcome']}** by {column}: each value against every other value, "
        f"over n = {n(total)} rows, {n(yes_total)} of them {result['outcome']} "
        f"(rate {n(yes_total / total if total else 0)}).",
        "",
        "For each value, a 2 × 2 table: a = this value and the outcome, b = this value without it, "
        "c = other values with it, d = other values without it.",
        "",
        "- Odds ratio = (a × d) / (b × c). When a cell is 0, 0.5 is added to all four (Haldane).",
        "- Its spread on the log scale: SE = √(1/a + 1/b + 1/c + 1/d) (Woolf).",
        f"- 95% range = OR × e^(∓{n(Z95)} × SE). A range that doesn't cross 1 is a clear difference.",
        "",
        table(["Value", "a", "b", "c", "d", "Odds ratio", "SE (log)", "95% range"], rows, {1, 2, 3, 4, 6}),
    ]
    if result.get("together"):
        out += ["", "### Is the outcome tied to the column at all?", "", _together_cells(result["together"])]
    return out


# ===== find predictors =====


def predictors(result: dict[str, Any]) -> list[str]:
    rows = []
    for c in result.get("candidates") or []:
        levels = c.get("levels") or [None, None]
        small = min(levels) - 1 if all(isinstance(x, int) for x in levels) else None
        stat = c.get("statistic")
        check = (
            f"√({n(stat)} / ({n(c['rows'])} × {small})) = {n(math.sqrt(float(stat) / (c['rows'] * small)))}"
            if stat is not None and small
            else n(c["v"])
        )
        rows.append([c["column"], stat, c["df"], p_text(c["p"]), check, c["strength"]])
    return [
        f"**Method:** each column tested together with {result['target']} (numbers cut into equal-count "
        "buckets first): Pearson's chi-square test of independence, then Cramér's V for how strong.",
        "",
        "- χ² = Σ (O − E)² / E over the table of the two columns' values; df = (r − 1)(c − 1).",
        "- V = √(χ² / (n × (min(r, c) − 1))): 0 is unrelated, 1 decides it. Under 0.1 none, under 0.2 weak, "
        "under 0.3 moderate, else strong.",
        "",
        table(["Column", "χ²", "df", "p", "V = √(χ² / (n × (min − 1)))", "Strength"], rows, {1, 2}),
    ]


# ===== regression =====


def regression(result: dict[str, Any]) -> list[str]:
    coefs = result.get("coefficients") or []
    fit = result.get("fit") or {}
    model = result.get("model") or {}
    if result.get("kind") == "logistic":
        rows = [
            [
                c["term"],
                c["coefficient"],
                c["se"],
                f"{n(c['coefficient'])} / {n(c['se'])} = {n(c['z'])}",
                p_text(c["p"]),
                f"e^{n(c['coefficient'])} = {n(c['odds_ratio'])}",
                f"{n(c['low'])} to {n(c['high'])}",
            ]
            for c in coefs
        ]
        dev, null = fit.get("deviance"), fit.get("null_deviance")
        return [
            f"**Model:** logistic regression of {result.get('outcome')} on "
            f"{', '.join(result.get('predictors', []))}, "
            f"n = {n(result['n'])}, fitted by iteratively reweighted least squares "
            f"({n(result.get('iterations'))} iterations).",
            "",
            "log(odds) = b₀ + b₁x₁ + …; each b is found where the log-likelihood is highest.",
            "",
            f"- z = b / SE; p = 2 × (1 − Φ(|z|)); odds ratio = e^b; 95% range = e^(b ∓ {n(Z95)} × SE).",
            "",
            table(["Term", "b", "SE", "z = b / SE", "p", "Odds ratio", "95% range"], rows, {1, 2}),
            "",
            f"- Deviance = −2 × log-likelihood = {n(dev)}; with no predictors {n(null)}.",
            f"- McFadden's R² = 1 − deviance / null deviance = 1 − {n(dev)} / {n(null)} = "
            f"**{n(fit.get('pseudo_r2'))}**.",
            f"- Right on {n(fit.get('accuracy'))} of rows "
            f"(guessing the commoner answer: {n(fit.get('majority'))}).",
        ]
    df = model.get("df") or (int(result["n"]) - len(coefs))
    t_star = t_quantile(0.975, float(df)) if df else float("nan")
    rows = [
        [
            c["term"],
            c["coefficient"],
            c["se"],
            f"{n(c['coefficient'])} / {n(c['se'])} = {n(c['t'])}",
            p_text(c["p"]),
            f"{n(c['coefficient'])} ± {n(t_star)} × {n(c['se'])} = {n(c['low'])} to {n(c['high'])}",
        ]
        for c in coefs
    ]
    sigma2 = model.get("sigma2")
    sse = float(sigma2) * float(df) if sigma2 is not None else None
    r2 = fit.get("r2")
    sst = sse / (1 - float(r2)) if sse is not None and r2 is not None and r2 < 1 else None
    p = len(coefs)
    references = ", ".join(f"{k}: {v}" for k, v in (result.get("references") or {}).items()) or "none"
    return [
        f"**Model:** linear regression (ordinary least squares) of {result['target']} on "
        f"{', '.join(result.get('predictors', []))}, n = {n(result['n'])} rows, {p} terms.",
        "",
        "ŷ = b₀ + b₁x₁ + …, with b = (XᵀX)⁻¹ Xᵀy: the b that make the squared misses smallest. Number "
        "columns enter centred on their mean; a category enters as one 0/1 column per value, against its "
        f"commonest value ({references}).",
        "",
        f"- Leftover variance σ² = SSE / (n − p) = {n(sse)} / {n(df)} = **{n(sigma2)}**.",
        "- SE of each b = √(σ² × its diagonal entry of (XᵀX)⁻¹).",
        f"- t = b / SE; p = 2 × P(T with {n(df)} df ≥ |t|); 95% range = b ± t* × SE, t* = {n(t_star)}.",
        "",
        table(["Term", "b", "SE", "t = b / SE", "p", "95% range"], rows, {1, 2}),
        "",
        f"- R² = 1 − SSE / SST = 1 − {n(sse)} / {n(sst)} = **{n(r2)}** (the share of the ups and downs the "
        "model explains).",
        f"- Adjusted R² = 1 − (1 − R²)(n − 1) / (n − p) = **{n(fit.get('adj_r2'))}**.",
        f"- Typical miss (RMSE) = **{n(fit.get('rmse'))}**.",
        f"- F = ((SST − SSE) / (p − 1)) / (SSE / (n − p)) = **{n(fit.get('f'))}**, "
        f"p = {p_text(fit.get('f_p'))}.",
        "",
        "**Check it yourself:** `statsmodels.formula.api.ols(...).fit().summary()` gives the same b, SE, t, "
        "p and R².",
    ]


def predict(result: dict[str, Any]) -> list[str]:
    terms = result.get("terms") or []
    rows = [[t.get("term"), t.get("x"), t.get("coefficient"), t.get("contribution")] for t in terms]
    total = sum(float(t.get("contribution") or 0) for t in terms)
    lines = [
        "**What-if:** the model's equation with the values you gave put in (numbers centred on their "
        "mean, a category as 1 for its own value and 0 otherwise).",
        "",
        table(["Term", "x (as it enters)", "b", "b × x"], rows) if rows else "",
        "",
    ]
    if result.get("log_odds") is not None:
        lines.append(
            f"log(odds) = Σ b × x = {n(total)}; probability = 1 / (1 + e^−{n(total)}) = "
            f"**{n(result.get('estimate'))}**."
        )
    else:
        lines.append(f"Estimate = Σ b × x = **{n(result.get('estimate'))}**.")
    if result.get("low") is not None:
        lines.append(
            f"Range: {n(result.get('low'))} to {n(result.get('high'))} (the estimate ± t* × √(σ² × (1 + "
            "xᵀ(XᵀX)⁻¹x)): the model's own uncertainty plus a single row's scatter)."
        )
    return lines


# ===== time series and forecast =====


def time_series(result: dict[str, Any]) -> list[str]:
    mult = result.get("multiplicative")
    index = result.get("season_index") or []
    out = [
        f"**Method:** {result.get('what')} totalled per {result.get('period')} "
        f"({result.get('total')}), then split into trend × season × the rest"
        f"{'' if mult else ' (added, not multiplied)'}.",
        "",
        "1. Trend: a centred moving average over one full cycle (the ends carry the nearest average out).",
        f"2. Season: for each position, the median of value {'÷' if mult else '−'} trend over the years "
        "(a second pass replaces big spikes first, so one wild period can't move its month).",
    ]
    if index:
        out += [
            "",
            table(
                ["Position", "Index", "Against a typical period"],
                [
                    [s["position"], s["index"], f"{(s['index'] - 1) * 100:+.0f}%" if mult else n(s["index"])]
                    for s in index
                ],
            ),
            "",
        ]
    anomalies = result.get("anomalies") or []
    out.append(
        "3. Standing out: with the season taken out, each period against a line through its eight nearest "
        "neighbours (within the same stretch between steps). Score = (its distance − the typical "
        "distance) / the robust spread of all distances; beyond 5 it stands out."
    )
    if anomalies:
        out += [
            "",
            table(
                ["Period", "Value", "Distance", "Typical", "Spread", "Score = (d − typical) / spread"],
                [
                    [a["period"], a["value"], a.get("off"), a.get("typical_off"), a.get("spread"), a["score"]]
                    for a in anomalies
                ],
            ),
        ]
    for s in result.get("shifts") or []:
        out.append(
            f"4. A step in the level at {s['period']}: a curved trend plus a step fitted at each cut; "
            f"this cut's step has t = {n(s.get('t'))} (kept above 6), "
            f"from about {n(s.get('before'))} to {n(s.get('after'))}."
        )
    out.append(
        "5. Memory: the correlation of each period's leftover with the one before = "
        f"**{n(result.get('memory'))}**."
    )
    return out


def forecast(result: dict[str, Any]) -> list[str]:
    rows = []
    z80 = normal_quantile(0.9)
    for f in (result.get("forecast") or [])[:24]:
        half = (f["high80"] - f["estimate"]) / z80 if z80 else None
        rows.append(
            [
                f["period"],
                f["estimate"],
                half,
                f"{n(f['low80'])} to {n(f['high80'])}",
                f"{n(f['low95'])} to {n(f['high95'])}",
            ]
        )
    check = result.get("check") or {}
    return [
        f"**Model:** least squares on {n(len(result.get('history') or []))} {result.get('period')}s: "
        f"{result.get('what')} = a + b × t {'+ a step for each level shift ' if result.get('steps') else ''}"
        f"{'+ one term per position in the season ' if result.get('seasonal') else ''}"
        f"{'+ the predictors ' if result.get('predictors') else ''}({n(result.get('terms'))} terms).",
        "",
        f"- The trend b = **{n(result.get('trend_per_period'))}** per {result.get('period')}.",
        "- Each future period: the equation with its t, its season position and the predictors' "
        "futures put in.",
        f"- Its uncertainty SE = √(σ² × (1 + xᵀ(XᵀX)⁻¹x)); 80% range = estimate ± {n(z80)} × SE, 95% ± "
        f"{n(normal_quantile(0.975))} × SE.",
        "",
        table(["Period", "Estimate", "SE", "80% range", "95% range"], rows, {1, 2}),
        "",
        f"- Held-back check: the last {n(check.get('periods'))} {result.get('period')}s were forecast "
        f"from the rest; the typical miss (mean of |actual − forecast|) = "
        f"**{n(check.get('typical_miss'))}**, "
        f"{n((check.get('typical_miss_share') or 0) * 100)}% of the typical actual.",
    ]


# ===== the tie-out =====


def tie_out(result: dict[str, Any]) -> list[str]:
    tol, pct = result.get("tolerance", 0), result.get("tolerance_kind") == "percent"
    grouped = ", ".join(result.get("group_by") or []) or "nothing"
    allowed = f"{n(tol)}% of the left total" if pct else n(tol)
    rows = []
    for g in (result.get("groups") or [])[:MAX_ROWS]:
        label = " · ".join("(blank)" if v is None else str(v) for v in g["group"]) or "All rows"
        for m in g["measures"] or [
            {
                "column": "rows",
                "left": g["left_rows"],
                "right": g["right_rows"],
                "difference": g["row_difference"],
                "ties": g["status"] == "ties",
            }
        ]:
            rows.append(
                [
                    label,
                    m["column"],
                    m["left"],
                    m["right"],
                    f"{n(m['right'] or 0)} − {n(m['left'] or 0)} = {n(m['difference'])}",
                    "ties" if m["ties"] else "off",
                ]
            )
    return [
        f"**Tie-out:** each group's totals on both sides, grouped by {grouped}.",
        "",
        f"Difference = right − left. A total ties when |difference| ≤ {allowed}; "
        "a group ties when its row counts "
        "match and every total ties. A group on one side only has no total on the other: its whole total is "
        "the difference.",
        "",
        table(["Group", "Total of", "Left", "Right", "Difference", "Status"], rows, {2, 3}),
        "",
        f"Grand total: rows {n(result['total']['left_rows'])} and {n(result['total']['right_rows'])}; "
        + "; ".join(
            f"{m['column']} {n(m['left'])} and {n(m['right'])}, difference {n(m['difference'])}"
            for m in result["total"]["measures"]
        )
        + ".",
    ]


PROOFS: dict[str, tuple[str, Callable[[dict[str, Any]], list[str]]]] = {
    "fit_test": ("Fit test", fit_test),
    "outliers": ("Outliers", outliers),
    "odds": ("Odds", odds),
    "predictors": ("Find predictors", predictors),
    "regression": ("Regression", regression),
    "predict": ("What-if", predict),
    "time_series": ("Time series", time_series),
    "forecast": ("Forecast", forecast),
    "tie_out": ("Tie-out", tie_out),
}


def proof(analysis: str, result: Any, dataset: str = "") -> dict[str, str]:
    """{"title", "markdown"}: the proof of one result, ready for a Sandbox note."""
    if analysis not in PROOFS:
        raise ProofError(f"Show the math covers {', '.join(PROOFS)}.")
    if not isinstance(result, dict):
        raise ProofError("Send the result the window showed.")
    name, build = PROOFS[analysis]
    try:
        body = build(result)
    except (KeyError, TypeError, ZeroDivisionError) as error:
        raise ProofError(f"This {name.lower()} result is missing what its proof needs ({error}).") from error
    where = f" on {dataset}" if dataset else ""
    title = f"The math: {name}{where}"
    return {"title": title, "markdown": "\n".join([f"## {title}", "", *body]).strip() + "\n"}
