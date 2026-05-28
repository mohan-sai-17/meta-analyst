# Meta-Analyst

A quantitative stock analysis system that surfaces mispriced call options by combining analyst upgrade signals, historical seasonality, and Black-Scholes pricing. Runs as a fully automated cloud pipeline on AWS.

---

## How It Works — The Four-Filter Model

```
[1] Conflict Check   Is the analyst firm an underwriter of this company?
        ↓
[2] Seasonality      Is this historically a strong month for the stock?
        ↓
[3] Options Pricing  Is the call option underpriced vs Black-Scholes fair value?
        ↓
[4] Position Sizing  How many contracts at 2% fixed risk?
        ↓
     Trade Ticket
```

---

## Architecture

```
Bootstrap (one-time)                Daily Pipeline (GitHub Actions, 2pm UTC weekdays)
  cloud/pipeline/                     cloud/pipeline/
    bootstrap_stocks_s3.py  ─┐          handler.py --step ohlc
    bootstrap_ohlc_s3.py   ──┼──▶       handler.py --step ratings
    bootstrap_ratings_s3.py ─┘          handler.py --step sectors
    bootstrap_sectors_s3.py             handler.py --step fundamentals
                                        handler.py --step insider
S3 Data Lake                            handler.py --step marts
  raw/ohlc/                             handler.py --step validate
  raw/ratings/                          handler.py --step backtest
  raw/fundamentals/                     handler.py --step alerts
  raw/insider/
  mart/*.parquet              Lambda API (cloud/api/main.py)
                                FastAPI + DuckDB + httpfs → reads S3 Parquet
                                served via Mangum on Lambda Function URL

                              cloud/frontend/ (Cloudflare Pages)
                                Next.js → Lambda REST API

                              chatbot/ (RAG AI Assistant)
                                LangGraph agent + Pinecone + Groq
                                Answers natural language questions about stocks
```

---

## Pipeline — 9 Steps

Orchestrated by Step Functions, triggered by EventBridge Scheduler. All steps call the same Lambda function (`meta-analyst-pipeline`) with a different `step` payload via `handler.py`.

| Step | Script | What It Does |
|------|--------|-------------|
| `ohlc` | `update_ohlc_s3.py` | Reads watermarks (MAX date per ticker) from S3 via DuckDB, fetches only missing candles from yfinance (20 parallel workers), uploads new partition to `raw/ohlc/date=YYYY-MM-DD/`. |
| `ratings` | `update_ratings_s3.py` | Reads watermarks (MAX GradeDate per ticker), fetches full ratings from yfinance (10 workers), keeps only new rows, uploads to `raw/ratings/date=YYYY-MM-DD/`. |
| `sectors` | `bootstrap_sectors_s3.py` | Fetches sector classification from yfinance for each ticker. Idempotent — skips if already done. |
| `fundamentals` | `update_fundamentals_s3.py` | Fetches fundamentals (P/E, EV/EBITDA, debt ratios, etc.) from yfinance for all tickers. |
| `insider` | `update_insider_s3.py` | Downloads insider trading filings from SEC EDGAR form.idx. Rate-limited to 8 req/s. |
| `marts` | `compute_marts.py` | Downloads all raw Parquet from S3 to `/tmp`, runs DuckDB SQL from `sql/`, uploads rebuilt mart Parquet back to `mart/`. |
| `validate` | `validate_data.py` | Checks mart Parquet files for row count regressions and schema drift. Fails fast before backtest runs. |
| `backtest` | `backtest_v2.py` | Walk-forward backtest on mart signals. Writes results Parquet to S3. |
| `alerts` | `send_alerts.py` | Reads `mart_daily_candidates` from S3, publishes daily summary + urgent per-signal alerts via Amazon SNS. |

---

## S3 Data Lake Layout

