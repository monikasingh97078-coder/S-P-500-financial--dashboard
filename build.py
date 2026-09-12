"""
Build script for the Financial Performance dashboard.

Loads the two raw S&P 500 CSVs into a SQLite database, derives the
financial metrics we don't have directly (revenue, net income, net
margin), runs the analysis queries, and writes each result to its own
CSV in results/. This mirrors the "SQL-first" workflow: every number
on the dashboard traces back to a query you can read below.
"""
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "financial.db"
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

DB_PATH.unlink(missing_ok=True)
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# ---------------------------------------------------------------------------
# 1. Load the raw CSVs as-is (text columns stay text; we clean with SQL next)
# ---------------------------------------------------------------------------
cur.execute("""
    CREATE TABLE financials (
        symbol TEXT, name TEXT, sub_industry TEXT, price TEXT,
        price_earnings TEXT, dividend_yield TEXT, eps TEXT,
        week_low_52 TEXT, week_high_52 TEXT, market_cap TEXT,
        ebitda TEXT, price_sales TEXT, price_book TEXT, sec_filings TEXT
    )
""")
with open(ROOT / "data" / "constituents-financials.csv") as f:
    reader = csv.reader(f)
    next(reader)
    cur.executemany("INSERT INTO financials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", reader)

cur.execute("""
    CREATE TABLE constituents (
        symbol TEXT, security TEXT, gics_sector TEXT, gics_sub_industry TEXT,
        hq TEXT, date_added TEXT, cik TEXT, founded TEXT
    )
""")
with open(ROOT / "data" / "constituents.csv") as f:
    reader = csv.reader(f)
    next(reader)
    cur.executemany("INSERT INTO constituents VALUES (?,?,?,?,?,?,?,?)", reader)

conn.commit()

# ---------------------------------------------------------------------------
# 2. Join + derive numeric columns into a clean `companies` table.
#    Revenue and net income aren't columns in the raw data, but they fall
#    out of ratios that are: Price/Sales = MarketCap / Revenue, and
#    Price/Earnings = MarketCap / NetIncome (both use the same share count).
# ---------------------------------------------------------------------------
cur.executescript("""
DROP TABLE IF EXISTS companies;
CREATE TABLE companies AS
SELECT
    f.symbol,
    f.name,
    c.gics_sector                                   AS sector,
    c.gics_sub_industry                              AS sub_industry,
    CAST(f.market_cap AS REAL)                       AS market_cap,
    CAST(f.dividend_yield AS REAL)                   AS dividend_yield,
    CAST(f.price_book AS REAL)                       AS price_book,
    CASE WHEN f.price_sales NOT IN ('', '0') AND f.market_cap != ''
         THEN CAST(f.market_cap AS REAL) / CAST(f.price_sales AS REAL) END AS revenue,
    CASE WHEN f.price_earnings NOT IN ('', '0') AND f.market_cap != ''
         THEN CAST(f.market_cap AS REAL) / CAST(f.price_earnings AS REAL) END AS net_income
FROM financials f
LEFT JOIN constituents c ON c.symbol = f.symbol;

-- net margin depends on both derived columns, so it needs its own pass
ALTER TABLE companies ADD COLUMN net_margin REAL;
UPDATE companies
SET net_margin = 100.0 * net_income / revenue
WHERE revenue IS NOT NULL AND revenue > 0 AND net_income IS NOT NULL;

ALTER TABLE companies ADD COLUMN margin_tier TEXT;
UPDATE companies SET margin_tier = CASE
    WHEN net_margin IS NULL THEN NULL
    WHEN net_margin < 0 THEN '< 0%'
    WHEN net_margin < 10 THEN '0-10%'
    WHEN net_margin < 20 THEN '10-20%'
    WHEN net_margin < 30 THEN '20-30%'
    ELSE '30%+' END;

ALTER TABLE companies ADD COLUMN revenue_tier TEXT;
UPDATE companies SET revenue_tier = CASE
    WHEN revenue IS NULL THEN NULL
    WHEN revenue < 10e9 THEN '< $10B'
    WHEN revenue < 50e9 THEN '$10-50B'
    WHEN revenue < 100e9 THEN '$50-100B'
    ELSE '$100B+' END;
""")
conn.commit()


def save(name, sql):
    """Run a query and write it straight to results/<name>.csv"""
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    with open(RESULTS_DIR / f"{name}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"  results/{name}.csv  ({len(rows)} rows)")


print("Running analysis queries...")

# (1) Headline KPIs: combined revenue, combined income, avg margin, avg yield
save("summary", """
    SELECT
        SUM(revenue)                                   AS combined_revenue,
        SUM(net_income)                                AS combined_net_income,
        AVG(net_margin)                                AS avg_net_margin_pct,
        AVG(dividend_yield) * 100                      AS avg_dividend_yield_pct,
        COUNT(*)                                        AS company_count
    FROM companies
    WHERE revenue IS NOT NULL AND net_income IS NOT NULL
""")

# (2) Top 10 by revenue
save("top10_revenue", """
    SELECT symbol, name, sector, revenue, net_income, net_margin
    FROM companies WHERE revenue IS NOT NULL
    ORDER BY revenue DESC LIMIT 10
""")

# (3) Top 10 by market cap
save("top10_market_cap", """
    SELECT symbol, name, sector, market_cap, revenue, net_margin
    FROM companies WHERE market_cap IS NOT NULL
    ORDER BY market_cap DESC LIMIT 10
""")

# (4) Top 10 by net margin (require meaningful revenue so tiny-revenue
#     companies with noisy ratios don't dominate)
save("top10_margin", """
    SELECT symbol, name, sector, net_margin, revenue, net_income
    FROM companies
    WHERE net_margin IS NOT NULL AND revenue > 1e9
    ORDER BY net_margin DESC LIMIT 10
""")

# (5) Top 10 by dividend yield
save("top10_dividend_yield", """
    SELECT symbol, name, sector, dividend_yield * 100 AS dividend_yield_pct, revenue
    FROM companies WHERE dividend_yield IS NOT NULL
    ORDER BY dividend_yield DESC LIMIT 10
""")

# (6) Net margin distribution (tiers)
save("margin_distribution", """
    SELECT margin_tier, COUNT(*) AS company_count
    FROM companies WHERE margin_tier IS NOT NULL
    GROUP BY margin_tier
    ORDER BY CASE margin_tier
        WHEN '< 0%' THEN 0 WHEN '0-10%' THEN 1 WHEN '10-20%' THEN 2
        WHEN '20-30%' THEN 3 ELSE 4 END
""")

# (7) Revenue and margin rolled up by sector (for the sector filter)
save("by_sector", """
    SELECT sector,
           COUNT(*)             AS company_count,
           SUM(revenue)         AS combined_revenue,
           AVG(net_margin)      AS avg_net_margin_pct
    FROM companies
    WHERE sector IS NOT NULL AND revenue IS NOT NULL
    GROUP BY sector
    ORDER BY combined_revenue DESC
""")

# (8) Full per-company table, for client-side filtering in the dashboard
save("companies_full", """
    SELECT symbol, name, sector, revenue, net_income, net_margin,
           market_cap, dividend_yield * 100 AS dividend_yield_pct,
           margin_tier, revenue_tier
    FROM companies
    WHERE revenue IS NOT NULL AND net_income IS NOT NULL
    ORDER BY revenue DESC
""")

conn.close()
print("Done.")
