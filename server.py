#!/usr/bin/env python3
"""
Dividend Holdings Dashboard - local server.

Run:   python server.py            (live data from Yahoo Finance, built-in client, no packages needed)
       python server.py --demo     (synthetic sample data, works offline)
       python server.py --public-demo   (read-only sample portfolio for a public website, see DEPLOY.md)
Then open http://127.0.0.1:8765 in your browser.

Your holdings live in holdings.csv next to this file. To keep them in a
cloud-synced folder (OneDrive, Dropbox, iCloud Drive) shared by several computers,
put the full path of that file in holdings_path.txt, or run with --holdings PATH,
or set the DIVIDEND_HOLDINGS environment variable.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOLDINGS_CSV = HERE / "holdings.csv"   # may be replaced in main() by a synced location
HOLDINGS_PATH_FILE = HERE / "holdings_path.txt"
BACKUPS_TO_KEEP = 20
CACHE_FILE = HERE / ".cache.json"
INDEX_HTML = HERE / "index.html"
CACHE_TTL_SECONDS = 15 * 60
CSV_FIELDS = ["symbol", "shares", "cost_basis", "frequency", "core_focus", "distribution_type", "strategy", "exposure", "flagged", "notes"]

DEMO = False
_lock = threading.Lock()

# Public read-only demo (python server.py --public-demo, or PUBLIC_DEMO=1), for hosting on a website.
# Uses the sample portfolio in demo/, never your holdings.csv, and turns off every way to change
# data or stop the server. Market data is cached for hours so visitors don't trigger rate limits.
PUBLIC_DEMO = False
DEMO_DIR = HERE / "demo"
DEMO_HOLDINGS = DEMO_DIR / "demo-holdings.csv"
DEMO_PLANS = DEMO_DIR / "demo-scenarios.json"
# Fund details (expense ratio, category, top holdings, sectors...) saved on a computer Yahoo answers,
# with python server.py --save-demo-snapshot. Yahoo often refuses these to hosting services.
DEMO_SNAPSHOT = DEMO_DIR / "fund-snapshot.json"
SNAPSHOT_INFO_KEYS = ("quoteType", "longName", "category", "fundFamily", "annualReportExpenseRatio", "netExpenseRatio",
                      "totalAssets", "marketCap", "beta3Year", "beta", "threeYearAverageReturn",
                      "fiveYearAverageReturn", "sector", "industry", "longBusinessSummary")
SNAPSHOT_EXTRA = ["SPY", "QQQ"]  # benchmarks people often add in Symbol comparison / Research
_snapshot: dict | None = None
PUBLIC_CACHE_HOURS = 6          # how long live prices are reused in the public demo
PUBLIC_NEW_LOOKUPS = 40         # new symbols visitors may look up per window...
PUBLIC_LOOKUP_WINDOW = 10 * 60  # ...of this many seconds, so a busy page can't hammer Yahoo
_lookups: list[float] = []
_lookups_lock = threading.Lock()
_yahoo_slots = threading.Semaphore(3)  # at most 3 symbols fetched from Yahoo at once (public demo)
_failed: dict[str, float] = {}         # public demo: symbol -> when Yahoo last failed for it
PUBLIC_RETRY_AFTER = 15 * 60           # don't ask Yahoo again for a failed symbol sooner than this


def _public_lookup_ok() -> bool:
    """Count one uncached lookup against the public demo's budget. False when it is used up."""
    now = time.time()
    with _lookups_lock:
        _lookups[:] = [t for t in _lookups if now - t < PUBLIC_LOOKUP_WINDOW]
        if len(_lookups) >= PUBLIC_NEW_LOOKUPS:
            return False
        _lookups.append(now)
        return True


BUSY_MSG = "The live demo has looked up a lot of new symbols in the last few minutes. Try again shortly."


def _load_snapshot() -> dict:
    global _snapshot
    if _snapshot is None:
        try:
            _snapshot = json.loads(DEMO_SNAPSHOT.read_text(encoding="utf-8")).get("symbols", {})
        except Exception:
            _snapshot = {}
    return _snapshot


def _fill_from_snapshot(symbol: str, raw: dict) -> None:
    """Public demo: when Yahoo held back the fund details, use the saved snapshot for this symbol."""
    if not (raw.get("fund") or {}).get("error"):
        return
    snap = _load_snapshot().get(symbol)
    if not snap:
        return
    info = raw["info"]
    for k, v in (snap.get("info") or {}).items():
        if info.get(k) in (None, ""):
            info[k] = v
    raw["fund"] = dict(snap.get("fund") or {})
    hist = raw.get("history") or []
    if not info.get("regularMarketPreviousClose") and len(hist) >= 2:
        info["regularMarketPreviousClose"] = hist[-2]["close"]  # for the day change


def save_demo_snapshot() -> None:
    """Fetch fund details for the demo portfolio and save them to demo/fund-snapshot.json."""
    syms = []
    with DEMO_HOLDINGS.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if sym and not sym.startswith("#") and sym not in syms:
                syms.append(sym)
    syms += [s for s in SNAPSHOT_EXTRA if s not in syms]
    out, failed = {}, []
    for sym in syms:
        try:
            raw = fetch_raw(sym)
            fund = {k: v for k, v in (raw.get("fund") or {}).items() if k != "error"}
            if (raw.get("fund") or {}).get("error"):
                raise RuntimeError(raw["fund"]["error"])
            out[sym] = {"info": {k: raw["info"][k] for k in SNAPSHOT_INFO_KEYS if raw["info"].get(k) not in (None, "")},
                        "fund": fund}
            print(f"  {sym:6} ok")
        except Exception as e:
            failed.append(sym)
            print(f"  {sym:6} failed: {e}")
    if not out:
        print("Nothing saved: Yahoo didn't return fund details. Check your internet connection and try again.")
        sys.exit(1)
    DEMO_SNAPSHOT.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "symbols": out}, indent=1), encoding="utf-8")
    print(f"\nSaved fund details for {len(out)} symbols to {DEMO_SNAPSHOT}"
          + (f" ({len(failed)} failed: {', '.join(failed)})" if failed else ""))


# --------------------------------------------------------------------------
# Holdings file
# --------------------------------------------------------------------------
def _num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _flag(v) -> str:
    """Yes/no column (flagged): anything like yes, y, x, true, 1 counts as set."""
    return "yes" if str(v or "").strip().lower() in ("yes", "y", "x", "true", "1", "\u2713", "\u2714") else ""


def read_holdings() -> list[dict]:
    example = HERE / "holdings.example.csv"
    if not HOLDINGS_CSV.exists() and example.exists():
        HOLDINGS_CSV.write_bytes(example.read_bytes())
    if not HOLDINGS_CSV.exists():
        write_holdings([
            {"symbol": "SCHD", "shares": "", "cost_basis": "", "distribution_type": "", "notes": ""},
        ])
    rows = []
    with HOLDINGS_CSV.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if not sym or sym.startswith("#"):
                continue
            rows.append({
                "symbol": sym,
                "shares": _num(r.get("shares")),
                "cost_basis": _num(r.get("cost_basis")),
                "frequency": (r.get("frequency") or "").strip(),
                "core_focus": (r.get("core_focus") or "").strip(),
                "distribution_type": (r.get("distribution_type") or "").strip(),
                "strategy": (r.get("strategy") or "").strip(),
                "exposure": (r.get("exposure") or "").strip(),
                "flagged": _flag(r.get("flagged")),
                "notes": (r.get("notes") or "").strip(),
            })
    return rows


def _csv_row(r: dict) -> dict:
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return int(v)  # 100 rather than 100.0
        return v
    return {k: cell(r.get(k)) for k in CSV_FIELDS}


def write_holdings(rows: list[dict]) -> None:
    # Write to a temp file and swap it in, so a sync client (OneDrive, Dropbox)
    # or a crash never sees a half-written holdings file.
    HOLDINGS_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOLDINGS_CSV.with_name(HOLDINGS_CSV.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(_csv_row(r))
    os.replace(tmp, HOLDINGS_CSV)


def backup_holdings() -> str | None:
    """Copy the current holdings file into backups/ before it is overwritten.
    Keeps the newest BACKUPS_TO_KEEP copies. Returns the backup path, if one was made."""
    if not HOLDINGS_CSV.exists():
        return None
    bdir = HOLDINGS_CSV.parent / "backups"
    bdir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{HOLDINGS_CSV.stem}-{stamp}.csv"
    n = 1
    while dest.exists():
        n += 1
        dest = bdir / f"{HOLDINGS_CSV.stem}-{stamp}-{n}.csv"
    dest.write_bytes(HOLDINGS_CSV.read_bytes())
    old = sorted(bdir.glob(f"{HOLDINGS_CSV.stem}-*.csv"), key=lambda p: p.stat().st_mtime)
    for p in old[:-BACKUPS_TO_KEEP]:
        try:
            p.unlink()
        except OSError:
            pass
    return str(dest)


def resolve_holdings_path(cli_path: str | None) -> Path:
    """--holdings beats DIVIDEND_HOLDINGS beats holdings_path.txt beats ./holdings.csv."""
    raw, source = cli_path, "--holdings"
    if not raw and os.environ.get("DIVIDEND_HOLDINGS"):
        raw, source = os.environ["DIVIDEND_HOLDINGS"], "DIVIDEND_HOLDINGS"
    if not raw and HOLDINGS_PATH_FILE.exists():
        for line in HOLDINGS_PATH_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().strip('"')
            if line and not line.startswith("#"):
                raw, source = line, HOLDINGS_PATH_FILE.name
                break
    if not raw:
        return HERE / "holdings.csv"
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = HERE / p
    if p.is_dir() or not p.suffix:
        p = p / "holdings.csv"
    if not p.parent.exists():
        print(f"Holdings folder from {source} does not exist: {p.parent}")
        print("Check the path (is the cloud folder synced on this computer?). Using the local holdings.csv instead.")
        return HERE / "holdings.csv"
    if not p.exists() and (HERE / "holdings.csv").exists():
        # First run against a new synced location: start it from the local file.
        p.write_bytes((HERE / "holdings.csv").read_bytes())
        print(f"Copied the local holdings.csv to {p}")
    return p


# --------------------------------------------------------------------------
# Raw data fetch (yfinance)  ->  {info, dividends, history, fund}
# --------------------------------------------------------------------------
def fetch_raw_yf(symbol: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(symbol)
    info = t.info or {}
    hist = t.history(period="13mo", interval="1d", auto_adjust=False, actions=False)
    divs = t.dividends  # full history, pandas Series
    fund = {}
    if (info.get("quoteType") or "").upper() in ("ETF", "MUTUALFUND"):
        try:
            fd = t.funds_data
            th = fd.top_holdings
            fund["top_holdings"] = [
                {"symbol": str(idx), "name": str(row.get("Name", "")),
                 "weight": float(row.get("Holding Percent", 0) or 0)}
                for idx, row in th.iterrows()
            ] if th is not None else []
            fund["description"] = fd.description or ""
            ov = fd.fund_overview or {}
            fund["category"] = ov.get("categoryName")
            fund["family"] = ov.get("family")
            fund["legal_type"] = ov.get("legalType")
            ac = fd.asset_classes or {}
            fund["asset_classes"] = {k: float(v) for k, v in ac.items() if v}
            sw = fd.sector_weightings or {}
            fund["sectors"] = {k: float(v) for k, v in sw.items() if v}
            try:
                ops = fd.fund_operations
                col = ops.columns[0]
                er = ops.loc["Annual Report Expense Ratio", col]
                fund["expense_ratio_frac"] = float(er) if er == er else None
            except Exception:
                pass
        except Exception as e:  # fund data is best-effort
            fund["error"] = str(e)

    div_list = []
    if divs is not None and len(divs):
        for ts, amt in divs.items():
            div_list.append({"date": ts.strftime("%Y-%m-%d"), "amount": float(amt)})

    hist_list = []
    if hist is not None and len(hist):
        adj_col = "Adj Close" if "Adj Close" in hist.columns else "Close"
        for ts, row in hist.iterrows():
            hist_list.append({"date": ts.strftime("%Y-%m-%d"),
                              "close": float(row["Close"]),
                              "adj": float(row[adj_col])})
    monthly = []
    try:
        mh = t.history(period="10y", interval="1mo", auto_adjust=False, actions=False)
        if mh is not None and len(mh) and "Adj Close" in mh.columns:
            for ts, row in mh.iterrows():
                if row["Adj Close"] == row["Adj Close"]:
                    monthly.append({"date": ts.strftime("%Y-%m-%d"), "close": float(row["Close"]),
                                    "adj": float(row["Adj Close"])})
    except Exception:
        pass
    return {"info": info, "dividends": div_list, "history": hist_list, "fund": fund, "monthly": monthly}


# --------------------------------------------------------------------------
# Built-in Yahoo Finance client (standard library only, no pandas / DLLs)
# --------------------------------------------------------------------------
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

USE_YFINANCE = False
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_yahoo_lock = threading.Lock()
_yahoo = {"opener": None, "crumb": None}


def _opener():
    if _yahoo["opener"] is None:
        jar = http.cookiejar.CookieJar()
        _yahoo["opener"] = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return _yahoo["opener"]


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*",
                                               "Accept-Language": "en-US,en;q=0.9"})
    with _opener().open(req, timeout=timeout) as r:
        return r.read()


