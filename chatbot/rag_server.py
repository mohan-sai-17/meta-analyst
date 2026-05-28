"""
rag_server.py
=============
Enterprise RAG API — LangGraph agent over Pinecone stock embeddings.

Enterprise patterns:
  - HyDE (Hypothetical Document Embeddings) + multi-query expansion
  - Reciprocal Rank Fusion (RRF) across parallel Pinecone searches
  - Cross-encoder re-ranking (ms-marco-MiniLM-L-6-v2)
  - LangGraph agent with typed state
  - Pydantic structured outputs + faithfulness validation
  - Input guardrails (topic check) + output guardrails (hallucination check)
  - Conversation history (multi-turn)

Tools:
  search_stocks       — HyDE + multi-query semantic search over ~450 stocks
  fetch_stock_details — full document from Pinecone for interview-style prep
  find_similar_stocks — vector similarity from an anchor ticker's embedding

Run:
    uvicorn chatbot.rag_server:app --port 8001 --reload

Env (chatbot/.env):
    PINECONE_API_KEY, PINECONE_INDEX, GROQ_API_KEY
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from pinecone import Pinecone
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer
from sentence_transformers.cross_encoder import CrossEncoder

from search_stocks import multi_search, search

load_dotenv(Path(__file__).parent / ".env")

PINECONE_KEY   = os.environ["PINECONE_API_KEY"]
PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "meta-analyst-stocks")
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]

EMBED_MODEL    = "nomic-ai/nomic-embed-text-v1.5"
RERANK_MODEL   = "cross-encoder/ms-marco-MiniLM-L-6-v2"
GROQ_MODEL     = "llama-3.3-70b-versatile"
MAX_STOCKS     = 12
HISTORY_PAIRS  = 4
RERANK_MULT    = 3

pc         = Pinecone(api_key=PINECONE_KEY)
pine_index = pc.Index(PINECONE_INDEX)

# Loaded once on first request
_session: dict = {
    "embed_model": None,
    "reranker":    None,
}


def _get_models():
    if _session["embed_model"] is None:
        print("Loading embedding model...", flush=True)
        _session["embed_model"] = SentenceTransformer(EMBED_MODEL, trust_remote_code=True)
        print("Loading re-ranker...", flush=True)
        _session["reranker"]    = CrossEncoder(RERANK_MODEL)
        print("Models ready.", flush=True)
    return _session["embed_model"], _session["reranker"]


# ── Structured output models ──────────────────────────────────────────────────

class StockVerdict(BaseModel):
    number:          int   = Field(description="Position in the listing (1, 2, 3...)")
    ticker:          str   = Field(description="Stock ticker symbol (e.g. AAPL)")
    sector:          str   = Field(description="Sector from the listing")
    signal_strength: Literal["strong", "good", "moderate", "weak"] = Field(
        description=(
            "Overall signal: strong (best_wr6>70%, health+insider PASS), "
            "good (wr6 60-70%), moderate (wr6 50-60%), weak (<50%)"
        )
    )
    verdict:     str       = Field(description="One direct sentence — the specific reason this stock matches the query")
    key_metrics: list[str] = Field(description="2-4 key facts: ['6M WR: 72%', 'Health: PASS', 'Insider: YES', '6M upside: +18%']")
    red_flags:   list[str] = Field(
        default_factory=list,
        description="Specific concerns only (weak fundamentals, no insider activity, low signal count). Empty if none."
    )


class StockSearchResponse(BaseModel):
    stocks:   list[StockVerdict] = Field(description="Verdict for EVERY stock in the tool result")
    summary:  str                = Field(description="2-3 sentence wrap-up: strongest match and recommended next step")
    has_more: bool               = Field(default=False)


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a sharp, data-driven equity research assistant for the Meta-Analyst trading platform.
You have access to analyst signal data, price targets, fundamental health, and insider activity for ~450 S&P 500 stocks.

You have three tools. Use the right one:

## search_stocks
CALL when:
- User's first message (always start with a search)
- User wants MORE or DIFFERENT stocks ("show more", "find different ones", "next batch")
- User wants a completely new filter or criteria

DO NOT call when:
- User asks to filter ALREADY-SHOWN stocks ("of these, which have insider buying?", "which ones are in tech?")
- User asks follow-up about a specific shown ticker ("tell me more about #3")
- User asks general questions answerable from context

Build a rich query: sector + signal type + criteria.
Example: "tech sector high win rate strong insider buying fundamentals pass"

## fetch_stock_details
CALL when user asks "tell me more about AAPL" or "what is the full analysis for #2".
Use the ticker shown in the listing.

## find_similar_stocks
CALL when user says "find more like AAPL" or "similar to #3".
Use the ticker shown in the listing.

## Direct answers (no tool)
Answer from already-shown stocks when:
- Filtering the current list ("of these which pass health screen")
- Comparing, ranking, or asking about shown stocks
- General questions about the market or methodology

## Result cap
search_stocks caps at {MAX_STOCKS} per call. Ask for more if the user wants to continue browsing.

## How to call a tool
Output ONLY this — no other text before or after:
<tool>{{"name": "search_stocks", "args": {{"query": "...", "top_n": 8}}}}</tool>
<tool>{{"name": "fetch_stock_details", "args": {{"ticker": "AAPL"}}}}</tool>
<tool>{{"name": "find_similar_stocks", "args": {{"ticker": "AAPL", "top_n": 5}}}}</tool>

## Output format
For stock listings: every stock gets signal_strength, verdict, and key_metrics.
For other questions: answer directly and concisely.
NEVER invent tickers or metrics not present in the tool result."""


