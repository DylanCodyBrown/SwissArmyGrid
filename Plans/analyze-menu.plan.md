# The Analyze menu: odds, predictors, regression, forecast (with the fit test and outliers)

The owner's brief (2026-11-13): "If you combine the app's ability to create categorical buckets and that
chi-square goodness of fit analysis you can do some powerful things. The next logical analysis would be
a very simple fast multiple regression with a forecast continuation component, so you can forecast where
a metric would be after you use the chi-square to determine what columns are good predictors. All of
these pieces (chi-square, a multiple regression and deviation) should work with each other to help an
analyst predict a metric." And: "One of the most underrated analyses is a simple odds analysis." The
analyses live in a drop-down in the Table row; each has its own form.

## The owner's decisions

1. **Forecast** means both: over time (a date column, trend and seasonality) and what-if (a prediction
   for values you type).
2. **Odds** means both: how often each value shows up, and the odds of an outcome given each value
   (odds ratios).
3. **Yes/no targets:** yes. Logistic regression, whose results are odds ratios (the odds analysis and
   the regression speak the same language).
4. **Separate tools with hand-off buttons**, not one guided path.
5. **Results go where the analyst chooses:** to the Sandbox (a card), to a grid (when the result is a
   table), or as Python to the notebook (a cell in the open marimo notebook, in its own tab).
6. **Time series analysis** too, "built the same way" (a form, hand-offs, the three result actions).
7. **Future values of predictors** in a forecast over time: picked per predictor (keep the last value,
   continue its own trend, or type a value). Time parts (trend, month of year, weekday) are always known.

## How the pieces work together

```
Odds ──────────► Fit test (together) ──► Find predictors ──► Regression ──► Forecast
  │  "West is late 4.2x as often"   "related?"   ranked, redundant pairs   │  what-if   over time
  │                                                                       ▼
  └──── Outliers (amount, frequency) ◄──── Check the misses (outliers of the model's misses)
```

- **Odds** describes categories. With an outcome it gives each value's odds ratio; **Test together**
  hands the two columns to the fit test.
- **Fit test** and **Outliers** stay in the Column row as shortcuts and join the menu. Outliers gains
  **Exclude these** beside Show these rows.
- **Find predictors** buckets the target (quartiles; a yes/no target is already categories) and
  numeric candidates, tests each candidate together with the target, and ranks by effect size
  (Cramér's V; for number against number also the correlation). It flags redundant pairs (two
  candidates that say the same thing) so the regression gets one of them. **Regression with these**
  hands over the target and the ticked predictors.
- **Regression**: linear for a number target, logistic for a yes/no target (the form asks which value
  is "yes"). Categorical predictors become indicator columns (the most common value is the
  reference; at most 30 values each, the rest "other"). Optional: leave out the target's outliers
  (robust fences). Results in words: each predictor's effect with its range, how much of the target the
  model explains, the typical miss. **What-if**: a field per predictor and the prediction with its
  range. **Add predicted column** and **Add miss column** are SQL steps (the fitted formula as plain
  numbers, so Copy as SQL still works); **Check the misses** opens Outliers on the miss column: the
  rows the model cannot explain, the strongest "liars" of all. **Forecast** hands the model over.
- **Forecast** over time: a date column, the metric and how to total it per period (sum, average,
  count; day, week, month, quarter), how many periods ahead, the trend, seasonality (suggested from
  the data: month of year for monthly data with two years, weekday for daily), and optional
  predictors, each with its future-value choice. It holds back the last few periods to report an
  honest typical miss, and draws history, fitted line and forecast with an 80% and a 95% band.

- **Time series**: a date column, a metric totalled per period (sum, average, count; day, week, month,
  quarter), optionally one series per value of a category. Its **shape** (trend, the seasonal pattern
  in words such as "December runs 34% above a typical month", what is left over), **growth** (period
  over period, the same period last year, a rolling average), **anomalies in time** (spikes and dips
  beyond robust fences, missing periods; **Show these rows** keeps the rows of those periods, **Check
  with Outliers** looks inside them), **level shifts** (where the series stepped and stayed), and
  **memory** (how much one period predicts the next: whether a forecast has anything to go on).
  Hands over to **Forecast** (same date, metric and period) and to the **Fit test** (is the seasonal
  pattern real or chance?).