def _crumb(refresh: bool = False) -> str | None:
    with _yahoo_lock:
        if _yahoo["crumb"] and not refresh:
            return _yahoo["crumb"]
        if refresh:
            _yahoo["opener"] = None
        for seed in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
            try:
                _get(seed)  # sets the session cookie (fc.yahoo.com answers 404, that's fine)
            except Exception:
                pass
            try:
                c = _get("https://query2.finance.yahoo.com/v1/test/getcrumb").decode().strip()
                if c and "<" not in c and len(c) < 40:
                    _yahoo["crumb"] = c
                    return c
            except Exception:
                continue
        return None


def _raw(v):
    """Yahoo wraps numbers as {"raw": 1.2, "fmt": "1.20"}."""
    if isinstance(v, dict):
        return v.get("raw")
    return v


def _quote_summary(symbol: str) -> dict:
    modules = ",".join(["price", "summaryDetail", "defaultKeyStatistics", "fundProfile", "topHoldings",
                        "assetProfile", "quoteType", "calendarEvents"])
    sym = urllib.parse.quote(symbol)
    last_err = None
    for attempt in range(2):
        crumb = _crumb(refresh=attempt > 0)
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}?modules={modules}"
        if crumb:
            url += "&crumb=" + urllib.parse.quote(crumb)
        try:
            data = json.loads(_get(url))
            res = (data.get("quoteSummary") or {}).get("result") or []
            return res[0] if res else {}
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                return {}
        except Exception as e:
            last_err = e
    raise RuntimeError(f"fund details unavailable ({last_err})")


def _chart(symbol: str, rng: str, interval: str) -> dict:
    sym = urllib.parse.quote(symbol)
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range={rng}&interval={interval}&events=div&includeAdjustedClose=true")
    data = json.loads(_get(url))
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise ValueError(err.get("description") or "symbol not found")
    return res[0]