```
s3://meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID/
  raw/
    stocks_list/
      bootstrap/stocks_list.parquet        ← Ticker, Stock_Name, Friends, Sector
    ohlc/
      bootstrap/ohlc_full.parquet          ← 10yr history
      date=YYYY-MM-DD/ohlc_update.parquet  ← daily increments
    ratings/
      bootstrap/ratings.parquet            ← full history
      date=YYYY-MM-DD/*.parquet            ← daily increments
    fundamentals/
      date=YYYY-MM-DD/fundamentals.parquet
    insider/
      date=YYYY-MM-DD/insider.parquet
  mart/
    mart_firm_scorecard.parquet
    mart_top_tier.parquet
    mart_price_targets.parquet
    mart_seasonality.parquet
    mart_recent_signals.parquet
    mart_sector_candidates.parquet
    mart_daily_candidates.parquet
    mart_fundamental_health.parquet
    mart_insider_summary.parquet
    backtest_results.parquet
```

---

## SQL Mart Definitions (`cloud/sql/`)

Executed by `compute_marts.py`. Each file produces one mart table.

| File | What It Produces |
|------|-----------------|
| `mart_firm_scorecard.sql` | Per-firm stats — signal count, avg return and win rate at 3M/6M horizons, per ticker. |
| `mart_top_tier.sql` | Filters scorecard to "Top Tier" firms: win_rate_6m > 50%, both returns > 0, signal_count ≥ 2. |
| `mart_price_targets.sql` | 3M and 6M price targets per ticker from Top Tier firm averages. |
| `mart_seasonality.sql` | Monthly win rate and avg return per ticker across all available history. |
| `mart_recent_signals.sql` | Top Tier signals from the last 90 days with current prices and estimated targets. |
| `mart_sector_candidates.sql` | Non-tech Oracle-green tickers grouped by sector (Sector Rotation page). |
| `mart_daily_candidates.sql` | Top Tier upgrades from the last 3 days with price targets, seasonality, historical vol, and bias flag. Powers Today's Buy Signals. |

---

## Lambda REST API (`cloud/api/`)

FastAPI deployed as a Lambda container image. Reads mart and raw Parquet from S3 via DuckDB + httpfs. Module-level DuckDB connection persists across warm invocations.

**Base URL:** `https://YOUR_LAMBDA_URL.lambda-url.us-east-1.on.aws`

| Endpoint | Returns |
|----------|---------|
| `GET /health` | `{"status": "ok"}` |
| `GET /tickers` | All tickers and company names |
| `GET /ohlc/{ticker}?days=N` | OHLC history (default 730 days, max 3650) |
| `GET /ratings/{ticker}` | All upgrade/initiation signals for a ticker |
| `GET /scorecard/{ticker}` | Per-firm scorecard (avg returns, win rates) |
| `GET /top-tier/{ticker}` | Top Tier firms only for a ticker |
| `GET /price-targets` | All Oracle price targets (bulk) |
| `GET /price-targets/{ticker}` | Oracle 3M/6M price targets for one ticker |
| `GET /seasonality/{ticker}?period=monthly` | Monthly win rate + avg return |
| `GET /seasonality/{ticker}?period=weekly` | Week-of-year (W1–W52) win rate + avg return |
| `GET /options/{ticker}?target=X&price=Y` | Best underpriced call via Black-Scholes |
| `GET /friends/{ticker}` | Underwriter relationships |
| `GET /signals` | Top Tier signals from last 90 days |
| `GET /candidates` | Pre-screened buy candidates: Top Tier upgrades last 3 days |
| `GET /sector-candidates` | Non-tech Oracle-green tickers by sector |
| `GET /fundamentals/{ticker}` | Fundamental health metrics |
| `GET /insider/{ticker}` | Insider transaction summary |
| `GET /scan-options` | Full S&P 500 Black-Scholes scan |

---

## Frontend — Next.js Dashboard (`cloud/frontend/`)

Deployed to Cloudflare Pages. Reads from the Lambda API.

| Page | Route | What It Shows |
|------|-------|--------------|
| Today's Buy Signals | `/` | Pre-screened candidates from `mart_daily_candidates` — green/red signal cards with price targets, seasonality, and bias flag. |
| Oracle | `/oracle` | All ~450 tickers in a searchable grid. Click any ticker → full OHLC chart, all analyst ratings, per-firm scorecard, price targets, and monthly + weekly seasonality heatmap. |
| Top Picks (Options) | `/top-picks` | Scans all oracle-covered tickers for underpriced calls via Black-Scholes. Adjustable ROI slider. |
| Sector Rotation | `/sector` | Sector pills + sortable table of Oracle-green non-tech candidates. |
| Scanner | `/scanner` | Last 90 days of Top Tier signals. By Stock (searchable) and By Firm views. |

