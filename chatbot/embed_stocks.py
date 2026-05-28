"""
embed_stocks.py
===============
Builds the Pinecone vector index from S3 mart Parquet files.

Run once after bootstrap, then daily after compute_marts.py completes.
Each ticker becomes one rich-text document covering analyst coverage,
price targets, fundamentals, insider activity, and daily candidate status.
Upsert is idempotent — safe to re-run at any time.

Usage:
    python chatbot/embed_stocks.py

Env vars (chatbot/.env):
    PINECONE_API_KEY  — Pinecone API key
    PINECONE_INDEX    — index name (default: meta-analyst-stocks)
    S3_BUCKET         — S3 bucket name
    AWS_*             — standard AWS credentials
"""

import os
import sys
import tempfile
from pathlib import Path

import boto3
import duckdb
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec
from sentence_transformers import SentenceTransformer

load_dotenv(Path(__file__).parent / ".env")

S3_BUCKET      = os.environ.get("S3_BUCKET", "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID")
PINECONE_KEY   = os.environ["PINECONE_API_KEY"]
PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "meta-analyst-stocks")
EMBED_MODEL    = "nomic-ai/nomic-embed-text-v1.5"
EMBED_DIM      = 768
BATCH_SIZE     = 64

MART_FILES = [
    "mart_firm_scorecard",
    "mart_price_targets",
    "mart_fundamental_health",
    "mart_insider_summary",
    "mart_daily_candidates",
    "mart_seasonality",
]


# ── S3 download ───────────────────────────────────────────────────────────────

