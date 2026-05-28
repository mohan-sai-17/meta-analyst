"""
update_insider_s3.py
=====================
Fetch Form 4 insider transaction data from SEC EDGAR for all S&P 500 tickers.
Writes Parquet to S3: raw/insider/date=YYYY-MM-DD/insider.parquet

Per-ticker metrics:
  - net_insider_buy_90d     : Total $ of purchases minus $ of sales (last 90 days)
  - insider_buy_count_90d   : Number of purchase (P) transactions
  - insider_sell_count_90d  : Number of sale (S) transactions
  - last_transaction_date   : ISO date of most recent Form 4 transaction
  - insider_buy_flag        : True when net_insider_buy_90d > 0
  - fetch_error             : Non-null if EDGAR request failed

EDGAR NOTES
-----------
- Form 4 is indexed in form.idx under the ISSUER'S (company's) CIK.
  company_tickers.json also uses company CIKs, so the two can be cross-referenced
  to map filings directly to tickers without per-ticker submissions requests.
- Full submission .txt files are accessible at:
    {SEC}/Archives/edgar/data/{company_cik}/{acc_nodash}/{acc_dashed}.txt
  The raw Form 4 XML is embedded inside the .txt as an <ownershipDocument> block.
- EDGAR rate limit: 10 req/sec. We use a global token-bucket at 8 req/s.

INDEX-BASED ARCHITECTURE
--------------------------
Old approach : 500 submissions JSON requests  (~63s just for discovery)
New approach : 1 form.idx download (1 request) -> filter by CIK -> fetch matching XMLs

Two S3 files maintained:
  raw/insider_txns/insider_txns.parquet  — rolling 90-day transaction rows
  raw/insider_txns/fetched_dates.json    — accession numbers already fetched

Daily flow:
  1. Load existing transactions + known accession set from S3
  2. Download quarterly form.idx (1 request) -> filter for our tickers + new accessions
  3. Fetch .txt only for new accession numbers (parallel, rate-limited)
  4. Append new rows, save both files
  5. Recompute per-ticker aggregates -> raw/insider/date=.../

Expected runtimes:
  Cold start (first run) : ~5-8 min (2 idx requests + ~4000 XMLs)
  Daily warm run         : ~1-2 min (2 idx requests + ~30-80 new XMLs)

Usage:
    python update_insider_s3.py
    python update_insider_s3.py --dry-run   # discover filings + test 5 XMLs, no S3 writes
    python update_insider_s3.py --force     # re-fetch all, ignore cache
"""

import re
import time
import threading
import warnings
import argparse
import requests
import boto3
import pandas as pd
from io import BytesIO
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET          = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION         = "us-east-1"
MAX_WORKERS        = 10
EDGAR_RATE         = 8.0          # req/s globally (EDGAR limit = 10; we use 8)
INSIDER_DAYS       = 90
DISCOVERY_LOOKBACK = 3            # days to look back in form.idx (catches weekends/holidays)
TXNS_KEY           = "raw/insider_txns/insider_txns.parquet"
EDGAR_BASE         = "https://data.sec.gov"
SEC_BASE           = "https://www.sec.gov"
HEADERS            = {
    "User-Agent"     : "MetaAnalyst-Pipeline your-email@example.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept"         : "application/json, text/html, */*",
}

TXN_COLS = [
    "ticker", "cik", "accession_no", "filing_date",
    "transaction_date", "transaction_code",
    "shares", "price_per_share", "total_value",
]


# ============================================================================
# RATE LIMITER  (token-bucket, shared across all threads)
# ============================================================================

class RateLimiter:
    def __init__(self, rate: float):
        self._rate    = rate
        self._lock    = threading.Lock()
        self._last_ts = 0.0

    def acquire(self):
        gap = 1.0 / self._rate
        with self._lock:
            now  = time.monotonic()
            wait = self._last_ts + gap - now
            if wait > 0:
                time.sleep(wait)
            self._last_ts = time.monotonic()


_rate_limiter = RateLimiter(EDGAR_RATE)


# ============================================================================
# PER-THREAD SESSION
# ============================================================================

_local = threading.local()

def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.session = s
    return _local.session

def _get(url: str, timeout: int = 30) -> requests.Response:
    _rate_limiter.acquire()
    return _session().get(url, timeout=timeout)


# ============================================================================
# EDGAR HELPERS
# ============================================================================