## Every result's actions

- **Send to Sandbox**: a card with the result in words and its chart or table.
- **Open as grid**: any tabular result (odds table, predictor ranking, coefficients, forecast) as a
  new grid, through the existing JSON-to-Arrow path.
- **To notebook**: Python that redoes the analysis, as a cell in the open notebook. DuckDB does the
  counting and the sums (the analyst's own table by its variable name); the statistics come from
  `swissarmygrid.stats`, a public, standard-library-only module (the same math the server uses), so
  the code runs in any Python with duckdb and swissarmygrid. Verified like Copy as code: run in a fresh
  namespace and compared with the window's numbers before it is offered.

## The page

**Analyze** at the end of the Table row: a menu of Odds, Fit test, Find predictors, Outliers,
Regression, Time series, Forecast. Each opens its form, filled from the selected columns. Hand-off buttons open the
next form already filled. The agent gets one tool per analysis (`analyze_odds`, `analyze_predictors`,
`analyze_regression`, `analyze_predict`, `analyze_time_series`, `analyze_forecast`, beside `column_fit_test` and
`column_outliers`).

## The math (standard library only, no new dependency)

- DuckDB computes every sum in one pass over the view: counts, and for regression the cross-products
  (X'X and X'y, with indicators as CASE expressions, numbers centered and scaled for stability).
  Logistic regression is iteratively reweighted least squares: a pass per iteration with the current
  coefficients as a SQL expression, usually under ten.
- Python solves the small system (Cholesky), and gives standard errors, t and F p-values (the
  regularized incomplete beta function), normal quantiles for ranges, Cramér's V, odds ratios with
  Woolf's 95% interval.
- Tests use made-up data with planted answers: the regression recovers planted coefficients, the
  logistic regression planted odds ratios, the forecast a planted trend and season; small-sample
  marks appear under 30 rows.

## Cautions shown, once and plainly

Association, not cause. A forecast is a range, not a line. Odds on under 30 rows are marked as rough.
Rows with a blank in any used column are left out, and the count is shown.

## Phases

- **A. (built)** The Analyze menu; the three result actions; Odds; Find predictors; Fit test and
  Outliers in the menu with Exclude these; `swissarmygrid.stats` and the notebook code for these.
  Send to Sandbox is a note for now: the Sandbox's markdown has no tables, and server-added Plotly
  cards wait for the Sandbox server part.
- **B. (built)** Regression (linear and logistic), what-if, predicted and miss columns, Check the misses.
- **C. (built)** Time series (its decomposition is what the forecast continues), then Forecast over time with
  per-predictor future values and the held-back check.

Each phase: domain math tests first, routes, agent tools, the forms, a browser test, the docs.

## Layout

domain/stats.py (distributions, linear algebra), domain/analysis.py (fit test, outliers, odds,
predictors), domain/models.py (regression, time series, forecast: encoding, query text, iterations);
services/analysis.py and services/models.py; routes_analysis.py and routes_models.py;
web/src/analyzeMenu.ts (the menu and the shared result actions), analysisDialog.ts (fit test,
outliers, odds, predictors), modelDialog.ts (regression, what-if, forecast); src/swissarmygrid/stats.py
(the public face of domain/stats.py and the analysis math, for generated code).

## Notes from building it

- Files: domain/regression.py and domain/timeseries.py (not models.py: services/relationships.py already
  has a Models), with services and routes of the same names.
- Anomalies in time are judged against a straight line through each period's eight nearest neighbours
  within the same stretch between steps, beyond 5 robust spreads: on 200 made-up series with a planted
  spike and step, 1 in 100 got a false flag and the planted spike scored 15 or more. A centred moving
  average blurred steps into false dips; a median of one-sided neighbours lagged at the ends; a log-scale
  straight trend curved away from a straight one in sales.
- Steps in the level: for each cut, a curved trend plus a step is fitted; the surest step (t of 6 or
  more) is kept. A straight trend alone made a bending series look stepped.
- The season is the median ratio per position from a second pass in which big spikes are replaced, so
  one wild June does not raise every June.
- The forecast carries found steps forward; its held-back check uses only steps the earlier periods saw.
- Not built yet: a Fit test on the seasonal pattern itself (is it chance?), a separate forecast per
  line of a split, and holiday or event dummies.