```bash
cd cloud/frontend
npm install
# Copy frontend vars from root .env.example into cloud/frontend/.env.local
# (Next.js requires the file to live there — not tracked in git)
npm run dev   # http://localhost:3000
```

---

## AI Chatbot (`chatbot/`)

Enterprise RAG assistant for natural language stock queries. Built with LangGraph, Pinecone, and Groq.

```
User question
    ↓
Input guardrail (Groq — topic check, blocks off-topic)
    ↓
HyDE expansion  (Groq — generates hypothetical stock doc + rephrased query)
    ↓
3 parallel Pinecone queries (original + HyDE + rephrase)
    ↓
Reciprocal Rank Fusion  (merges 3 result lists into one ranked list)
    ↓
Cross-encoder re-ranking  (ms-marco-MiniLM-L-6-v2 — final precision sort)
    ↓
LangGraph agent  (Groq llama-3.3-70b — decides tool vs. direct answer)
    ↓
Structured output  (Pydantic StockSearchResponse + faithfulness check)
    ↓
FastAPI response  →  Next.js chat UI
```

### Enterprise Patterns

| Pattern | Implementation |
|---------|---------------|
| HyDE | Generates a hypothetical stock profile excerpt to close the query/document semantic gap |
| Multi-query | 3 search vectors per query (original + HyDE + rephrased) |
| RRF | Reciprocal Rank Fusion merges parallel result lists with `1/(rank + 60)` weighting |
| Cross-encoder rerank | `ms-marco-MiniLM-L-6-v2` re-scores top-N candidates for final ordering |
| LangGraph agent | Typed `AgentState`, conditional edges, tool dispatch loop |
| Structured output | `StockSearchResponse` / `StockVerdict` with Pydantic v2, separate analysis LLM call |
| Faithfulness check | Validates structured output only references tickers present in tool result |
| Input guardrail | LLM-based topic filter (blocks non-financial queries) |
| Conversation history | Stateless server — client sends last N turn pairs on every request |

### Tools

| Tool | When called |
|------|-------------|
| `search_stocks(query, top_n)` | New searches, additional results |
| `fetch_stock_details(ticker)` | Full analysis for a specific ticker |
| `find_similar_stocks(ticker, top_n)` | Similarity search from an anchor ticker's vector |

### Chatbot Setup

**Prerequisites:** Python 3.11+, Pinecone account (free tier), Groq account (free tier)

```bash
# 1. Install dependencies
pip install -r chatbot/requirements.txt

# 2. Configure environment
cp .env.example chatbot/.env
# Fill in PINECONE_API_KEY, PINECONE_INDEX, GROQ_API_KEY, AWS_*, S3_BUCKET

# 3. Build the Pinecone index (run once, then daily after pipeline)
python chatbot/embed_stocks.py

# 4. Start the RAG server
uvicorn chatbot.rag_server:app --port 8001 --reload

# 5. Add to cloud/frontend/.env.local
# NEXT_PUBLIC_RAG_URL=http://localhost:8001
```

### Pinecone Index

- **Index name:** `meta-analyst-stocks` (configurable via `PINECONE_INDEX`)
- **Dimension:** 768 (nomic-embed-text-v1.5)
- **Metric:** cosine
- **~450 vectors** — one per S&P 500 ticker + one market overview document
- **Metadata per vector:** ticker, sector, best_wr6, health_pass, insider_flag, is_candidate, document (full text)

### Embedding Model

`nomic-ai/nomic-embed-text-v1.5` — 768-dim, runs locally on CPU, ~300 MB. Requires the nomic task prefix (`search_query:` / `search_document:`). Downloaded once via sentence-transformers, cached automatically.

### LLM

`llama-3.3-70b-versatile` via Groq — fast inference, generous free tier (14,400 requests/day). No local GPU needed.

### Example Queries