# ── HyDE + query expansion ────────────────────────────────────────────────────

_hyde_llm = None

def _get_hyde_llm() -> ChatGroq:
    global _hyde_llm
    if _hyde_llm is None:
        _hyde_llm = ChatGroq(
            model=GROQ_MODEL, temperature=0.6, max_tokens=300, api_key=GROQ_API_KEY
        )
    return _hyde_llm


def hyde_expand(query: str) -> tuple[list[str], list[str]]:
    """
    HyDE + multi-query expansion.
    Returns (texts, prefixes) — typically 3 search vectors:
      - original query          (search_query prefix)
      - hypothetical stock doc  (search_document prefix — closes query/doc gap)
      - rephrased alternative   (search_query prefix)
    Falls back to original only on any error.
    """
    try:
        resp = _get_hyde_llm().invoke([HumanMessage(content=(
            f'For the stock search: "{query}"\n\n'
            "Generate:\n"
            "HYPOTHESIS: A 2-sentence stock profile excerpt that would perfectly match this search\n"
            "REPHRASE: A different phrasing of the same search intent\n\n"
            "Output only those two lines, no explanation."
        ))])
        texts, prefixes = [query], ["search_query"]
        for line in resp.content.splitlines():
            if line.startswith("HYPOTHESIS:"):
                hyp = line.replace("HYPOTHESIS:", "").strip()
                if hyp:
                    texts.append(hyp); prefixes.append("search_document")
            elif line.startswith("REPHRASE:"):
                rep = line.replace("REPHRASE:", "").strip()
                if rep and rep.lower() != query.lower():
                    texts.append(rep); prefixes.append("search_query")
        return texts, prefixes
    except Exception:
        return [query], ["search_query"]


# ── Guardrails ────────────────────────────────────────────────────────────────

_guard_llm = None

def _get_guard_llm() -> ChatGroq:
    global _guard_llm
    if _guard_llm is None:
        _guard_llm = ChatGroq(
            model=GROQ_MODEL, temperature=0, max_tokens=30, api_key=GROQ_API_KEY
        )
    return _guard_llm


def input_guardrail(user_input: str) -> tuple[bool, str]:
    """LLM-based topic check. Returns (is_allowed, rejection_reason)."""
    try:
        resp = _get_guard_llm().invoke([
            SystemMessage(content=(
                "You guard a stock analysis assistant. "
                'Reply ONLY with "OK" or "BLOCKED: <one short reason>".'
            )),
            HumanMessage(content=(
                f'Is this appropriate for a stock/equity research assistant? "{user_input}"'
            )),
        ])
        text = resp.content.strip()
        if text.upper().startswith("BLOCKED"):
            reason = text.split(":", 1)[1].strip() if ":" in text else \
                "This assistant handles stock analysis and equity research questions only."
            return False, reason
        return True, ""
    except Exception:
        return True, ""


def output_faithfulness_check(response: StockSearchResponse, tool_result: str) -> bool:
    """Ensure the structured response only references tickers present in tool output."""
    tool_tickers = set(re.findall(r"TICKER:\s*([A-Z]{1,5})", tool_result))
    response_tickers = {v.ticker for v in response.stocks}
    return response_tickers.issubset(tool_tickers) or not tool_tickers