def get_cik_map() -> dict:
    """Download EDGAR company_tickers.json -> {TICKER: zero_padded_cik}."""
    url  = f"{SEC_BASE}/files/company_tickers.json"
    resp = _get(url, timeout=30)
    resp.raise_for_status()
    return {
        entry["ticker"]: str(entry["cik_str"]).zfill(10)
        for entry in resp.json().values()
        if "ticker" in entry and "cik_str" in entry
    }


def _quarter(dt: date) -> str:
    return f"QTR{(dt.month - 1) // 3 + 1}"


def fetch_form_idx_accessions(cik_to_ticker: dict, ticker_set: set,
                               known: set, lookback_days: int) -> list:
    """
    Download the EDGAR quarterly form.idx and return new Form 4 filings
    for our tickers that are not already in `known`.

    Checks the current quarter; also checks the previous quarter when we are
    within `lookback_days` days of the start of the current quarter, so that
    filings from just before a quarter boundary are not missed.

    Returns list of dicts:
      {ticker, cik, date_filed, accession_no, filename}
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    today  = date.today()

    # Which quarter(s) to fetch?
    quarters = [(today.year, _quarter(today))]
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    days_into_quarter = (today - date(today.year, q_start_month, 1)).days
    if days_into_quarter < lookback_days:
        prev = date(today.year, q_start_month, 1) - timedelta(days=1)
        quarters.append((prev.year, _quarter(prev)))

    all_filings = []
    for year, q in quarters:
        url = f"{SEC_BASE}/Archives/edgar/full-index/{year}/{q}/form.idx"
        print(f"  [IDX]  Fetching {year}/{q} form.idx ...", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            resp = _get(url, timeout=60)
            resp.raise_for_status()
        except Exception as e:
            print(f"FAILED ({e})")
            continue

        raw   = resp.text
        size  = len(raw) / 1024
        lines = raw.splitlines()
        print(f"{size:,.0f} KB  ({time.monotonic()-t0:.1f}s)")

        # Locate the separator line (dashes) to find header offsets
        sep_idx = next((i for i, ln in enumerate(lines) if ln.startswith("---")), None)
        if sep_idx is None:
            print(f"  [IDX]  WARNING: could not parse {year}/{q} form.idx header")
            continue

        header = lines[sep_idx - 1] if sep_idx > 0 else ""
        try:
            c0 = header.index("Form Type")
            c1 = header.index("Company Name")
        except ValueError:
            print(f"  [IDX]  WARNING: unexpected header format in {year}/{q} form.idx")
            continue

        rows_found = 0
        for line in lines[sep_idx + 1:]:
            # Anchor on 'edgar/' which is always present in the filename column
            ei = line.find("edgar/")
            if ei < 0:
                continue
            form_type = line[c0:c1].strip()
            if form_type not in ("4", "4/A"):
                continue
            filename = line[ei:].strip()
            # Date (YYYY-MM-DD, 10 chars) sits just before 'edgar/' after rstrip
            pre      = line[:ei].rstrip()
            date_str = pre[-10:].strip()
            if date_str < cutoff:
                continue
            # CIK is the last whitespace token before the date
            cik_raw    = pre[:-10].split()[-1] if pre[:-10].split() else ""
            cik_padded = cik_raw.zfill(10)

            ticker = cik_to_ticker.get(cik_padded)
            if not ticker or ticker not in ticker_set:
                continue

            # Derive accession number from filename
            # e.g. edgar/data/12927/000122520826003656/0001225208-26-003656.txt
            basename = filename.rsplit("/", 1)[-1]
            if not basename.endswith(".txt"):
                continue
            acc_dashed = basename[:-4]          # strip .txt
            acc_nd     = acc_dashed.replace("-", "")

            if acc_nd in known:
                continue

            all_filings.append({
                "ticker"      : ticker,
                "cik"         : cik_padded,
                "date_filed"  : date_str,
                "accession_no": acc_nd,
                "filename"    : filename,
            })
            rows_found += 1

        print(f"  [IDX]  {year}/{q}: {rows_found} new Form 4 filings for our tickers")

    # Deduplicate by accession_no (a filing cannot appear in two quarters, but
    # if the same accession is somehow listed twice we deduplicate defensively)
    seen   = set()
    deduped = []
    for f in all_filings:
        if f["accession_no"] not in seen:
            seen.add(f["accession_no"])
            deduped.append(f)

    return deduped


def parse_form4_txt(filing: dict) -> tuple:
    """
    Fetch the full submission .txt for a Form 4 filing and extract
    nonDerivativeTransaction rows from the embedded <ownershipDocument> XML.

    URL pattern: {SEC}/Archives/edgar/data/{cik}/{acc_nd}/{acc_dashed}.txt

    Returns (list_of_transaction_dicts, error_or_None).
    """
    cik_stripped = filing["cik"].lstrip("0")
    acc_nd       = filing["accession_no"]
    acc_dashed   = f"{acc_nd[:10]}-{acc_nd[10:12]}-{acc_nd[12:]}"
    url          = f"{SEC_BASE}/Archives/edgar/data/{cik_stripped}/{acc_nd}/{acc_dashed}.txt"

    try:
        resp = _get(url, timeout=30)
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code}"
        txt   = resp.text
        start = txt.find("<ownershipDocument>")
        if start == -1:
            start = txt.find("<?xml")
        end   = txt.find("</ownershipDocument>")
        if start == -1 or end == -1:
            return [], "ownershipDocument not found in .txt"
        xml_str = txt[start : end + len("</ownershipDocument>")]
        root    = ET.fromstring(xml_str)
    except Exception as e:
        return [], str(e)[:200]

    transactions = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        try:
            tx_date = txn.findtext(".//transactionDate/value", "").strip()
            tx_code = txn.findtext(".//transactionCoding/transactionCode", "").strip()
            shares  = txn.findtext(".//transactionAmounts/transactionShares/value", "").strip()
            price   = txn.findtext(".//transactionAmounts/transactionPricePerShare/value", "").strip()

            if tx_code not in ("P", "S", "A", "D"):
                continue

            shares_val = float(shares) if shares else None
            price_val  = float(price)  if price  else None
            total      = round(shares_val * price_val, 2) if (shares_val and price_val) else None

            transactions.append({
                "transaction_date" : tx_date,
                "transaction_code" : tx_code,
                "shares"           : shares_val,
                "price_per_share"  : price_val,
                "total_value"      : total,
            })
        except Exception:
            continue

    return transactions, None


# ============================================================================
# AGGREGATE
# ============================================================================

def aggregate_from_txns(txns_df: pd.DataFrame, tickers: list,
                        export_date: str) -> pd.DataFrame:
    """Compute per-ticker insider summary from the rolling transactions table."""
    cutoff = (date.today() - timedelta(days=INSIDER_DAYS)).isoformat()
    window = txns_df[txns_df["filing_date"] >= cutoff] if len(txns_df) else txns_df

    records = []
    for ticker in tickers:
        rows = window[window["ticker"] == ticker] if len(window) else pd.DataFrame()

        if len(rows) == 0:
            records.append({
                "ticker"                 : ticker,
                "as_of_date"             : export_date,
                "net_insider_buy_90d"    : None,
                "insider_buy_count_90d"  : 0,
                "insider_sell_count_90d" : 0,
                "last_transaction_date"  : None,
                "insider_buy_flag"       : None,
                "fetch_error"            : None,
            })
            continue

        buys  = rows[rows["transaction_code"] == "P"]["total_value"].dropna()
        sells = rows[rows["transaction_code"] == "S"]["total_value"].dropna()
        net   = round(float(buys.sum() - sells.sum()), 2)

        dates = rows["transaction_date"].dropna()
        records.append({
            "ticker"                 : ticker,
            "as_of_date"             : export_date,
            "net_insider_buy_90d"    : net,
            "insider_buy_count_90d"  : int((rows["transaction_code"] == "P").sum()),
            "insider_sell_count_90d" : int((rows["transaction_code"] == "S").sum()),
            "last_transaction_date"  : dates.max() if len(dates) else None,
            "insider_buy_flag"       : net > 0,
            "fetch_error"            : None,
        })

    return pd.DataFrame(records)


# ============================================================================
# S3 HELPERS
# ============================================================================

def _s3():
    return boto3.client("s3", region_name=AWS_REGION)


def load_txns() -> pd.DataFrame:
    try:
        buf = BytesIO()
        _s3().download_fileobj(S3_BUCKET, TXNS_KEY, buf)
        buf.seek(0)
        return pd.read_parquet(buf)
    except Exception:
        return pd.DataFrame(columns=TXN_COLS)


def save_txns(df: pd.DataFrame):
    buf = BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    _s3().put_object(Bucket=S3_BUCKET, Key=TXNS_KEY, Body=buf.read())


def get_tickers_from_s3() -> list:
    resp    = _s3().list_objects_v2(Bucket=S3_BUCKET, Prefix="raw/stocks_list/")
    objects = sorted(
        [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")],
        key=lambda o: o["Key"],
    )
    if not objects:
        raise RuntimeError("No stocks_list Parquet found in S3.")
    frames = []
    for obj in objects:
        buf = BytesIO()
        _s3().download_fileobj(S3_BUCKET, obj["Key"], buf)
        buf.seek(0)
        frames.append(pd.read_parquet(buf))
    return pd.concat(frames, ignore_index=True)["Ticker"].dropna().unique().tolist()


def watermark_exists(export_date: str) -> bool:
    key = f"raw/insider/date={export_date}/insider.parquet"
    try:
        _s3().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def upload_aggregate(df: pd.DataFrame, export_date: str) -> str:
    key = f"raw/insider/date={export_date}/insider.parquet"
    buf = BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    _s3().put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
    return key


# ============================================================================
# MAIN
# ============================================================================

def run_update(dry_run=False, force=False):
    start_time  = datetime.now()
    export_date = date.today().isoformat()
    mode_label  = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  UPDATE_INSIDER_S3 — SEC EDGAR Form 4 (Index-based){mode_label}")
    print(f"  Started  : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Window   : {INSIDER_DAYS} days")
    print(f"  Lookback : {DISCOVERY_LOOKBACK} days (form.idx discovery)")
    print(f"  Workers  : {MAX_WORKERS}  |  Rate : {EDGAR_RATE} req/s global")
    print(f"  Output   : s3://{S3_BUCKET}/raw/insider/date={export_date}/")
    print("=" * 68)

    if not force and not dry_run and watermark_exists(export_date):
        print(f"\n  Watermark exists for {export_date}. Use --force to re-fetch.")
        print("  Nothing to do. Exiting cleanly.\n")
        return

    # ------------------------------------------------------------------
    # 1. Load tickers + CIK map
    # ------------------------------------------------------------------
    print(f"\n  [INFO] Loading ticker list from S3...")
    tickers = get_tickers_from_s3()
    print(f"  [INFO] {len(tickers)} tickers loaded.")

    print(f"  [CIK]  Downloading EDGAR company_tickers.json...")
    cik_map       = get_cik_map()                          # {ticker: padded_cik}
    cik_to_ticker = {v: k for k, v in cik_map.items()}    # inverted: {padded_cik: ticker}
    ticker_set    = set(tickers)

    matched_tickers = [t for t in tickers if t in cik_map]
    skipped         = [t for t in tickers if t not in cik_map]
    print(f"  [CIK]  Matched {len(matched_tickers)} / {len(tickers)} tickers.")
    if skipped:
        print(f"  [CIK]  {len(skipped)} not in EDGAR map: {skipped}")

    # ------------------------------------------------------------------
    # 2. Load existing rolling transactions
    # ------------------------------------------------------------------
    print(f"\n  [TXNS] Loading existing transactions from S3...")
    if force:
        existing = pd.DataFrame(columns=TXN_COLS)
        known_accessions: set = set()
        print(f"  [TXNS] --force: clearing cache.")
    else:
        existing     = load_txns()
        cutoff_str   = (date.today() - timedelta(days=INSIDER_DAYS + 2)).isoformat()
        if len(existing):
            existing = existing[existing["filing_date"] >= cutoff_str].copy()
        known_accessions = set(existing["accession_no"].dropna().unique())
        print(f"  [TXNS] {len(existing):,} existing rows  |  "
              f"{len(known_accessions):,} accessions cached -> skipping XML for these.")

    # ------------------------------------------------------------------
    # 3. Discover new filings via form.idx  (1-2 HTTP requests)
    # ------------------------------------------------------------------
    print(f"\n  [IDX]  Discovering new Form 4 filings via quarterly index...")
    new_filings = fetch_form_idx_accessions(
        cik_to_ticker, ticker_set, known_accessions, DISCOVERY_LOOKBACK
    )
    unique_tickers_with_filings = len({f["ticker"] for f in new_filings})
    print(f"  [IDX]  {len(new_filings)} new filings to fetch "
          f"across {unique_tickers_with_filings} tickers")

    # ------------------------------------------------------------------
    # 4. Dry run
    # ------------------------------------------------------------------
    if dry_run:
        sample = new_filings[:5]
        print(f"\n  [DRY RUN] Fetching {len(sample)} sample filing(s)...")
        for filing in sample:
            txns, err = parse_form4_txt(filing)
            print(f"    {filing['ticker']:<8}  {filing['accession_no']}  "
                  f"rows={len(txns)}  err={err}")
        print("\n  Dry run complete. No S3 writes.\n")
        return

    # ------------------------------------------------------------------
    # 5. Fetch new transaction XMLs in parallel
    # ------------------------------------------------------------------
    print(f"\n  [FETCH] Fetching {len(new_filings)} new Form 4 .txt files "
          f"({MAX_WORKERS} workers)...")

    all_new_rows = []
    errors       = []
    done         = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(parse_form4_txt, filing): filing
            for filing in new_filings
        }
        for future in as_completed(futures):
            filing       = futures[future]
            txns, err    = future.result()
            if err:
                errors.append(filing["ticker"])
            for t in txns:
                all_new_rows.append({
                    "ticker"       : filing["ticker"],
                    "cik"          : filing["cik"],
                    "accession_no" : filing["accession_no"],
                    "filing_date"  : filing["date_filed"],
                    **t,
                })
            done += 1
            if done % 50 == 0 or done == len(new_filings):
                print(f"    {done}/{len(new_filings)} filings | "
                      f"{len(all_new_rows)} new rows | {len(errors)} errors")

    print(f"\n  [FETCH] Done. {len(all_new_rows)} new rows "
          f"from {len(new_filings)} new filings.")

    # ------------------------------------------------------------------
    # 6. Merge + trim rolling table
    # ------------------------------------------------------------------
    if all_new_rows:
        new_df   = pd.DataFrame(all_new_rows)[TXN_COLS]
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = existing.copy()

    cutoff_final = (date.today() - timedelta(days=INSIDER_DAYS)).isoformat()
    combined     = combined[combined["filing_date"] >= cutoff_final].copy()

    print(f"  [TXNS] Rolling table: {len(combined):,} rows "
          f"(window: {cutoff_final} -> {export_date})")

    # ------------------------------------------------------------------
    # 7. Save updated rolling table
    # ------------------------------------------------------------------
    print(f"  [TXNS] Saving to S3...")
    save_txns(combined)

    # ------------------------------------------------------------------
    # 8. Recompute per-ticker aggregates
    # ------------------------------------------------------------------
    print(f"\n  [AGG]  Computing per-ticker aggregates...")
    agg_df = aggregate_from_txns(combined, tickers, export_date)
    agg_df.loc[agg_df["ticker"].isin(skipped), "fetch_error"] = "not_in_edgar_map"

    buy_flag    = int(agg_df["insider_buy_flag"].sum())
    no_activity = int(
        ((agg_df["insider_buy_count_90d"] == 0) &
         (agg_df["insider_sell_count_90d"] == 0) &
         agg_df["fetch_error"].isna()).sum()
    )

    print(f"\n  [STATS]")
    print(f"    Tickers matched to EDGAR  : {len(matched_tickers)}")
    print(f"    New filings fetched       : {len(new_filings)}")
    print(f"    Insider buying flag       : {buy_flag}")
    print(f"    No activity in {INSIDER_DAYS}d window  : {no_activity}")
    print(f"    Not in EDGAR map          : {len(skipped)}")
    print(f"    Fetch errors              : {len(errors)}")

    print(f"\n  [WRITE] Uploading daily aggregate to S3...")
    key = upload_aggregate(agg_df, export_date)
    print(f"  [WRITE] Done -> s3://{S3_BUCKET}/{key}")

    elapsed = (datetime.now() - start_time).seconds
    mode    = "cold start" if not known_accessions else "incremental"
    print(f"\n  Elapsed : {elapsed}s  ({mode})")
    print("=" * 68)
    print()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch SEC EDGAR Form 4 insider transactions -> S3 (index-based)."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover filings + test 5 XMLs, no S3 writes.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all, ignore cache.")
    args = parser.parse_args()
    run_update(dry_run=args.dry_run, force=args.force)