def fetch_raw_stdlib(symbol: str) -> dict:
    # Prices (2 years daily) and full dividend history
    ch = _chart(symbol, "2y", "1d")
    meta = ch.get("meta") or {}
    ts = ch.get("timestamp") or []
    ind = ch.get("indicators") or {}
    closes = ((ind.get("quote") or [{}])[0]).get("close") or []
    adjs = ((ind.get("adjclose") or [{}])[0]).get("adjclose") or closes
    hist = []
    for t, c, a in zip(ts, closes, adjs):
        if c is None:
            continue
        hist.append({"date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "close": float(c), "adj": float(a if a is not None else c)})
    def _divs(events: dict) -> list[dict]:
        return [{"date": datetime.fromtimestamp(int(d.get("date") or k), tz=timezone.utc).strftime("%Y-%m-%d"),
                 "amount": float(d["amount"])} for k, d in (events or {}).items() if d.get("amount")]

    # Recent dividends come from the daily chart (exact dates, up to date). The monthly "max" chart is
    # only used for older history: on monthly bars Yahoo can leave out the latest payments, which made
    # the most recent months look like they paid nothing.
    recent = _divs((ch.get("events") or {}).get("dividends"))
    first_daily = hist[0]["date"] if hist else "9999-12-31"

    # The last 10 years come from a daily chart: exact ex-dates and every payment. Yahoo's monthly
    # bars can leave out payments (for some stocks, whole years), which emptied the dividends-by-year
    # chart and the growth figures. Month-end adjusted closes from it give the 3y / 5y returns.
    mid, monthly = [], []
    first_10y = first_daily
    try:
        dch = _chart(symbol, "10y", "1d")
        mid = [d for d in _divs((dch.get("events") or {}).get("dividends")) if d["date"] < first_daily]
        dind = dch.get("indicators") or {}
        dcl = ((dind.get("quote") or [{}])[0]).get("close") or []
        dadj = ((dind.get("adjclose") or [{}])[0]).get("adjclose") or []
        days = []
        for i, (t, c) in enumerate(zip(dch.get("timestamp") or [], dcl)):
            a = dadj[i] if i < len(dadj) else None
            if c is not None and a is not None:
                days.append({"date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                             "close": float(c), "adj": float(a)})
        if days:
            first_10y = min(first_10y, days[0]["date"])
            # last trading day of each month, plus the latest day
            monthly = [b for k, b in enumerate(days) if k == len(days) - 1 or days[k + 1]["date"][:7] != b["date"][:7]]
    except Exception:
        pass
    # Anything older than that comes from the monthly "max" chart (best effort; only used for the
    # years-paying count and growth streak).
    try:
        older = [d for d in _divs((_chart(symbol, "max", "1mo").get("events") or {}).get("dividends"))
                 if d["date"] < first_10y]
    except Exception:
        older = []
    seen, divs = set(), []
    for d in sorted(older + mid + recent, key=lambda d: d["date"]):
        if d["date"] not in seen:
            seen.add(d["date"])
            divs.append(d)

    info = {"quoteType": (meta.get("instrumentType") or "").upper(),
            "longName": meta.get("longName") or meta.get("shortName"),
            "regularMarketPrice": meta.get("regularMarketPrice"),
            "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
            "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh")}
    fund: dict = {}
    try:
        qs = _quote_summary(symbol)
    except Exception as e:
        qs = {}
        fund["error"] = str(e)
    if qs:
        pr, sd = qs.get("price") or {}, qs.get("summaryDetail") or {}
        ks, fp = qs.get("defaultKeyStatistics") or {}, qs.get("fundProfile") or {}
        th, ap = qs.get("topHoldings") or {}, qs.get("assetProfile") or {}
        qt, ce = qs.get("quoteType") or {}, qs.get("calendarEvents") or {}

        def put(key, val):
            val = _raw(val)
            if val not in (None, "", {}):
                info[key] = val

        put("quoteType", (qt.get("quoteType") or "").upper())
        put("longName", pr.get("longName") or qt.get("longName") or pr.get("shortName"))
        put("regularMarketPrice", pr.get("regularMarketPrice"))
        put("regularMarketPreviousClose", pr.get("regularMarketPreviousClose") or sd.get("previousClose"))
        put("fiftyTwoWeekLow", sd.get("fiftyTwoWeekLow"))
        put("fiftyTwoWeekHigh", sd.get("fiftyTwoWeekHigh"))
        put("yield", sd.get("yield"))
        put("exDividendDate", sd.get("exDividendDate") or ce.get("exDividendDate"))
        put("dividendDate", ce.get("dividendDate"))
        put("category", ks.get("category") or fp.get("categoryName"))
        put("fundFamily", ks.get("fundFamily") or fp.get("family"))
        fee = fp.get("feesExpensesInvestment") or {}
        put("annualReportExpenseRatio", fee.get("annualReportExpenseRatio") or ks.get("annualReportExpenseRatio"))
        put("totalAssets", sd.get("totalAssets") or ks.get("totalAssets"))
        put("marketCap", pr.get("marketCap") or sd.get("marketCap"))
        put("beta3Year", ks.get("beta3Year"))
        put("beta", sd.get("beta"))
        put("threeYearAverageReturn", ks.get("threeYearAverageReturn"))
        put("fiveYearAverageReturn", ks.get("fiveYearAverageReturn"))
        put("sector", ap.get("sector"))
        put("industry", ap.get("industry"))
        put("longBusinessSummary", ap.get("longBusinessSummary"))
        fund["top_holdings"] = [{"symbol": h.get("symbol") or "", "name": h.get("holdingName") or "",
                                 "weight": float(_raw(h.get("holdingPercent")) or 0)}
                                for h in th.get("holdings") or []]
        sectors = {}
        for item in th.get("sectorWeightings") or []:
            for k, v in item.items():
                if _raw(v):
                    sectors[k] = float(_raw(v))
        fund["sectors"] = sectors
        fund["asset_classes"] = {k: float(_raw(th[k])) for k in
                                 ("stockPosition", "bondPosition", "cashPosition", "preferredPosition",
                                  "convertiblePosition", "otherPosition") if _raw(th.get(k))}
        fund["category"] = fp.get("categoryName") or ks.get("category")
        fund["family"] = fp.get("family") or ks.get("fundFamily")
        fund["legal_type"] = fp.get("legalType")
        fund["description"] = ap.get("longBusinessSummary") or ""
    return {"info": info, "dividends": divs, "history": hist, "fund": fund, "monthly": monthly}


def fetch_raw(symbol: str) -> dict:
    return fetch_raw_yf(symbol) if USE_YFINANCE else fetch_raw_stdlib(symbol)



# --------------------------------------------------------------------------
# Strategy and underlying-exposure detection (from fund name + description)
# --------------------------------------------------------------------------
import re

_EXPOSURES = [
    (r"weeklypay|yieldmax|single[- ]stock", "Single-stock ETFs"),
    (r"magnificent (seven|7)|\bmag ?7\b|\bmags\b", "Magnificent 7"),
    (r"\bfang\b", "Big tech (FANG)"),
    (r"\bai\b|artificial intelligence", "AI / tech"),
    (r"nasdaq[- ]?100|\bndx\b|\bqqq\b", "Nasdaq-100"),
    (r"s&p 500|s&p500|\bspx\b|\bxsp\b|\bspy\b", "S&P 500"),
    (r"russell 2000|\brut\b|small[- ]cap", "Russell 2000 / small cap"),
    (r"dow jones industrial|\bdjia\b|\bdia\b", "Dow 30"),
    (r"dow jones u\.?s\.? dividend 100", "US Dividend 100 (quality dividend)"),
    (r"s&p mid ?cap 400|mid[- ]cap", "US mid cap"),
    (r"bitcoin|\bbtc\b|crypto", "Bitcoin / crypto"),
    (r"ethereum|\beth\b", "Ethereum / crypto"),
    (r"\bgold\b", "Gold"),
    (r"semiconductor", "Semiconductors"),
    (r"\butilit", "Utilities"),
    (r"\bmlps?\b|energy infrastructure|midstream", "Energy infrastructure (MLPs)"),
    (r"emerging market", "Emerging markets"),
    (r"international|foreign|developed markets|ex-u\.?s\.?|\beafe\b", "International developed"),
    (r"real estate|\breit", "Real estate"),
    (r"treasur|\bt-bill|government bond", "US Treasuries"),
    (r"municipal|\bmuni", "Municipal bonds"),
    (r"high[- ]yield|junk", "High-yield bonds"),
    (r"preferred", "Preferred stock"),
    (r"\bbond|fixed income|aggregate", "Investment-grade bonds"),
    (r"high dividend|dividend yield|dividend", "US dividend stocks"),
    (r"\bnasdaq\b", "Nasdaq-100"),
    (r"\bu\.?s\.? equity|\bu\.?s\.? large[- ]cap|large[- ]cap", "US large cap"),
]
# Option-income and single-stock funds hold T-bills as collateral, so their descriptions
# mention Treasuries without that being what they track.
_COLLATERAL_LABELS = {"US Treasuries", "Investment-grade bonds", "High-yield bonds"}
_OPTIONISH = r"option|covered[- ]call|premium income|buy[- ]?write|autocall|weeklypay|yieldmax|high income"

_CATEGORY_EXPOSURE = {
    "large growth": "US large-cap growth", "large value": "US large-cap value", "large blend": "US large-cap blend",
    "technology": "Technology", "derivative income": "US large cap (options income)",
}


def classify_exposure(info: dict, fund: dict, name: str, desc: str, category: str) -> str:
    def match(text, skip=()):
        for pat, label in _EXPOSURES:
            if label not in skip and re.search(pat, text):
                return label
        return None

    lname = name.lower()
    if (info.get("quoteType") or "").upper() == "EQUITY":
        # A company's business description mentions "international", "bonds" and so on
        # without that being an exposure, so for stocks read only the name, then the sector.
        hit = match(lname, skip={"US dividend stocks", "US large cap"})
        if hit:
            return hit
        industry = (info.get("industry") or "").lower()
        if "asset management" in industry or "closed-end" in industry:
            return "Closed-end fund / BDC"
        sector = info.get("sector") or ""
        return sector[:1] + sector[1:].lower() if sector else "Single stock"   # "Real Estate" -> "Real estate"
    # Funds: the name is the most reliable clue, then the description, then the category.
    hit = match(lname)
    if hit:
        return hit
    text = f"{name} {desc}".lower()
    skip = _COLLATERAL_LABELS if re.search(_OPTIONISH, text) or "derivative" in (category or "").lower() else ()
    hit = match(desc.lower(), skip)
    if hit:
        return hit
    c = (category or "").lower()
    for k, v in _CATEGORY_EXPOSURE.items():
        if k in c:
            return v
    return category or "Unclassified"


_STRATEGY_RULES = [
    ("calls", r"covered[- ]call|buy[- ]?write|(sell|sells|selling|write|writes|writing)\b[^.;]{0,60}\bcall|call[- ]writ|call option",
     "Sells calls (covered calls)"),
    ("puts", r"(sell|sells|selling|write|writes|writing)\b[^.;]{0,60}\bputs?\b|put[- ]writ|cash[- ]secured put|\bput option",
     "Sells puts"),
    ("spreads", r"call spread|put spread|credit spread|spreads", "Option spreads"),
    ("eln", r"equity[- ]linked note|\belns?\b", "Uses equity-linked notes (ELNs)"),
    ("0dte", r"0dte|zero[- ]day|daily options|same[- ]day", "Daily / 0DTE options"),
    ("weekly", r"weekly", "Weekly options"),
    ("index_opts", r"index options|section 1256|options on the (nasdaq|s&p|index)", "Index options (Sec. 1256 tax treatment)"),
    ("single_opts", r"single[- ]stock|individual stock|options on (individual|its) (stocks|holdings)", "Options on individual stocks"),
    ("partial", r"portion of|partial|up to \d+%|between \d+% and \d+%", "Partial overwrite (keeps some upside)"),
    ("synthetic", r"synthetic", "Synthetic exposure"),
    ("leverage", r"leverag|\b[23]x\b", "Uses leverage"),
    ("active", r"actively managed|active(ly)? manag|portfolio manager", "Actively managed"),
]


def classify_strategy(info: dict, name: str, desc: str, category: str, dist_type: str,
                      money_market: bool = False) -> tuple[str, list[str]]:
    text = f"{name}. {desc}".lower()
    tags = []
    for key, pat, label in _STRATEGY_RULES:
        if re.search(pat, text):
            tags.append(label)
    if "Option spreads" in tags and "Sells calls (covered calls)" not in tags and "call spread" in text:
        tags.insert(0, "Sells calls (covered calls)")
    sells_calls = "Sells calls (covered calls)" in tags
    sells_puts = "Sells puts" in tags
    cat = (category or "").lower()
    qt = (info.get("quoteType") or "").upper()
    if sells_calls and sells_puts:
        primary = "Sells calls and puts"
    elif sells_calls:
        primary = "Sells calls (covered call)"
    elif sells_puts:
        primary = "Put writing"
    elif "weeklypay" in text:
        primary = "Leveraged weekly payout"
        if "Uses leverage" not in tags:
            tags.append("Uses leverage")
    elif ("Uses equity-linked notes (ELNs)" in tags or "derivative income" in cat or "option" in text
          or re.search(r"premium income|yieldmax|autocall|buy[- ]?write", text)
          or ("high income" in text and "bond" not in cat)):
        primary = "Options income"
    elif qt == "EQUITY":
        primary = "Individual company dividend"
    elif money_market:
        primary = "Money market (cash)"
    elif any(k in cat for k in ("bond", "treasury", "muni", "fixed", "credit", "loan")):
        primary = "Bond interest income"
    elif "real estate" in cat or "reit" in text:
        primary = "REIT income"
    elif "dividend" in text or "value" in cat:
        primary = "Dividend stock selection"
    else:
        primary = "Passive index"
    if "Uses equity-linked notes (ELNs)" in tags and "1256" not in text:
        tags = [t for t in tags if not t.startswith("Index options")]
    tags = [t for t in tags if t not in ("Sells calls (covered calls)", "Sells puts")]
    return primary, tags

# --------------------------------------------------------------------------
# Turn raw data into one dashboard record
# --------------------------------------------------------------------------
FREQ_NAMES = {12: "Monthly", 4: "Quarterly", 2: "Semi-annual", 1: "Annual", 52: "Weekly"}
_FREQ_WORDS = {"weekly": 52, "monthly": 12, "quarterly": 4, "semi-annual": 2, "semiannual": 2,
               "semi annual": 2, "annual": 1, "yearly": 1}


def parse_frequency(v: str) -> tuple[str, int] | None:
    """Frequency typed in holdings.csv: a word (Quarterly) or payments per year (4)."""
    v = (v or "").strip().lower()
    if not v:
        return None
    n = _FREQ_WORDS.get(v)
    if n is None:
        try:
            n = int(float(v))
        except ValueError:
            return None
    return (FREQ_NAMES.get(n, f"{n}x / year"), n) if n > 0 else None


def _d(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def infer_frequency(divs: list[dict], today: datetime) -> tuple[str, int]:
    """Infer payout frequency from the median gap between recent payments."""
    recent = [d for d in divs if _d(d["date"]) >= today - timedelta(days=760)]
    if len(recent) < 2:
        if not recent:
            return ("None", 0)
        # only one payment in ~2 years of data: judge the cadence from the longer history instead
        recent = divs[-8:]
        if len(recent) < 2:
            return ("Annual", 1)
    dates = [_d(d["date"]) for d in recent]
    gaps = sorted((b - a).days for a, b in zip(dates, dates[1:]))
    g = gaps[len(gaps) // 2]
    if g <= 10:
        return "Weekly", 52
    if g <= 45:
        return "Monthly", 12
    if g <= 120:
        return "Quarterly", 4
    if g <= 240:
        return "Semi-annual", 2
    return "Annual", 1


_MONEY_MARKET_RE = re.compile(r"money[- ]market|cash reserves|cash management|liquidity fund|\bmmf\b", re.I)


def is_money_market(info: dict, fund: dict, category: str, name: str, symbol: str = "") -> bool:
    """Money market funds (FDRXX, SPAXX, VMFXX, SWVXX...): their payouts are interest, not dividends.

    Yahoo often has thin data for these (no category, sometimes no name at all, so the name falls back to
    the ticker), so beyond Yahoo's MONEYMARKET quote type also check the name and category, and the US
    convention that a 5-letter mutual fund ticker ending in XX is a money market fund."""
    qt = (info.get("quoteType") or "").upper()
    if qt == "MONEYMARKET":
        return True
    if qt in ("EQUITY", "ETF"):
        return False
    text = f"{category} {fund.get('category') or ''} {fund.get('legal_type') or ''} {name}"
    if _MONEY_MARKET_RE.search(text):
        return True
    return bool(re.fullmatch(r"[A-Z]{3}XX", (symbol or "").upper()))


def guess_distribution_type(info: dict, fund: dict, category: str, name: str, symbol: str = "") -> str:
    c = f"{category} {name}".lower()
    qt = (info.get("quoteType") or "").upper()
    if is_money_market(info, fund, category, name, symbol):
        if "muni" in c or "tax-exempt" in c or "tax exempt" in c:
            return "Interest - generally federally tax-exempt"
        return "Interest (taxed as ordinary income)"
    if qt == "EQUITY":
        sector = (info.get("sector") or "").lower()
        industry = (info.get("industry") or "").lower()
        if "reit" in industry or "real estate" in sector:
            return "REIT dividends (mostly non-qualified, some ROC)"
        if "partnership" in name.lower() or " lp" in name.lower() or "midstream" in industry:
            return "Partnership distributions (largely return of capital, K-1)"
        if "bdc" in name.lower() or "asset management" in industry and "capital" in name.lower():
            return "Ordinary income (BDC / RIC dividends)"
        return "Qualified dividends (if holding period met)"
    if any(k in c for k in ("covered call", "option", "derivative income", "premium income", "buywrite", "yieldmax")):
        return "Option premium income (often largely return of capital)"
    if any(k in c for k in ("bond", "treasury", "fixed income", "muni", "credit", "loan", "t-bill", "bill etf",
                            "aggregate", "ultrashort", "short-term", "government", "corporate", "high yield")) \
            and "dividend" not in c and "equity" not in c:
        if "muni" in c:
            return "Interest - generally federally tax-exempt"
        return "Interest (taxed as ordinary income)"
    if "real estate" in c or "reit" in c:
        return "REIT dividends (mostly non-qualified, some ROC)"
    if any(k in c for k in ("mlp", "midstream", "energy infrastructure")):
        return "MLP distributions (largely return of capital)"
    if "preferred" in c:
        return "Preferred dividends (mix of qualified and ordinary)"
    if any(k in c for k in ("international", "foreign", "emerging", "world", "global", "ex-us")):
        return "Mostly qualified dividends (foreign tax may be withheld)"
    return "Mostly qualified dividends"


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def project_monthly(divs: list[dict], freq_n: int, annual_income: float | None) -> list[float]:
    """Projected income per calendar month (index 0 = January) from the payout cadence.

    Monthly and weekly payers are spread evenly over all 12 months. Quarterly, semi-annual and annual
    payers repeat the month of their latest payment every 3, 6 or 12 months. This does not depend on which months
    happen to fall inside a trailing 365-day window, so one late, early or missing payment no longer
    empties a month.
    """
    out = [0.0] * 12
    if not annual_income or not freq_n:
        return out
    if freq_n >= 12:
        return [annual_income / 12] * 12
    if not divs:
        return out
    # Anchor on the latest payment's month and step back by the cadence (every 3 months for quarterly, ...)
    step = 12 / freq_n
    last = int(divs[-1]["date"][5:7]) - 1
    per = annual_income / freq_n
    for k in range(freq_n):
        out[int(round(last - k * step)) % 12] += per
    return out


def _ann_return(monthly, years: int, today: datetime, yahoo_fallback=None):
    """Annualized total return (dividends reinvested) over the last `years` years, from monthly
    dividend-adjusted closes. Yahoo only publishes 3y/5y averages for some funds (none for stocks),
    so we work it out ourselves and only fall back to Yahoo's figure when there is no history."""
    bars = [b for b in (monthly or []) if b.get("adj")]
    if len(bars) >= 2:
        cut = (today - timedelta(days=round(365.25 * years))).strftime("%Y-%m-%d")
        base = [b for b in bars if b["date"] <= cut]
        if base:  # not enough history (a younger fund) -> None rather than a misleading figure
            b0, b1 = base[-1], bars[-1]
            yrs = (_d(b1["date"]) - _d(b0["date"])).days / 365.25
            if yrs > 0 and b0["adj"] > 0:
                return ((b1["adj"] / b0["adj"]) ** (1 / yrs) - 1) * 100
            return None
        return None
    return yahoo_fallback * 100 if yahoo_fallback is not None else None


def build_record(raw: dict, position: dict, today: datetime | None = None) -> dict:
    today = today or datetime.now()
    info, fund = raw.get("info") or {}, raw.get("fund") or {}
    divs = sorted(raw.get("dividends") or [], key=lambda d: d["date"])
    hist = raw.get("history") or []

    sym = position["symbol"]
    qt = (info.get("quoteType") or "").upper()
    name = info.get("longName") or info.get("shortName") or sym
    price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("navPrice")
    if price is None and hist:
        price = hist[-1]["close"]
    prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
    if prev is None and len(hist) > 1:
        prev = hist[-2]["close"]
    lo, hi = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
    yr = [h for h in hist if _d(h["date"]) >= today - timedelta(days=365)]
    if (lo is None or hi is None) and yr:
        lo, hi = min(h["close"] for h in yr), max(h["close"] for h in yr)

    # 12-month returns: price and total (dividends reinvested via adjusted close)
    price_ret = total_ret = None
    if yr:
        base = yr[0]
        last_close = price or yr[-1]["close"]
        price_ret = _pct(last_close, base["close"])
        total_ret = _pct(yr[-1]["adj"], base["adj"])

    # Dividends
    freq_name, freq_n = infer_frequency(divs, today)
    freq_src = "estimated"
    override = parse_frequency(position.get("frequency") or "")
    if override:
        freq_name, freq_n = override
        freq_src = "you"
    ttm = [d for d in divs if _d(d["date"]) > today - timedelta(days=365)]
    ttm_amt = sum(d["amount"] for d in ttm)
    last_div = divs[-1] if divs else None
    ttm_yield = ttm_amt / price * 100 if price and ttm_amt else None
    fwd_amt = last_div["amount"] * freq_n if last_div and freq_n else None
    fwd_yield = fwd_amt / price * 100 if price and fwd_amt else None
    sec_yield = info.get("yield")  # Yahoo's fund yield (fraction)
    sec_yield = sec_yield * 100 if isinstance(sec_yield, (int, float)) else None

    by_year: dict[int, float] = {}
    for d in divs:
        y = int(d["date"][:4])
        by_year[y] = by_year.get(y, 0) + d["amount"]
    last_full = today.year - 1
    g1 = g5 = None
    if by_year.get(last_full) and by_year.get(last_full - 1):
        g1 = _pct(by_year[last_full], by_year[last_full - 1])
    if by_year.get(last_full) and by_year.get(last_full - 5):
        g5 = ((by_year[last_full] / by_year[last_full - 5]) ** (1 / 5) - 1) * 100
    years_paying = len([y for y in by_year if y <= last_full])
    # consecutive years of growth (calendar)
    streak = 0
    y = last_full
    while by_year.get(y) and by_year.get(y - 1) and by_year[y] >= by_year[y - 1] * 0.999:
        streak += 1
        y -= 1

    # Payout consistency: coefficient of variation of the last 12 payments
    recent_amts = [d["amount"] for d in divs[-max(freq_n, 4):]] if divs else []
    cv = None
    if len(recent_amts) >= 3:
        m = sum(recent_amts) / len(recent_amts)
        sd = math.sqrt(sum((a - m) ** 2 for a in recent_amts) / len(recent_amts))
        cv = sd / m * 100 if m else None

    ex_date = info.get("exDividendDate")
    ex_date = datetime.fromtimestamp(ex_date).strftime("%Y-%m-%d") if isinstance(ex_date, (int, float)) else None
    pay_date = info.get("dividendDate")
    pay_date = datetime.fromtimestamp(pay_date).strftime("%Y-%m-%d") if isinstance(pay_date, (int, float)) else None
    # Days from ex-date to pay date. Yahoo only announces one pay date (the next or latest one), and
    # often none for ETFs, so the calendar estimates each cycle's pay date as ex-date + this gap.
    # Best source: Yahoo's own ex/pay pair for the same cycle. Otherwise a typical gap for this kind
    # of security (funds pay a few days after the ex-date, companies two to four weeks after).
    pay_lag, pay_lag_src = None, "typical"
    if ex_date and pay_date:
        gap = (_d(pay_date) - _d(ex_date)).days
        if 0 <= gap <= 75:
            pay_lag, pay_lag_src = gap, "yahoo"
    if pay_lag is None:
        if qt == "EQUITY":
            pay_lag = 15 if freq_n >= 12 else 21
        else:
            pay_lag = 1 if freq_n >= 52 else 4
    next_ex_est = None
    if last_div and freq_n:
        nxt = _d(last_div["date"]) + timedelta(days=round(365 / freq_n))
        while nxt < today - timedelta(days=3):
            nxt += timedelta(days=round(365 / freq_n))
        next_ex_est = nxt.strftime("%Y-%m-%d")
    if ex_date and ex_date >= today.strftime("%Y-%m-%d"):
        next_ex_est = ex_date

    # Months in which it paid in the last 12 months (shown for reference only)
    pay_months = sorted({int(d["date"][5:7]) for d in ttm})

    # Expense ratio (percent)
    er = info.get("netExpenseRatio")
    if er is None and info.get("annualReportExpenseRatio") is not None:
        er = info["annualReportExpenseRatio"] * 100
    if er is None and fund.get("expense_ratio_frac") is not None:
        er = fund["expense_ratio_frac"] * 100

    category = fund.get("category") or info.get("category") or ""
    if qt == "EQUITY":
        focus = " / ".join(x for x in (info.get("sector"), info.get("industry")) if x)
        desc = info.get("longBusinessSummary") or ""
    else:
        focus = category
        desc = fund.get("description") or info.get("longBusinessSummary") or ""

    money_market = is_money_market(info, fund, category, name, sym)
    yield_src = "dividends"
    if money_market and not ttm_yield and not fwd_yield and sec_yield:
        # Yahoo keeps no distribution history for money market funds (they accrue interest daily and pay
        # monthly), so there is nothing to compute TTM / forward yield from. Use Yahoo's fund yield (its
        # current 7-day yield) for both instead, at the usual monthly cadence.
        ttm_yield = fwd_yield = sec_yield
        yield_src = "yahoo"
        if not freq_n:
            freq_name, freq_n = "Monthly", 12
        if price:
            fwd_amt = price * sec_yield / 100
    dist_type = position.get("distribution_type") or guess_distribution_type(info, fund, category, name, sym)
    dist_src = "you" if position.get("distribution_type") else "estimated"
    strat_primary, strat_tags = classify_strategy(info, name, desc, category, dist_type, money_market)
    exposure = position.get("exposure") or ("Cash / money market" if money_market
                                            else classify_exposure(info, fund, name, desc, category))

    shares, cost = position.get("shares"), position.get("cost_basis")
    mv = shares * price if shares and price else None
    annual_income = shares * (fwd_amt or ttm_amt) if shares and (fwd_amt or ttm_amt) else None
    yoc = (fwd_amt or ttm_amt) / cost * 100 if cost and (fwd_amt or ttm_amt) else None
    gain = (price - cost) * shares if shares and cost and price else None
    total_cost = cost * shares if shares and cost else None
    proj_monthly = project_monthly(divs, freq_n, annual_income)
    gain_pct = _pct(price, cost) if cost else None

    # Yield trap / health flags
    flags = []
    if fwd_yield and fwd_yield > 9:
        flags.append("Very high yield - check sustainability and NAV erosion")
    if g1 is not None and g1 < -10:
        flags.append(f"Dividend cut {g1:.0f}% last year")
    if price_ret is not None and price_ret < -15 and (fwd_yield or 0) > 6:
        flags.append("Price down sharply while yield is high (possible NAV decay)")
    if er is not None and er > 0.75:
        flags.append("Expense ratio above 0.75%")
    if cv is not None and cv > 25 and freq_n >= 4:
        flags.append("Payout amounts vary a lot")

    spark = [round(h["close"], 4) for h in yr[::5]]
    if yr and (not spark or spark[-1] != round(yr[-1]["close"], 4)):
        spark.append(round(yr[-1]["close"], 4))

    return {
        "symbol": sym,
        "name": name,
        "type": qt or "?",
        "price": price,
        "change_pct": _pct(price, prev),
        "low52": lo, "high52": hi,
        "range_pos": ((price - lo) / (hi - lo) * 100) if price and lo is not None and hi and hi > lo else None,
        "focus": position.get("core_focus") or focus,
        "focus_source": "you" if position.get("core_focus") else "estimated",
        "description": desc,
        "family": fund.get("family") or info.get("fundFamily"),
        "frequency": freq_name,
        "freq_n": freq_n,
        "frequency_source": freq_src,
        "expense_ratio": er,
        "top_holdings": (fund.get("top_holdings") or [])[:10],
        "sectors": fund.get("sectors") or {},
        "asset_classes": fund.get("asset_classes") or {},
        "ttm_yield": ttm_yield,
        "fwd_yield": fwd_yield,
        "sec_yield": sec_yield,
        "yield_source": yield_src,
        "ttm_dividends": ttm_amt or None,
        "last_div": last_div,
        "ex_date": ex_date,
        "pay_date": pay_date,
        "pay_lag": pay_lag,
        "pay_lag_source": pay_lag_src,
        "next_ex_est": next_ex_est,
        "pay_months": pay_months,
        "price_return_12m": price_ret,
        "total_return_12m": total_ret,
        "return_3y": _ann_return(raw.get("monthly"), 3, today, info.get("threeYearAverageReturn")),
        "return_5y": _ann_return(raw.get("monthly"), 5, today, info.get("fiveYearAverageReturn")),
        "beta": info.get("beta3Year") or info.get("beta"),
        "aum": info.get("totalAssets") or info.get("marketCap"),
        "dist_type": dist_type,
        "dist_type_source": dist_src,
        "div_growth_1y": g1,
        "div_growth_5y": g5,
        "growth_streak": streak,
        "years_paying": years_paying,
        "payout_cv": cv,
        "div_by_year": {str(k): round(v, 4) for k, v in sorted(by_year.items()) if k >= today.year - 10},
        "div_recent": divs[-24:],
        "spark": spark,
        "flags": flags,
        "strategy": position.get("strategy") or strat_primary,
        "strategy_source": "you" if position.get("strategy") else "estimated",
        "strategy_tags": strat_tags,
        "exposure": exposure,
        "exposure_source": "you" if position.get("exposure") else "estimated",
        "shares": shares,
        "cost_basis": cost,
        "market_value": mv,
        "annual_income": annual_income,
        "yield_on_cost": yoc,
        "total_cost": total_cost,
        "gain": gain,
        "gain_pct": gain_pct,
        "proj_monthly": proj_monthly,
        # Share of a year's income that lands in each calendar month (sums to 1), for the Planning tab
        "pay_shape": project_monthly(divs, freq_n, 1.0),
        "notes": position.get("notes") or "",
        "flagged": bool(position.get("flagged")),
    }


# --------------------------------------------------------------------------
# Demo data (synthetic, clearly labelled in the UI)
# --------------------------------------------------------------------------
DEMO_SPECS = {
    "SCHD": ("Demo US Dividend Equity ETF", "Large Value", 27.5, 0.06, 4, 0.26, 0.11, "ETF", "Mostly qualified dividends"),
    "JEPI": ("Demo Equity Premium Income ETF", "Derivative Income", 57.0, 0.35, 12, 0.42, 0.0, "ETF", None),
    "VYM": ("Demo High Dividend Yield ETF", "Large Value", 135.0, 0.06, 4, 0.87, 0.05, "ETF", None),
    "O": ("Demo Realty Income Corp", "", 57.0, None, 12, 0.268, 0.03, "EQUITY", None),
    "BND": ("Demo Total Bond Market ETF", "Intermediate Core Bond", 73.5, 0.03, 12, 0.24, 0.02, "ETF", None),
    "JEPQ": ("Demo Nasdaq Equity Premium Income ETF", "Derivative Income", 56.0, 0.35, 12, 0.48, 0.0, "ETF", None),
    "QQQI": ("Demo Nasdaq 100 Income ETF", "Derivative Income", 52.0, 0.68, 12, 0.62, 0.0, "ETF", None),
    "GPIQ": ("Demo Nasdaq-100 Premium Income ETF", "Derivative Income", 50.0, 0.29, 12, 0.42, 0.0, "ETF", None),
    "SPYI": ("Demo S&P 500 High Income ETF", "Derivative Income", 51.0, 0.68, 12, 0.50, 0.0, "ETF", None),
    "DIVO": ("Demo Enhanced Dividend Income ETF", "Large Value", 42.0, 0.56, 12, 0.18, 0.03, "ETF", None),
    "SGOV": ("Demo 0-3 Month Treasury Bond ETF", "Ultrashort Bond", 100.5, 0.09, 12, 0.36, 0.0, "ETF", None),
    # The public demo's sample portfolio (demo/demo-holdings.csv), for DEMO_DATA=synthetic
    # and as a stand-in when live data can't be reached.
    "DGRO": ("Demo Dividend Growth ETF", "Large Value", 64.0, 0.08, 4, 0.38, 0.08, "ETF", None),
    "VIG": ("Demo Dividend Appreciation ETF", "Large Blend", 205.0, 0.05, 4, 0.85, 0.08, "ETF", None),
    "NOBL": ("Demo Dividend Aristocrats ETF", "Large Value", 102.0, 0.35, 4, 0.55, 0.04, "ETF", None),
    "MAIN": ("Demo Main Street Capital", "", 60.0, None, 12, 0.25, 0.04, "EQUITY", None),
    "ARCC": ("Demo Ares Capital", "", 21.0, None, 4, 0.48, 0.02, "EQUITY", None),
    "JNJ": ("Demo Johnson & Johnson", "", 160.0, None, 4, 1.30, 0.05, "EQUITY", None),
    "ABBV": ("Demo AbbVie", "", 185.0, None, 4, 1.64, 0.06, "EQUITY", None),
    "PG": ("Demo Procter & Gamble", "", 160.0, None, 4, 1.06, 0.05, "EQUITY", None),
    "KO": ("Demo Coca-Cola", "", 68.0, None, 4, 0.51, 0.05, "EQUITY", None),
    "PEP": ("Demo PepsiCo", "", 145.0, None, 4, 1.42, 0.06, "EQUITY", None),
    "XOM": ("Demo Exxon Mobil", "", 115.0, None, 4, 1.03, 0.04, "EQUITY", None),
    "CVX": ("Demo Chevron", "", 155.0, None, 4, 1.71, 0.05, "EQUITY", None),
    "ENB": ("Demo Enbridge", "", 45.0, None, 4, 0.68, 0.03, "EQUITY", None),
    "VZ": ("Demo Verizon", "", 42.0, None, 4, 0.69, 0.02, "EQUITY", None),
    "MO": ("Demo Altria", "", 58.0, None, 4, 1.06, 0.04, "EQUITY", None),
    "TXN": ("Demo Texas Instruments", "", 185.0, None, 4, 1.42, 0.05, "EQUITY", None),
}
DEMO_SECTORS = {  # sector, industry for the synthetic stocks (default: a retail REIT, like O)
    "MAIN": ("Financial Services", "Asset Management"), "ARCC": ("Financial Services", "Asset Management"),
    "JNJ": ("Healthcare", "Drug Manufacturers - General"), "ABBV": ("Healthcare", "Drug Manufacturers - General"),
    "PG": ("Consumer Defensive", "Household & Personal Products"), "KO": ("Consumer Defensive", "Beverages - Non-Alcoholic"),
    "PEP": ("Consumer Defensive", "Beverages - Non-Alcoholic"), "MO": ("Consumer Defensive", "Tobacco"),
    "XOM": ("Energy", "Oil & Gas Integrated"), "CVX": ("Energy", "Oil & Gas Integrated"),
    "ENB": ("Energy", "Oil & Gas Midstream"), "VZ": ("Communication Services", "Telecom Services"),
    "TXN": ("Technology", "Semiconductors"),
}
DEMO_DESC = {
    "JEPI": "Seeks income by holding a defensive portfolio of S&P 500 stocks and selling out-of-the-money S&P 500 index call options through equity-linked notes (ELNs). Actively managed.",
    "JEPQ": "Holds Nasdaq-100 stocks and sells out-of-the-money call options on the Nasdaq-100 index through equity-linked notes. Actively managed.",
    "QQQI": "Holds Nasdaq-100 stocks and writes Nasdaq-100 index call options, using call spreads; index options receive Section 1256 tax treatment.",
    "GPIQ": "Holds Nasdaq-100 stocks and writes call options on a portion of the portfolio, between 25% and 75%, to keep some upside.",
}
NASDAQ_DEMO = [("NVDA", "NVIDIA"), ("MSFT", "Microsoft"), ("AAPL", "Apple"), ("AMZN", "Amazon"), ("AVGO", "Broadcom"),
               ("META", "Meta Platforms"), ("GOOGL", "Alphabet A"), ("TSLA", "Tesla"), ("COST", "Costco"), ("NFLX", "Netflix")]


def demo_raw(symbol: str) -> dict:
    import random
    rnd = random.Random(symbol)
    spec = DEMO_SPECS.get(symbol) or (f"Demo {symbol} Fund", "Large Blend", rnd.uniform(20, 200), 0.2, 4,
                                      rnd.uniform(0.1, 1.0), 0.05, "ETF", None)
    name, cat, p0, er, freq, div0, growth, qt, _ = spec
    today = datetime.now()
    hist, price, f = [], p0, 1.0
    d = today - timedelta(days=395)
    while d <= today:
        if d.weekday() < 5:
            price *= 1 + rnd.gauss(0.0003, 0.009)
            f *= 1 + div0 * freq / p0 / 252
            hist.append({"date": d.strftime("%Y-%m-%d"), "close": price, "adj": price * f})
        d += timedelta(days=1)
    divs = []
    step = 365 // freq
    d = today - timedelta(days=365 * 7)
    amt = div0 / ((1 + growth) ** 6)
    yr = d.year
    while d <= today - timedelta(days=5):
        if d.year != yr:
            amt *= 1 + growth
            yr = d.year
        a = amt * (1 + rnd.gauss(0, 0.12 if "Derivative" in cat else 0.02))
        divs.append({"date": d.strftime("%Y-%m-%d"), "amount": round(a, 4)})
        d += timedelta(days=step)
    info = {"quoteType": qt, "longName": name, "regularMarketPrice": price,
            "regularMarketPreviousClose": hist[-2]["close"],
            "fiftyTwoWeekLow": min(h["close"] for h in hist[-252:]),
            "fiftyTwoWeekHigh": max(h["close"] for h in hist[-252:]),
            "category": cat, "netExpenseRatio": er, "fundFamily": "Demo Funds",
            "totalAssets": rnd.uniform(5e9, 7e10), "beta3Year": round(rnd.uniform(0.6, 1.0), 2),
            "threeYearAverageReturn": rnd.uniform(0.03, 0.1), "fiveYearAverageReturn": rnd.uniform(0.04, 0.12),
            "exDividendDate": int((today + timedelta(days=rnd.randint(2, 25))).timestamp())}
    if rnd.random() < 0.5:  # like Yahoo: only some symbols come with an announced pay date
        lag = rnd.randint(18, 28) if qt == "EQUITY" else rnd.randint(2, 6)
        info["dividendDate"] = info["exDividendDate"] + lag * 86400
    fund = {}
    if qt == "EQUITY":
        sector, industry = DEMO_SECTORS.get(symbol, ("Real Estate", "REIT - Retail"))
        info.update({"sector": sector, "industry": industry,
                     "longBusinessSummary": "Sample demo company used to preview the dashboard."})
    else:
        names = ["Alpha Corp", "Beta Industries", "Gamma Holdings", "Delta Energy", "Epsilon Foods",
                 "Zeta Pharma", "Eta Telecom", "Theta Bank", "Iota Retail", "Kappa Tech"]
        w = sorted((rnd.uniform(0.02, 0.05) for _ in names), reverse=True)
        th = [{"symbol": n.split()[0].upper()[:4], "name": n, "weight": x} for n, x in zip(names, w)]
        if "Nasdaq" in name:
            nw = sorted((rnd.uniform(0.025, 0.09) for _ in NASDAQ_DEMO), reverse=True)
            th = [{"symbol": t, "name": n, "weight": x} for (t, n), x in zip(NASDAQ_DEMO, nw)]
        fund = {"top_holdings": th,
                "description": DEMO_DESC.get(symbol, f"Sample demo fund in the {cat} category, used to preview the dashboard."),
                "category": cat, "family": "Demo Funds",
                "sectors": {"financial_services": 0.2, "healthcare": 0.15, "technology": 0.14, "consumer_defensive": 0.12,
                            "industrials": 0.12, "energy": 0.1, "other": 0.17},
                "asset_classes": {"stockPosition": 0.99, "cashPosition": 0.01}}
    return {"info": info, "dividends": divs, "history": hist, "fund": fund}


# --------------------------------------------------------------------------
# Cache + assembly
# --------------------------------------------------------------------------
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(c: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(c))
    except Exception:
        pass


def get_raw(symbol: str, cache: dict, force: bool, budget: bool = False) -> tuple[dict | None, str | None, float | None]:
    """budget=True (public demo, visitor lookups): an uncached symbol counts against the lookup budget."""
    if DEMO:
        return demo_raw(symbol), None, time.time()
    ent = cache.get(symbol)
    # Entries saved by an older version have no "monthly" data (3y / 5y returns): fetch them again.
    fresh = ent and "monthly" in ent["raw"] and time.time() - ent["ts"] < CACHE_TTL_SECONDS
    if fresh and not force:
        return ent["raw"], None, ent["ts"]
    if PUBLIC_DEMO and not force and time.time() - _failed.get(symbol, 0) < PUBLIC_RETRY_AFTER:
        # Yahoo failed for this symbol a moment ago: answer right away instead of waiting on it again.
        if ent:
            return ent["raw"], "Using cached data", ent["ts"]
        if not budget:
            return demo_raw(symbol), "Live data unavailable right now, showing sample numbers", time.time()
        return None, "Live data unavailable right now. Try again later.", None
    if PUBLIC_DEMO and budget and not ent and not _public_lookup_ok():
        return None, BUSY_MSG, None
    try:
        if PUBLIC_DEMO:
            with _yahoo_slots:
                raw = fetch_raw(symbol)
        else:
            raw = fetch_raw(symbol)
        if not raw["history"] and not raw["info"].get("regularMarketPrice"):
            raise ValueError("no data returned - check the symbol")
        if PUBLIC_DEMO:
            _fill_from_snapshot(symbol, raw)
        cache[symbol] = {"ts": time.time(), "raw": raw}
        _failed.pop(symbol, None)
        return raw, None, cache[symbol]["ts"]
    except Exception as e:
        if PUBLIC_DEMO:
            _failed[symbol] = time.time()
        if ent:  # fall back to stale data rather than nothing
            return ent["raw"], f"Using cached data: {e}", ent["ts"]
        if PUBLIC_DEMO and not budget:
            # Public demo portfolio: show sample numbers rather than an empty page if Yahoo is unreachable.
            return demo_raw(symbol), "Live data unavailable right now, showing sample numbers", time.time()
        return None, str(e), None


def _apply_pay_lag(rec: dict, cache: dict) -> None:
    """Remember each symbol's ex-to-pay gap whenever Yahoo shows both dates, and reuse it on later
    loads when Yahoo has dropped the pay date (it often does between cycles)."""
    lags = cache.setdefault("_pay_lags", {})
    sym = rec["symbol"]
    if rec.get("pay_lag_source") == "yahoo":
        lags[sym] = rec["pay_lag"]
    elif isinstance(lags.get(sym), int):
        rec["pay_lag"], rec["pay_lag_source"] = lags[sym], "learned"


def build_dashboard(force: bool = False) -> dict:
    with _lock:
        holdings = read_holdings()
        cache = _load_cache()
        out, errors = [], []

        def work(pos):
            raw, err, ts = get_raw(pos["symbol"], cache, force)
            if raw is None:
                return None, {"symbol": pos["symbol"], "error": err}
            try:
                rec = build_record(raw, pos)
                rec["fetched_at"] = ts
                _apply_pay_lag(rec, cache)
                if (raw.get("fund") or {}).get("error") and not err:
                    err = "Some fund details missing: " + raw["fund"]["error"]
                if err:
                    rec["warning"] = err
                return rec, None
            except Exception as e:
                return None, {"symbol": pos["symbol"], "error": f"processing failed: {e}"}

        with ThreadPoolExecutor(max_workers=4) as ex:
            for rec, err in ex.map(work, holdings):
                if rec:
                    out.append(rec)
                if err:
                    errors.append(err)
        if not DEMO:
            _save_cache(cache)

        total_mv = sum(r["market_value"] or 0 for r in out)
        total_inc = sum(r["annual_income"] or 0 for r in out)
        total_cost = sum((r["cost_basis"] or 0) * (r["shares"] or 0) for r in out if r["cost_basis"] and r["shares"])
        for r in out:
            r["weight"] = r["market_value"] / total_mv * 100 if total_mv and r["market_value"] else None
        monthly = [0.0] * 12
        for r in out:
            for i, v in enumerate(r.get("proj_monthly") or []):
                monthly[i] += v
        er_w = sum((r["expense_ratio"] or 0) * (r["market_value"] or 0) for r in out)
        return {
            "demo": DEMO,
            "public_demo": PUBLIC_DEMO,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "holdings": out,
            "errors": errors,
            "portfolio": {
                "market_value": total_mv or None,
                "annual_income": total_inc or None,
                "monthly_avg": total_inc / 12 if total_inc else None,
                "yield": total_inc / total_mv * 100 if total_mv else None,
                "yield_on_cost": total_inc / total_cost * 100 if total_cost else None,
                "cost": total_cost or None,
                "gain": total_mv - total_cost if total_cost else None,
                "weighted_er": er_w / total_mv if total_mv else None,
                "monthly_income": monthly,
            },
        }


# --------------------------------------------------------------------------
# Symbol Comparison tab: price history for any tickers over a chosen period
# --------------------------------------------------------------------------
# period -> (Yahoo range to fetch, bar interval, bars per year). Short periods fetch a full year
# so the trailing-12-month yield is always available; the series is then cut to the period.
COMPARE_PERIODS = {
    "1M": ("1y", "1d", 252), "3M": ("1y", "1d", 252), "6M": ("1y", "1d", 252), "YTD": ("1y", "1d", 252),
    "1Y": ("1y", "1d", 252), "3Y": ("3y", "1d", 252), "5Y": ("5y", "1wk", 52), "10Y": ("10y", "1wk", 52),
    "MAX": ("max", "1mo", 12),
}
COMPARE_MAX_SYMBOLS = 10
_cmp_cache: dict = {}
_cmp_lock = threading.Lock()


def _period_start(period: str, today: datetime) -> datetime | None:
    months = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60, "10Y": 120}.get(period)
    if period == "YTD":
        return datetime(today.year, 1, 1)
    if months is None:
        return None  # MAX: everything
    y, m = today.year, today.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(today.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30,
                          31, 31, 30, 31, 30, 31][m - 1])
    return datetime(y, m, day)


def _fetch_series_stdlib(symbol: str, rng: str, interval: str) -> dict:
    ch = _chart(symbol, rng, interval)
    meta = ch.get("meta") or {}
    ts = ch.get("timestamp") or []
    ind = ch.get("indicators") or {}
    closes = ((ind.get("quote") or [{}])[0]).get("close") or []
    adjs = ((ind.get("adjclose") or [{}])[0]).get("adjclose") or []
    bars = []
    for i, (t, c) in enumerate(zip(ts, closes)):
        if c is None:
            continue
        a = adjs[i] if i < len(adjs) and adjs[i] is not None else None
        bars.append({"date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "close": float(c), "adj": float(a) if a is not None else None})
    divs = [{"date": datetime.fromtimestamp(int(d.get("date") or k), tz=timezone.utc).strftime("%Y-%m-%d"),
             "amount": float(d["amount"])}
            for k, d in ((ch.get("events") or {}).get("dividends") or {}).items() if d.get("amount")]
    return {"name": meta.get("longName") or meta.get("shortName") or symbol,
            "type": (meta.get("instrumentType") or "").upper(),
            "currency": meta.get("currency") or "",
            "price": meta.get("regularMarketPrice"),
            "bars": bars, "dividends": sorted(divs, key=lambda d: d["date"])}


def _fetch_series_yf(symbol: str, rng: str, interval: str) -> dict:
    import yfinance as yf
    t = yf.Ticker(symbol)
    h = t.history(period=rng, interval=interval, auto_adjust=False, actions=True)
    bars, divs = [], []
    if h is not None and len(h):
        adj_col = "Adj Close" if "Adj Close" in h.columns else None
        for idx, row in h.iterrows():
            c = float(row["Close"])
            if c != c:
                continue
            bars.append({"date": idx.strftime("%Y-%m-%d"), "close": c,
                         "adj": float(row[adj_col]) if adj_col and row[adj_col] == row[adj_col] else None})
            if "Dividends" in h.columns and float(row["Dividends"] or 0) > 0:
                divs.append({"date": idx.strftime("%Y-%m-%d"), "amount": float(row["Dividends"])})
    try:
        fi = t.fast_info
        price, cur = fi.get("lastPrice"), fi.get("currency")
    except Exception:
        price, cur = None, ""
    name = symbol
    try:
        name = (t.info or {}).get("longName") or symbol
    except Exception:
        pass
    return {"name": name, "type": "", "currency": cur or "", "price": price or (bars[-1]["close"] if bars else None),
            "bars": bars, "dividends": divs}


def _demo_series(symbol: str, rng: str, interval: str) -> dict:
    """Synthetic history for --demo: one daily series per symbol, cut and thinned per period."""
    import random
    rnd = random.Random("cmp-" + symbol)
    years = rnd.choice([6, 12, 20])
    drift, vol = rnd.uniform(0.0, 0.12), rnd.uniform(0.10, 0.26)
    dy = rnd.choice([0.0, 0.015, 0.03, 0.06, 0.11])
    today = datetime.now()
    d = today - timedelta(days=int(365 * years))
    price = adj = rnd.uniform(20, 120)
    bars, divs, n = [], [], 0
    while d <= today:
        if d.weekday() < 5:
            r = rnd.gauss((drift - dy) / 252, vol / 252 ** 0.5)
            price *= 1 + r
            adj *= 1 + r + dy / 252
            n += 1
            if dy and n % 63 == 0:
                divs.append({"date": d.strftime("%Y-%m-%d"), "amount": round(price * dy / 4, 4)})
            bars.append({"date": d.strftime("%Y-%m-%d"), "close": price, "adj": adj})
        d += timedelta(days=1)
    yrs = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}.get(rng)
    if yrs:
        cut = (today - timedelta(days=int(365.25 * yrs) + 3)).strftime("%Y-%m-%d")
        bars = [b for b in bars if b["date"] >= cut]
    step = {"1d": 1, "1wk": 5, "1mo": 21}[interval]
    bars = bars[::-1][::step][::-1]
    # Match the price level and payouts of the demo holdings data, so the tabs agree with each other.
    ref = demo_raw(symbol)
    k = ref["info"]["regularMarketPrice"] / bars[-1]["close"]
    bars = [{"date": b["date"], "close": b["close"] * k, "adj": b["adj"] * k} for b in bars]
    if dy:
        divs = ref["dividends"]
    return {"name": ref["info"]["longName"], "type": ref["info"]["quoteType"], "currency": "USD",
            "price": bars[-1]["close"], "bars": bars, "dividends": divs}


def _get_series(symbol: str, rng: str, interval: str, force: bool) -> dict:
    if DEMO:
        return _demo_series(symbol, rng, interval)
    key = f"{symbol}|{rng}|{interval}"
    with _cmp_lock:
        ent = _cmp_cache.get(key)
    if ent and not force and time.time() - ent["ts"] < CACHE_TTL_SECONDS:
        return ent["data"]
    if PUBLIC_DEMO and not ent and not _public_lookup_ok():
        raise ValueError(BUSY_MSG)
    try:
        fetch = _fetch_series_yf if USE_YFINANCE else _fetch_series_stdlib
        if PUBLIC_DEMO:
            with _yahoo_slots:
                data = fetch(symbol, rng, interval)
        else:
            data = fetch(symbol, rng, interval)
        if not data["bars"]:
            raise ValueError("no price history - check the symbol")
    except Exception:
        if ent:
            return ent["data"]
        raise
    with _cmp_lock:
        _cmp_cache[key] = {"ts": time.time(), "data": data}
        if PUBLIC_DEMO and len(_cmp_cache) > 400:  # keep memory bounded on a small server
            for k in sorted(_cmp_cache, key=lambda k: _cmp_cache[k]["ts"])[:100]:
                del _cmp_cache[k]
    return data


def compare_symbol(symbol: str, period: str, force: bool = False) -> dict:
    rng, interval, per_year = COMPARE_PERIODS[period]
    raw = _get_series(symbol, rng, interval, force)
    bars, divs = raw["bars"], raw["dividends"]
    today = datetime.now()
    start = _period_start(period, today)
    # Base = the last bar on or before the period start, so a 1-year return runs from a year ago.
    i0 = 0
    if start is not None:
        s = start.strftime("%Y-%m-%d")
        before = [i for i, b in enumerate(bars) if b["date"] <= s]
        i0 = before[-1] if before else 0
    win = bars[i0:]
    if len(win) < 2:
        raise ValueError("not enough price history for this period")
    # Total return index: Yahoo's dividend-adjusted close when present, otherwise dividends
    # reinvested at the close of the bar they fall in.
    if all(b["adj"] is not None for b in win):
        tr = [b["adj"] for b in win]
    else:
        tr, units, j = [], 1.0, 0
        dv = [d for d in divs if d["date"] > win[0]["date"]]
        for b in win:
            while j < len(dv) and dv[j]["date"] <= b["date"]:
                units *= 1 + dv[j]["amount"] / b["close"]
                j += 1
            tr.append(units * b["close"])
    p0, t0 = win[0]["close"], tr[0]
    price_pct = [round((b["close"] / p0 - 1) * 100, 3) for b in win]
    tr_pct = [round((v / t0 - 1) * 100, 3) for v in tr]
    first, last = _d(win[0]["date"]), _d(win[-1]["date"])
    yrs = (last - first).days / 365.25
    total = tr_pct[-1]
    ann = ((1 + total / 100) ** (1 / max(yrs, 1)) - 1) * 100 if yrs >= 0.95 and total > -100 else None
    rets = [tr[k] / tr[k - 1] - 1 for k in range(1, len(tr)) if tr[k - 1]]
    vol = None
    if len(rets) >= 5:
        m = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(per_year) * 100
    peak, mdd = tr[0], 0.0
    for v in tr:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    in_period = [d for d in divs if win[0]["date"] < d["date"] <= win[-1]["date"]]
    price = raw.get("price") or win[-1]["close"]
    ttm_cut = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    ttm_amt = sum(d["amount"] for d in divs if d["date"] > ttm_cut)
    wanted_start = start.strftime("%Y-%m-%d") if start else None
    return {
        "symbol": symbol, "name": raw["name"], "type": raw["type"], "currency": raw["currency"],
        "price": price, "dates": [b["date"] for b in win], "price_pct": price_pct, "tr_pct": tr_pct,
        "price_return": price_pct[-1], "total_return": total,
        "income_return": total - price_pct[-1],
        "annualized": ann, "volatility": vol, "max_drawdown": mdd * 100,
        "high": max(b["close"] for b in win), "low": min(b["close"] for b in win),
        "divs_in_period": sum(d["amount"] for d in in_period) or None, "div_count": len(in_period),
        "ttm_yield": ttm_amt / price * 100 if price and ttm_amt else None,
        "first_date": win[0]["date"], "last_date": win[-1]["date"],
        # history shorter than the period asked for (a newer fund): returns run from its first price
        "partial": bool(wanted_start and win[0]["date"] > wanted_start and i0 == 0
                        and (_d(win[0]["date"]) - start).days > 10),
    }


def build_compare(symbols: list[str], period: str, force: bool = False) -> dict:
    period = period.upper()
    if period not in COMPARE_PERIODS:
        raise ValueError(f"unknown period {period}")
    syms = []
    for s in symbols:
        s = s.strip().upper()
        if s and s not in syms:
            syms.append(s)
    syms = syms[:COMPARE_MAX_SYMBOLS]

    def work(sym):
        if not re.fullmatch(r"[A-Z0-9.\-^=]{1,15}", sym):
            return {"symbol": sym, "error": "not a valid ticker"}
        try:
            return compare_symbol(sym, period, force)
        except Exception as e:
            return {"symbol": sym, "error": str(e)}

    with ThreadPoolExecutor(max_workers=4) as ex:
        series = list(ex.map(work, syms))
    return {"demo": DEMO, "period": period, "generated_at": datetime.now(timezone.utc).isoformat(),
            "series": series}


# --------------------------------------------------------------------------
# Research tab: the full detail record for any one symbol (held or not)
# --------------------------------------------------------------------------
def build_research(symbol: str, force: bool = False) -> dict:
    sym = (symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-^=]{1,15}", sym):
        raise ValueError("not a valid ticker")
    with _lock:
        held = next((r for r in read_holdings() if r["symbol"] == sym), None)
        cache = _load_cache()
    position = held or {"symbol": sym, "shares": None, "cost_basis": None}
    # Fetch outside the lock so the main dashboard isn't held up by a slow lookup.
    raw, err, ts = get_raw(sym, cache, force, budget=not held)
    if raw is None:
        raise ValueError(err or "no data returned - check the symbol")
    if not DEMO and sym in cache:
        with _lock:
            c = _load_cache()
            c[sym] = cache[sym]
            _save_cache(c)
    rec = build_record(raw, position)
    rec["fetched_at"] = ts
    _apply_pay_lag(rec, cache)
    if (raw.get("fund") or {}).get("error") and not err:
        err = "Some fund details missing: " + raw["fund"]["error"]
    if err:
        rec["warning"] = err
    rec["in_holdings"] = bool(held)
    return {"demo": DEMO, "holding": rec}


# --------------------------------------------------------------------------
# Planning tab: quotes for any symbols, and saved what-if scenarios
# --------------------------------------------------------------------------
PLAN_MAX_SYMBOLS = 60
PLAN_FIELDS = ("symbol", "name", "type", "price", "ttm_yield", "fwd_yield", "frequency", "freq_n",
               "expense_ratio", "dist_type", "dist_type_source", "strategy", "exposure", "focus",
               "flags", "pay_shape", "total_return_12m")


def build_plan_quotes(symbols: list[str], force: bool = False) -> dict:
    """Yield, payout, distribution type etc. for symbols you may not hold (no position needed)."""
    syms = []
    for s in symbols:
        s = (s or "").strip().upper()
        if s and s not in syms and re.fullmatch(r"[A-Z0-9.\-^=]{1,15}", s):
            syms.append(s)
    syms = syms[:PLAN_MAX_SYMBOLS]
    with _lock:
        held = {r["symbol"]: r for r in read_holdings()}
        cache = _load_cache()

    def work(sym):
        raw, err, ts = get_raw(sym, cache, force, budget=sym not in held)
        if raw is None:
            return {"symbol": sym, "error": err or "no data returned - check the symbol"}
        try:
            # Use your own overrides (distribution type, frequency...) but not your share count.
            pos = dict(held.get(sym) or {"symbol": sym})
            pos.update({"symbol": sym, "shares": None, "cost_basis": None})
            rec = build_record(raw, pos)
            out = {k: rec.get(k) for k in PLAN_FIELDS}
            if err:
                out["warning"] = err
            return out
        except Exception as e:
            return {"symbol": sym, "error": f"processing failed: {e}"}

    with ThreadPoolExecutor(max_workers=4) as ex:
        quotes = list(ex.map(work, syms))
    if not DEMO:
        with _lock:
            c = _load_cache()
            for sym in syms:
                if sym in cache:
                    c[sym] = cache[sym]
            _save_cache(c)
    return {"demo": DEMO, "quotes": quotes}


def plans_path() -> Path:
    """Saved scenarios live next to the holdings file, so they sync with it (OneDrive, Dropbox...)."""
    if PUBLIC_DEMO:
        return DEMO_PLANS
    return HOLDINGS_CSV.with_name(HOLDINGS_CSV.stem + "-plans.json")


def read_plans() -> list:
    p = plans_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("plans", []) if isinstance(data, dict) else []
    except Exception:
        return []


def write_plans(plans: list) -> None:
    if not isinstance(plans, list) or not all(isinstance(x, dict) for x in plans):
        raise ValueError("plans must be a list of objects")
    if len(plans) > 200:
        raise ValueError("too many saved scenarios (200 max)")
    p = plans_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "plans": plans}, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = INDEX_HTML.read_bytes()
            if PUBLIC_DEMO:  # the page hides editing, import and Stop when the body has this class
                page = page.replace(b"<body>", b'<body class="public-demo">', 1)
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, '{"ok":true}')
        if path == "/api/data":
            # The public demo ignores Refresh: prices come from the cache, refreshed in the background.
            force = "refresh=1" in self.path and not PUBLIC_DEMO
            return self._send(200, json.dumps(build_dashboard(force), default=str))
        if path == "/api/research":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                res = build_research((q.get("symbol") or [""])[0], force=(q.get("refresh") or [""])[0] == "1" and not PUBLIC_DEMO)
                return self._send(200, json.dumps(res, default=str))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if path == "/api/compare":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                res = build_compare((q.get("symbols") or [""])[0].split(","), (q.get("period") or ["1Y"])[0],
                                    force=(q.get("refresh") or [""])[0] == "1" and not PUBLIC_DEMO)
                return self._send(200, json.dumps(res, default=str))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if path == "/api/plan/quotes":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                res = build_plan_quotes((q.get("symbols") or [""])[0].split(","),
                                        force=(q.get("refresh") or [""])[0] == "1" and not PUBLIC_DEMO)
                return self._send(200, json.dumps(res, default=str))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if path == "/api/plans":
            with _lock:
                return self._send(200, json.dumps({"plans": read_plans(),
                                                   "path": "" if PUBLIC_DEMO else str(plans_path())}))
        if path == "/api/holdings":
            return self._send(200, json.dumps(read_holdings()))
        if path == "/api/holdings/info":
            if PUBLIC_DEMO:
                return self._send(200, json.dumps({"path": "the demo's sample portfolio (read-only)", "default": False}))
            return self._send(200, json.dumps({"path": str(HOLDINGS_CSV),
                                               "default": HOLDINGS_CSV == HERE / "holdings.csv"}))
        if path == "/api/holdings/file":
            # The holdings file exactly as saved, for Import CSV on another computer.
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\r\n")
            w.writeheader()
            for r in read_holdings():
                w.writerow(_csv_row(r))
            data = buf.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="holdings-{datetime.now():%Y-%m-%d}.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if PUBLIC_DEMO:
            # Read-only: no saving holdings or scenarios, no importing, no stopping the server.
            return self._send(403, '{"error":"The live demo is read-only."}')
        if self.path.split("?")[0] == "/api/shutdown":
            # Stop button / stop_windows.bat. Only from this computer, and only with the custom
            # header, so another website open in the browser can't stop the dashboard.
            if self.client_address[0] not in ("127.0.0.1", "::1") or self.headers.get("X-Dashboard-Stop") != "1":
                return self._send(403, '{"error":"forbidden"}')
            self._send(200, '{"ok":true}')
            print("\nStop requested from the dashboard. Shutting down...")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path.split("?")[0] == "/api/plans":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 5_000_000:
                    raise ValueError("too large")
                body = json.loads(self.rfile.read(n) or b"{}")
                with _lock:
                    write_plans(body.get("plans"))
                return self._send(200, json.dumps({"ok": True, "path": str(plans_path())}))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if self.path.split("?")[0] != "/api/holdings":
            return self._send(404, '{"error":"not found"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            rows = body.get("holdings")
            if not isinstance(rows, list):
                raise ValueError("holdings must be a list")
            clean = []
            for r in rows:
                sym = str(r.get("symbol", "")).strip().upper()
                if sym:
                    clean.append({"symbol": sym, "shares": _num(r.get("shares")),
                                  "cost_basis": _num(r.get("cost_basis")),
                                  "frequency": str(r.get("frequency") or "").strip(),
                                  "core_focus": str(r.get("core_focus") or "").strip(),
                                  "distribution_type": str(r.get("distribution_type") or "").strip(),
                                  "strategy": str(r.get("strategy") or "").strip(),
                                  "exposure": str(r.get("exposure") or "").strip(),
                                  "flagged": _flag(r.get("flagged")),
                                  "notes": str(r.get("notes") or "").strip()})
            backup = None
            with _lock:
                if body.get("backup"):
                    backup = backup_holdings()
                write_holdings(clean)
            self._send(200, json.dumps({"ok": True, "count": len(clean), "backup": backup}))
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _keep_warm():
    """Public demo: load the sample portfolio now and again before the cache runs out,
    so visitors get cached prices instead of waiting on Yahoo."""
    while True:
        try:
            d = build_dashboard(force=True)
            stand_in = sum(1 for h in d["holdings"] if "sample numbers" in (h.get("warning") or ""))
            print(f"{datetime.now():%Y-%m-%d %H:%M} Demo portfolio prices refreshed"
                  + (f" ({stand_in} symbols using sample numbers: Yahoo unreachable)" if stand_in else "."), flush=True)
        except Exception as e:
            print(f"Demo refresh failed: {e}", flush=True)
        time.sleep(max(600, CACHE_TTL_SECONDS - 15 * 60))


def main():
    global DEMO, USE_YFINANCE, PUBLIC_DEMO, CACHE_TTL_SECONDS
    ap = argparse.ArgumentParser(description="Local dividend holdings dashboard")
    ap.add_argument("--port", type=int, default=None, help="default 8765, or $PORT with --public-demo")
    ap.add_argument("--demo", action="store_true", help="use synthetic data (no internet needed)")
    ap.add_argument("--save-demo-snapshot", action="store_true",
                    help="save fund details for the demo portfolio to demo/fund-snapshot.json, then exit")
    ap.add_argument("--public-demo", action="store_true",
                    help="read-only sample portfolio for hosting publicly (also PUBLIC_DEMO=1); see DEPLOY.md")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--yfinance", action="store_true",
                    help="fetch with the yfinance library instead of the built-in client")
    ap.add_argument("--holdings", metavar="PATH",
                    help="holdings file (or folder) to use, e.g. in OneDrive/Dropbox/iCloud Drive")
    a = ap.parse_args()
    global HOLDINGS_CSV
    if a.save_demo_snapshot:
        USE_YFINANCE = a.yfinance
        print("Saving fund details for the demo portfolio from Yahoo Finance...")
        save_demo_snapshot()
        return
    PUBLIC_DEMO = a.public_demo or _env_on("PUBLIC_DEMO")
    DEMO = a.demo
    if PUBLIC_DEMO:
        # Never touch a real holdings file: the sample portfolio ships in demo/.
        HOLDINGS_CSV = DEMO_HOLDINGS
        DEMO = DEMO or os.environ.get("DEMO_DATA", "").strip().lower() == "synthetic"
        try:
            hours = float(os.environ.get("DEMO_CACHE_HOURS") or PUBLIC_CACHE_HOURS)
        except ValueError:
            hours = PUBLIC_CACHE_HOURS
        CACHE_TTL_SECONDS = int(max(0.5, hours) * 3600)
        a.no_browser = True
    else:
        HOLDINGS_CSV = resolve_holdings_path(a.holdings)
    USE_YFINANCE = a.yfinance
    if USE_YFINANCE and not DEMO:
        try:
            import yfinance  # noqa: F401
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"\nCould not load yfinance with {sys.executable} (Python {sys.version.split()[0]}): {e!r}")
            print("Run without --yfinance to use the built-in client (no extra packages needed).")
            sys.exit(1)
    if PUBLIC_DEMO:
        # A hosting service (Render etc.) says which port to listen on, and traffic arrives from outside.
        port = a.port or int(os.environ.get("PORT") or 8765)
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        srv.daemon_threads = True
        print(f"Public read-only demo listening on port {port} "
              f"({'synthetic data' if DEMO else f'live Yahoo Finance data, cached {CACHE_TTL_SECONDS / 3600:g} h'})",
              flush=True)
        if not DEMO:
            threading.Thread(target=_keep_warm, daemon=True).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        srv.server_close()
        return
    a.port = a.port or 8765
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Dividend dashboard running at {url}  ({'DEMO data' if DEMO else 'live Yahoo Finance data'})")
    print(f"Holdings file: {HOLDINGS_CSV}")
    print("Edit it or use the Edit holdings button in the page.")
    print("To stop: click Stop in the dashboard, run stop_windows.bat / stop_mac_linux.sh, or press Ctrl+C.")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    print("Stopped.")


if __name__ == "__main__":
    main()