# ── Re-ranking ────────────────────────────────────────────────────────────────

def rerank(query: str, stocks: list[dict], top_n: int, reranker: CrossEncoder) -> list[dict]:
    if len(stocks) <= top_n:
        return stocks
    docs = [
        f"{s['ticker']}. {s['sector']}. "
        f"6M WR: {s['best_wr6']:.0f}%. "
        f"Health: {'PASS' if s['health_pass'] else 'FAIL'}. "
        f"Insider: {'YES' if s['insider_flag'] else 'NO'}. "
        f"Signals: {s['total_signals']}."
        for s in stocks
    ]
    scores = reranker.predict([(query, doc) for doc in docs])
    ranked = sorted(zip(scores, stocks), key=lambda x: x[0], reverse=True)
    return [s for _, s in ranked[:top_n]]


# ── Stock formatting ──────────────────────────────────────────────────────────

def format_stocks_for_llm(stocks: list[dict]) -> str:
    lines = []
    for i, s in enumerate(stocks, 1):
        lines.append(
            f"TICKER: {s['ticker']} (id: stock_{s['ticker']})\n"
            f"  Sector: {s['sector'] or 'Unknown'}\n"
            f"  Best 6M WR: {s['best_wr6']:.0f}% | "
            f"Signals: {s['total_signals']} | "
            f"Health: {'PASS' if s['health_pass'] else 'FAIL'} | "
            f"Insider buy: {'YES' if s['insider_flag'] else 'NO'} | "
            f"Today's candidate: {'YES' if s['is_candidate'] else 'NO'}\n"
            f"  ---\n"
            f"{s['document'][:600]}"
        )
    return "\n\n".join(lines)


def format_structured_response(resp: StockSearchResponse) -> str:
    icons = {"strong": "[STRONG]", "good": "[GOOD]", "moderate": "[MODERATE]", "weak": "[WEAK]"}
    lines = []
    for s in resp.stocks:
        metrics   = " | ".join(s.key_metrics)
        flags_str = f"\n   Red flags: {'; '.join(s.red_flags)}" if s.red_flags else ""
        lines.append(
            f"{s.number}. {icons.get(s.signal_strength, '')} {s.ticker} ({s.sector})\n"
            f"   {s.verdict}\n"
            f"   {metrics}{flags_str}"
        )
    out = "\n\n".join(lines) + f"\n\n{resp.summary}"
    if resp.has_more:
        out += f"\n\nShowing {len(resp.stocks)} results — ask for more to continue."
    return out


# ── Tools ─────────────────────────────────────────────────────────────────────

def tool_search_stocks(args: dict, shown_ids: set, embed_model, reranker) -> str:
    query  = args.get("query", "")
    top_n  = min(int(args.get("top_n", 8)), MAX_STOCKS)

    query_texts, query_prefixes = hyde_expand(query)

    label = f"\n[search_stocks | HyDE+RRF | {len(query_texts)} queries | top {top_n}"
    if shown_ids: label += f" | skipping {len(shown_ids)} shown"
    print(label + "]", flush=True)
    for i, (t, p) in enumerate(zip(query_texts, query_prefixes)):
        tag = "original" if i == 0 else ("HyDE" if p == "search_document" else "rephrase")
        print(f"  [{tag}] {t[:80]}", flush=True)

    candidates = multi_search(
        query_texts, query_prefixes, embed_model,
        top_n=min(top_n * RERANK_MULT, 60),
        exclude_ids=shown_ids,
    )

    stocks = rerank(query, candidates, top_n, reranker)

    for s in stocks:
        shown_ids.add(s["id"])

    if not stocks:
        return "No matching stocks found after applying filters."

    return format_stocks_for_llm(stocks)


def tool_fetch_stock_details(args: dict) -> str:
    ticker = args.get("ticker", "").upper().strip()
    print(f"\n[fetch_stock_details: {ticker}]", flush=True)

    try:
        result = pine_index.fetch(ids=[f"stock_{ticker}"])
        vectors = result.get("vectors") or {}
        key = f"stock_{ticker}"
        if key not in vectors:
            return f"No data found for ticker '{ticker}'. Verify the ticker is in the S&P 500 coverage."
        doc = vectors[key]["metadata"].get("document", "")
        return f"Full analysis for {ticker}:\n\n{doc}" if doc else \
               f"Ticker {ticker} found but document is empty — re-run embed_stocks.py."
    except Exception as exc:
        return f"Could not fetch details for '{ticker}': {exc}"