- "Which stocks have strong insider buying and pass fundamentals?"
- "Show me today's best buy candidates across all sectors"
- "Find tech stocks with 6-month win rate above 65%"
- "Which S&P 500 stocks have the highest 6-month upside right now?"
- "Show me financials stocks with positive seasonality this month"
- "Tell me more about NVDA" → `fetch_stock_details`
- "Find more stocks like AAPL" → `find_similar_stocks`

---

## Infrastructure — Terraform (`cloud/terraform/`)

All AWS resources defined as code. State is local (`terraform.tfstate` — gitignored, not in S3).

| Resource | Details |
|----------|---------|
| S3 bucket | `meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID` — date-partitioned Parquet |
| ECR repo | `meta-analyst-api` — stores both API and pipeline Docker images |
| Lambda: API | `meta-analyst-api` — Docker `:latest`, 512 MB, 120s timeout, public Function URL |
| Lambda: Pipeline | `meta-analyst-pipeline` — Docker `:pipeline-latest`, 1024 MB, 900s timeout |
| Lambda: Billing | `meta-analyst-billing-check` — zip, 128 MB, monitors credit expiry |
| Step Functions | `meta-analyst-pipeline-prod` — 9 sequential steps, each with independent retry |
| EventBridge | Daily pipeline at `cron(0 14 ? * MON-FRI *)` UTC |
| SNS | `meta-analyst-pipeline-alerts-prod` — pipeline failures + daily buy signal emails |
| IAM | GitHub Actions OIDC role (keyless deploy) + Lambda execution role |

```bash
cd cloud/terraform
terraform init
terraform plan -var-file=environments/account_a.tfvars
terraform apply -var-file=environments/account_a.tfvars
```

---

## CI/CD — GitHub Actions (`.github/workflows/`)

| Workflow | Trigger | What It Does |
|----------|---------|-------------|
| `deploy.yml` | Push to `main` (cloud/pipeline or cloud/api changed) | Builds Docker image → pushes to ECR → updates Lambda function code |
| `daily_pipeline.yml` | Manual fallback | Triggers Step Functions execution via AWS CLI |
| `bootstrap.yml` | One-time | Pushes initial pipeline Docker image + billing_check.zip to S3 |
| `billing_check.yml` | Manual | Invokes billing-check Lambda directly |
| `terraform_plan.yml` | Manual | Runs `terraform plan` for review (apply is always local) |

**GitHub Secrets required:**

| Secret | Purpose |
|--------|---------|
| `AWS_ROLE_ARN` | GitHub Actions OIDC role (keyless auth) |
| `BUCKET_NAME` | S3 bucket name |
| `NEXT_PUBLIC_API_URL` | Lambda Function URL (injected at frontend build time) |
| `CLOUDFLARE_API_TOKEN` | Cloudflare Pages deploy token |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |

---

## Setup

**Prerequisites:** Python 3.11+, AWS CLI v2, Terraform, Docker

```bash
# 1. Provision AWS infrastructure
cd cloud/terraform
terraform init
terraform apply -var-file=environments/account_a.tfvars

# 2. Bootstrap data (run once — seeds S3 with historical data)
cd cloud/pipeline
python bootstrap_stocks_s3.py    # S&P 500 tickers + SEC underwriter data
python bootstrap_ohlc_s3.py     # 10yr OHLCV history (~10-15 min)
python bootstrap_ratings_s3.py  # Full analyst ratings history
python compute_marts.py          # Build initial mart Parquet files

# 3. Run Bootstrap GitHub Action (builds pipeline Docker image → ECR)
# GitHub Actions → Bootstrap → Run workflow

# 4. Run Deploy GitHub Action (builds API Docker image → ECR → Lambda)
# GitHub Actions → Deploy → Run workflow
```

After setup, GitHub Actions runs the full 9-step pipeline automatically every weekday at 2pm UTC.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Storage | S3 (Parquet, date-partitioned) |
| Compute | AWS Lambda (Docker container) |
| Orchestration | Step Functions + EventBridge Scheduler |
| API | FastAPI + DuckDB + Mangum |
| Frontend | Next.js → Cloudflare Pages |
| IaC | Terraform |
| Alerts | Amazon SNS |
| AI Chatbot | LangGraph + Pinecone + Groq |
| Data Sources | yfinance, SEC EDGAR, finviz |