def download_marts(s3, tmpdir: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    for mart in MART_FILES:
        key   = f"mart/{mart}.parquet"
        local = os.path.join(tmpdir, f"{mart}.parquet")
        try:
            s3.download_file(S3_BUCKET, key, local)
            paths[mart] = local
            print(f"  OK  {mart}")
        except Exception as exc:
            print(f"  SKP {mart}: {exc}")
    return paths


# ── Document builders ─────────────────────────────────────────────────────────

def _safe(val, fmt: str = "{}", fallback: str = "N/A") -> str:
    if val is None:
        return fallback
    try:
        return fmt.format(val)
    except Exception:
        return str(val)


def build_ticker_doc(con: duckdb.DuckDBPyConnection, ticker: str) -> dict | None:
    """
    Build one rich-text document per ticker.

    All data for a ticker lives in one document (not split) because financial
    signals are correlated — keeping them together preserves semantic coherence
    for the retriever. ~300-500 tokens, well within nomic's context window.
    """
    try:
        sc_rows = con.execute(f"""
            SELECT Firm, signal_count, avg_return_3m, avg_return_6m,
                   win_rate_3m, win_rate_6m
            FROM mart_firm_scorecard
            WHERE Ticker = '{ticker}'
            ORDER BY signal_count DESC
        """).fetchall()

        if not sc_rows:
            return None

        lines = [f"TICKER: {ticker}", ""]

        lines.append("ANALYST COVERAGE:")
        for firm, sig, r3, r6, wr3, wr6 in sc_rows:
            lines.append(
                f"  {firm}: {sig} signals | "
                f"3M WR {_safe(wr3, '{:.0f}%')} / {_safe(r3, '{:+.1f}%')} | "
                f"6M WR {_safe(wr6, '{:.0f}%')} / {_safe(r6, '{:+.1f}%')}"
            )
        lines.append("")

        pt = con.execute(f"""
            SELECT current_price, target_price_3m, target_price_6m,
                   expected_return_3m_pct, expected_return_6m_pct,
                   avg_win_rate_3m, avg_win_rate_6m, top_tier_firm_count
            FROM mart_price_targets WHERE Ticker = '{ticker}' LIMIT 1
        """).fetchone()
        if pt:
            lines += [
                "PRICE TARGETS:",
                f"  Current: ${_safe(pt[0], '{:.2f}')}",
                f"  3M: ${_safe(pt[1], '{:.2f}')} ({_safe(pt[3], '{:+.1f}%')}, WR {_safe(pt[5], '{:.0f}%')})",
                f"  6M: ${_safe(pt[2], '{:.2f}')} ({_safe(pt[4], '{:+.1f}%')}, WR {_safe(pt[6], '{:.0f}%')})",
                f"  Top-tier firms: {_safe(pt[7])}",
                "",
            ]

        fund = con.execute(f"""
            SELECT debt_equity, current_ratio, interest_coverage,
                   fcf_positive_years, passes_health_screen
            FROM mart_fundamental_health WHERE ticker = '{ticker}' LIMIT 1
        """).fetchone()
        if fund:
            lines += [
                "FUNDAMENTAL HEALTH:",
                f"  Debt/Equity: {_safe(fund[0], '{:.2f}')} | "
                f"Current ratio: {_safe(fund[1], '{:.2f}')} | "
                f"Interest coverage: {_safe(fund[2], '{:.1f}x')}",
                f"  FCF positive years: {_safe(fund[3])} | "
                f"Health screen: {'PASS' if fund[4] else 'FAIL'}",
                "",
            ]

        ins = con.execute(f"""
            SELECT net_insider_buy_90d, insider_buy_count_90d,
                   insider_sell_count_90d, insider_buy_flag, insider_strength
            FROM mart_insider_summary WHERE ticker = '{ticker}' LIMIT 1
        """).fetchone()
        if ins:
            lines += [
                "INSIDER ACTIVITY (90 days):",
                f"  Net buying: ${_safe(ins[0], '{:+,.0f}')} | "
                f"Buys/Sells: {_safe(ins[1])}/{_safe(ins[2])} | "
                f"Strength: {_safe(ins[4])}",
                f"  Buy flag: {'YES' if ins[3] else 'NO'}",
                "",
            ]

        seas = con.execute(f"""
            SELECT month, win_rate, avg_return
            FROM mart_seasonality WHERE Ticker = '{ticker}'
            ORDER BY month
        """).fetchall()
        if seas:
            strong = [str(r[0]) for r in seas if (r[1] or 0) >= 60 and (r[2] or 0) > 0]
            if strong:
                lines += [f"STRONG SEASONAL MONTHS (WR≥60%): {', '.join(strong)}", ""]

        cand = con.execute(f"""
            SELECT current_price, target_3m, target_6m,
                   season_win_rate, season_avg_return, hist_vol,
                   passes_health_screen, insider_buy_flag
            FROM mart_daily_candidates WHERE Ticker = '{ticker}' LIMIT 1
        """).fetchone()
        if cand:
            lines += [
                "TODAY'S CANDIDATE: ACTIVE",
                f"  Price: ${_safe(cand[0], '{:.2f}')} | "
                f"3M: ${_safe(cand[1], '{:.2f}')} | "
                f"6M: ${_safe(cand[2], '{:.2f}')}",
                f"  Seasonal WR: {_safe(cand[3], '{:.0f}%')} | "
                f"Hist vol: {_safe(cand[5], '{:.1f}%')} | "
                f"Health: {'PASS' if cand[6] else 'FAIL'} | "
                f"Insider: {'YES' if cand[7] else 'NO'}",
            ]
        else:
            lines.append("TODAY'S CANDIDATE: NOT ON LIST")

        total_signals = sum(r[1] for r in sc_rows)
        best_wr6      = max((r[5] or 0) for r in sc_rows)
        health_pass   = bool(fund[4]) if fund else False
        insider_flag  = bool(ins[3])  if ins  else False
        doc_text      = "\n".join(lines)

        return {
            "id":   f"stock_{ticker}",
            "text": doc_text,
            "metadata": {
                "ticker":        ticker,
                "stock_name":    ticker,
                "sector":        "",
                "type":          "stock_profile",
                "total_signals": total_signals,
                "best_wr6":      float(best_wr6),
                "health_pass":   health_pass,
                "insider_flag":  insider_flag,
                "is_candidate":  cand is not None,
                "document":      doc_text,
            },
        }

    except Exception as exc:
        print(f"  WARN {ticker}: {exc}")
        return None


def build_market_overview(con: duckdb.DuckDBPyConnection) -> dict | None:
    try:
        total = con.execute(
            "SELECT COUNT(DISTINCT Ticker) FROM mart_firm_scorecard"
        ).fetchone()[0]

        top_firms = con.execute("""
            SELECT Firm, COUNT(DISTINCT Ticker) AS n, AVG(win_rate_6m) AS wr6
            FROM mart_firm_scorecard
            GROUP BY Firm ORDER BY n DESC LIMIT 10
        """).fetchall()

        candidates = 0
        try:
            candidates = con.execute(
                "SELECT COUNT(*) FROM mart_daily_candidates"
            ).fetchone()[0]
        except Exception:
            pass

        health  = con.execute(
            "SELECT COUNT(*) FROM mart_fundamental_health WHERE passes_health_screen"
        ).fetchone()[0]
        insider = con.execute(
            "SELECT COUNT(*) FROM mart_insider_summary WHERE insider_buy_flag"
        ).fetchone()[0]

        lines = [
            "MARKET OVERVIEW",
            f"  Tracked tickers: {total}",
            f"  Today's candidates: {candidates}",
            f"  Passing health screen: {health}",
            f"  Active insider buying: {insider}",
            "",
            "TOP ANALYST FIRMS:",
        ] + [
            f"  {firm}: {n} tickers, avg 6M WR {_safe(wr, '{:.0f}%')}"
            for firm, n, wr in top_firms
        ]

        text = "\n".join(lines)
        return {
            "id":   "market_overview",
            "text": text,
            "metadata": {
                "ticker":        "",
                "stock_name":    "Market Overview",
                "sector":        "all",
                "type":          "market_overview",
                "total_signals": total,
                "best_wr6":      0.0,
                "health_pass":   False,
                "insider_flag":  False,
                "is_candidate":  False,
                "document":      text,
            },
        }
    except Exception as exc:
        print(f"  WARN market overview: {exc}")
        return None


def build_documents(con: duckdb.DuckDBPyConnection, mart_paths: dict[str, str]) -> list[dict]:
    for mart, path in mart_paths.items():
        escaped = path.replace("\\", "/")
        con.execute(
            f"CREATE OR REPLACE VIEW {mart} AS SELECT * FROM read_parquet('{escaped}')"
        )

    tickers = [r[0] for r in con.execute(
        "SELECT DISTINCT Ticker FROM mart_firm_scorecard ORDER BY Ticker"
    ).fetchall()]
    print(f"  {len(tickers)} tickers in mart_firm_scorecard")

    docs = [d for t in tickers if (d := build_ticker_doc(con, t)) is not None]
    print(f"  {len(docs)} stock documents built")

    overview = build_market_overview(con)
    if overview:
        docs.append(overview)
        print("  market overview document built")

    return docs


# ── Pinecone upsert ───────────────────────────────────────────────────────────

def ensure_index(pc: Pinecone) -> None:
    existing = [idx.name for idx in pc.list_indexes()]
    if PINECONE_INDEX not in existing:
        print(f"  Creating index '{PINECONE_INDEX}' (dim={EMBED_DIM}, cosine)...")
        pc.create_index(
            name=PINECONE_INDEX,
            dimension=EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        print("  Done.")


def upsert(pc: Pinecone, docs: list[dict], model: SentenceTransformer) -> None:
    index  = pc.Index(PINECONE_INDEX)
    texts  = [f"search_document: {d['text']}" for d in docs]
    total  = len(texts)

    print(f"\nEmbedding {total} documents...")
    all_vecs = []
    for i in range(0, total, BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        vecs  = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        all_vecs.extend(vecs)
        print(f"  [{min(i + BATCH_SIZE, total)}/{total}]", end="\r")
    print()

    records = [
        {"id": d["id"], "values": all_vecs[i].tolist(), "metadata": d["metadata"]}
        for i, d in enumerate(docs)
    ]

    for i in range(0, len(records), 100):
        index.upsert(vectors=records[i : i + 100])

    stats = index.describe_index_stats()
    print(f"  Upserted {len(records)} vectors — index total: {stats.total_vector_count}.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 55)
    print("  Meta-Analyst — Stock Embedding Pipeline")
    print("=" * 55)
    print(f"  Bucket : {S3_BUCKET}")
    print(f"  Model  : {EMBED_MODEL}")
    print(f"  Index  : {PINECONE_INDEX}\n")

    s3  = boto3.client("s3")
    con = duckdb.connect()
    pc  = Pinecone(api_key=PINECONE_KEY)

    ensure_index(pc)

    print("Loading embedding model (downloads once, then cached)...")
    model = SentenceTransformer(EMBED_MODEL, trust_remote_code=True)
    print("Model ready.\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        print("Downloading mart Parquets from S3...")
        mart_paths = download_marts(s3, tmpdir)

        if "mart_firm_scorecard" not in mart_paths:
            print("ERROR: mart_firm_scorecard missing. Run compute_marts.py first.")
            sys.exit(1)

        print("\nBuilding documents...")
        docs = build_documents(con, mart_paths)

    upsert(pc, docs, model)
    print("\nDone — Pinecone index ready for RAG queries.")


if __name__ == "__main__":
    main()