def tool_find_similar_stocks(args: dict, shown_ids: set, reranker) -> str:
    ticker = args.get("ticker", "").upper().strip()
    top_n  = min(int(args.get("top_n", 5)), MAX_STOCKS)
    print(f"\n[find_similar_stocks: {ticker} | top {top_n}]", flush=True)

    try:
        result  = pine_index.fetch(ids=[f"stock_{ticker}"])
        vectors = result.get("vectors") or {}
        key     = f"stock_{ticker}"
        if key not in vectors:
            return f"No embedding found for '{ticker}'."

        anchor_vec = vectors[key]["values"]
        response   = pine_index.query(
            vector=anchor_vec,
            top_k=min(top_n * RERANK_MULT + 10, 100),
            include_metadata=True,
        )

        results = []
        for match in response.matches:
            if match.id == key or match.id in shown_ids:
                continue
            meta = match.metadata or {}
            if meta.get("type") == "market_overview":
                continue
            results.append({
                "id":            match.id,
                "score":         float(match.score),
                "ticker":        meta.get("ticker", ""),
                "sector":        meta.get("sector", ""),
                "best_wr6":      float(meta.get("best_wr6", 0)),
                "health_pass":   bool(meta.get("health_pass", False)),
                "insider_flag":  bool(meta.get("insider_flag", False)),
                "is_candidate":  bool(meta.get("is_candidate", False)),
                "total_signals": int(meta.get("total_signals", 0)),
                "document":      meta.get("document", ""),
            })
            if len(results) >= top_n:
                break

        for s in results:
            shown_ids.add(s["id"])

        if not results:
            return "No similar stocks found (all candidates already shown)."

        return f"Stocks similar to {ticker}:\n\n" + format_stocks_for_llm(results)

    except Exception as exc:
        return f"Could not find similar stocks for '{ticker}': {exc}"


# ── LangGraph agent ───────────────────────────────────────────────────────────

_TOOL_DISPATCH = {
    "search_stocks":       "search",
    "fetch_stock_details": "fetch",
    "find_similar_stocks": "similar",
}
_TOOL_CALL_RE = re.compile(r"<tool>(.*?)</tool>", re.DOTALL)

_ANALYSIS_SCHEMA = json.dumps(StockSearchResponse.model_json_schema(), indent=2)
_ANALYSIS_SYSTEM = SystemMessage(content=(
    "You are a stock analysis assistant. Respond ONLY with a single valid JSON object "
    "matching this schema exactly — no markdown, no code fences, no explanation:\n\n"
    + _ANALYSIS_SCHEMA
))

llm          = ChatGroq(model=GROQ_MODEL, temperature=0.3, max_tokens=3000, api_key=GROQ_API_KEY)
analysis_llm = ChatGroq(model=GROQ_MODEL, temperature=0,   max_tokens=3000, api_key=GROQ_API_KEY)


def invoke_analysis(messages: list) -> StockSearchResponse:
    system  = [m for m in messages if isinstance(m, SystemMessage)]
    non_sys = [m for m in messages if not isinstance(m, SystemMessage)]
    prompt  = [_ANALYSIS_SYSTEM] + system + non_sys
    resp    = analysis_llm.invoke(prompt)
    text    = resp.content.strip()
    text    = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return StockSearchResponse.model_validate(json.loads(text))


class AgentState(TypedDict):
    messages:    Annotated[list[BaseMessage], add_messages]
    shown_ids:   set[str]
    last_result: str


def call_model(state: AgentState) -> dict:
    embed_model, reranker = _get_models()
    shown_ids = state.get("shown_ids", set())

    system  = [m for m in state["messages"] if isinstance(m, SystemMessage)]
    rest    = [m for m in state["messages"] if not isinstance(m, SystemMessage)]
    trimmed = system + rest[-(HISTORY_PAIRS * 2):]

    # ── After tool result — generate response ─────────────────────────────────
    if rest and isinstance(rest[-1], ToolMessage):
        tool_content = rest[-1].content

        if "TICKER:" in tool_content:
            try:
                struct = invoke_analysis(trimmed)

                if not output_faithfulness_check(struct, tool_content):
                    print("\n[guardrail] faithfulness check failed — falling back", flush=True)
                    raise ValueError("faithfulness")

                output = format_structured_response(struct)
                return {
                    "messages": [AIMessage(content=output)],
                    "shown_ids": shown_ids,
                    "last_result": tool_content,
                }
            except Exception:
                pass  # fall through to direct LLM

        response = llm.invoke(trimmed)
        return {
            "messages": [response],
            "shown_ids": shown_ids,
            "last_result": tool_content,
        }

    # ── First call — tool decision or direct answer ───────────────────────────
    response = llm.invoke(trimmed)
    content  = response.content.strip()

    match = _TOOL_CALL_RE.search(content)
    if match:
        try:
            data   = json.loads(match.group(1))
            name   = data["name"]
            args   = data.get("args", {})
            print(f"\n[tool_call] {name}({args})", flush=True)

            if name == "search_stocks":
                result = tool_search_stocks(args, shown_ids, embed_model, reranker)
            elif name == "fetch_stock_details":
                result = tool_fetch_stock_details(args)
            elif name == "find_similar_stocks":
                result = tool_find_similar_stocks(args, shown_ids, reranker)
            else:
                result = f"Unknown tool: {name}"

            return {
                "messages": [
                    AIMessage(content=content),
                    ToolMessage(content=result, tool_call_id="1"),
                ],
                "shown_ids": shown_ids,
                "last_result": "",
            }
        except Exception as exc:
            print(f"\n[tool_call error] {exc}", flush=True)

    return {
        "messages":   [response],
        "shown_ids":  shown_ids,
        "last_result": "",
    }


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    return "agent" if isinstance(last, ToolMessage) else END


workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue, {"agent": "agent", END: END})
graph = workflow.compile()


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Meta-Analyst RAG API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class HistoryMessage(BaseModel):
    role:    str
    content: str


class AskRequest(BaseModel):
    question:     str
    history:      list[HistoryMessage] = []
    shown_tickers: list[str] = []


class SourceStock(BaseModel):
    ticker:     str
    stock_name: str
    doc_type:   str


class AskResponse(BaseModel):
    answer:      str
    sources:     list[SourceStock]
    search_used: bool


@app.get("/health")
def health():
    try:
        stats = pine_index.describe_index_stats()
        count = stats.total_vector_count
    except Exception:
        count = -1
    return {"status": "ok", "index": PINECONE_INDEX, "vector_count": count}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    if not req.question.strip():
        return AskResponse(answer="Please enter a question.", sources=[], search_used=False)

    allowed, reason = input_guardrail(req.question)
    if not allowed:
        return AskResponse(answer=reason, sources=[], search_used=False)

    # Rebuild LangGraph state from history
    messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT)]
    for msg in req.history[-(HISTORY_PAIRS * 2):]:
        if msg.role == "user":
            messages.append(HumanMessage(content=msg.content))
        elif msg.role == "assistant":
            messages.append(AIMessage(content=msg.content))
    messages.append(HumanMessage(content=req.question))

    shown_ids = {f"stock_{t}" for t in req.shown_tickers}

    try:
        state = graph.invoke({
            "messages":    messages,
            "shown_ids":   shown_ids,
            "last_result": "",
        })
    except Exception as exc:
        err = str(exc)
        if "rate_limit" in err.lower() or "429" in err:
            return AskResponse(
                answer="Rate limit reached — please wait a moment and try again.",
                sources=[], search_used=False,
            )
        return AskResponse(answer=f"Error: {err}", sources=[], search_used=False)

    # Extract final answer
    ai_messages = [m for m in state["messages"] if isinstance(m, AIMessage)]
    answer      = ai_messages[-1].content if ai_messages else "No response generated."

    # Extract sources from tickers mentioned in the answer
    tickers_mentioned = re.findall(r"\b([A-Z]{1,5})\b", answer)
    seen: set[str]    = set()
    sources: list[SourceStock] = []
    for t in tickers_mentioned:
        if t not in seen and len(t) >= 2:
            seen.add(t)
            sources.append(SourceStock(ticker=t, stock_name=t, doc_type="stock_profile"))

    search_used = any(isinstance(m, ToolMessage) for m in state["messages"])

    return AskResponse(answer=answer, sources=sources, search_used=search_used)
