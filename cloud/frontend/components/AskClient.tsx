"use client";

import { useState, useRef, useEffect } from "react";
import { Send, Bot, User, Loader2, AlertCircle, BarChart2, TrendingUp } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface SourceStock {
  ticker: string;
  stock_name: string;
  doc_type: string;
}

interface Message {
  role: "user" | "assistant";
  text: string;
  sources?: SourceStock[];
  search_used?: boolean;
  error?: boolean;
}

interface AskResponse {
  answer: string;
  sources: SourceStock[];
  search_used: boolean;
}

// ── Example prompts ───────────────────────────────────────────────────────────

const EXAMPLES = [
  "Which stocks have strong insider buying and pass the health screen?",
  "Show me today's best buy candidates across all sectors",
  "Find tech stocks with 6-month win rate above 65%",
  "Which S&P 500 stocks have the highest upside potential right now?",
  "Show me financials sector stocks with positive seasonality this month",
  "Give me an overview of today's market — candidates and top analyst firms",
];

// ── Sub-components ─────────────────────────────────────────────────────────────

function SourcePill({ stock }: { stock: SourceStock }) {
  if (stock.doc_type === "market_overview") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs
                       bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300
                       border border-blue-200 dark:border-blue-800">
        <BarChart2 size={10} /> Market
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-mono
                     bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300
                     border border-emerald-200 dark:border-emerald-800">
      <TrendingUp size={10} /> {stock.ticker}
    </span>
  );
}

function SignalBadge({ text }: { text: string }) {
  const lower = text.toLowerCase();
  const config =
    lower.includes("strong") ? "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 border-emerald-300 dark:border-emerald-700" :
    lower.includes("good")   ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 border-blue-300 dark:border-blue-700" :
    lower.includes("moderate") ? "bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 border-amber-300 dark:border-amber-700" :
    "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 border-red-300 dark:border-red-700";

  return (
    <span className={`px-2 py-0.5 rounded text-xs font-semibold border ${config}`}>
      {text}
    </span>
  );
}

function FormattedAnswer({ text }: { text: string }) {
  // Detect stock listing lines like "1. [STRONG] AAPL (Technology)"
  const stockLineRe = /^(\d+)\.\s+(\[STRONG\]|\[GOOD\]|\[MODERATE\]|\[WEAK\])\s+([A-Z]{1,5})\s+\(([^)]+)\)/;
  const lines       = text.split("\n");

  return (
    <div className="space-y-1 text-sm leading-relaxed">
      {lines.map((line, i) => {
        const match = line.match(stockLineRe);
        if (match) {
          const [, num, badge, ticker, sector] = match;
          return (
            <div key={i} className="flex items-center gap-2 font-medium">
              <span className="text-muted-foreground w-5 text-right">{num}.</span>
              <SignalBadge text={badge.replace(/\[|\]/g, "")} />
              <span className="font-mono font-bold">{ticker}</span>
              <span className="text-muted-foreground text-xs">({sector})</span>
            </div>
          );
        }
        // Verdict / metric lines
        if (line.startsWith("   ") && line.trim()) {
          return (
            <p key={i} className="pl-7 text-muted-foreground text-xs">
              {line.trim()}
            </p>
          );
        }
        if (!line.trim()) return <div key={i} className="h-1" />;
        return <p key={i}>{line}</p>;
      })}
    </div>
  );
}

function ChatMessage({ msg }: { msg: Message }) {
  const isUser = msg.role === "user";

  return (
    <div className={`flex gap-3 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      <div className={`flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center
                       ${isUser
                         ? "bg-primary text-primary-foreground"
                         : "bg-muted border border-border"}`}>
        {isUser ? <User size={15} /> : <Bot size={15} />}
      </div>

      <div className={`max-w-[82%] space-y-2 ${isUser ? "items-end" : "items-start"} flex flex-col`}>
        <div className={`rounded-2xl px-4 py-3 text-sm
                         ${isUser
                           ? "bg-primary text-primary-foreground rounded-tr-sm"
                           : msg.error
                             ? "bg-destructive/10 border border-destructive/30 text-destructive rounded-tl-sm"
                             : "bg-muted border border-border rounded-tl-sm"}`}>
          {msg.error ? (
            <div className="flex items-center gap-2">
              <AlertCircle size={14} />
              <span>{msg.text}</span>
            </div>
          ) : isUser ? (
            <p>{msg.text}</p>
          ) : (
            <FormattedAnswer text={msg.text} />
          )}
        </div>

        {msg.sources && msg.sources.length > 0 && (
          <div className="flex flex-wrap gap-1 px-1">
            {msg.sources
              .filter((s, i, arr) => arr.findIndex(x => x.ticker === s.ticker) === i)
              .slice(0, 12)
              .map((s) => (
                <SourcePill key={s.ticker} stock={s} />
              ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function AskClient() {
  const [messages,  setMessages]  = useState<Message[]>([]);
  const [input,     setInput]     = useState("");
  const [loading,   setLoading]   = useState(false);
  const [shownTickers, setShownTickers] = useState<string[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef  = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function send(question: string) {
    if (!question.trim() || loading) return;

    const userMsg: Message = { role: "user", text: question };
    setMessages(prev => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    const history = messages.map(m => ({ role: m.role, content: m.text }));

    try {
      const resp = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history, shown_tickers: shownTickers }),
      });

      const data: AskResponse = await resp.json();

      const newTickers = data.sources
        .filter(s => s.doc_type === "stock_profile")
        .map(s => s.ticker)
        .filter(t => !shownTickers.includes(t));
      if (newTickers.length > 0) {
        setShownTickers(prev => [...prev, ...newTickers]);
      }

      setMessages(prev => [...prev, {
        role:        "assistant",
        text:        data.answer,
        sources:     data.sources,
        search_used: data.search_used,
      }]);
    } catch {
      setMessages(prev => [...prev, {
        role:  "assistant",
        text:  "Could not reach the RAG server. Make sure it is running on port 8001.",
        error: true,
      }]);
    } finally {
      setLoading(false);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }

  function handleKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  const isEmpty = messages.length === 0;

  return (
    <div className="flex flex-col h-[calc(100vh-4rem)] max-w-3xl mx-auto px-4">

      {/* Header */}
      <div className="py-4 border-b border-border flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-primary/10 border border-primary/20
                        flex items-center justify-center">
          <Bot size={18} className="text-primary" />
        </div>
        <div>
          <h1 className="font-semibold text-sm">Stock Research Assistant</h1>
          <p className="text-xs text-muted-foreground">
            Powered by Pinecone · Groq · LangGraph
          </p>
        </div>
        {shownTickers.length > 0 && (
          <button
            onClick={() => { setMessages([]); setShownTickers([]); }}
            className="ml-auto text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            New chat
          </button>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto py-6 space-y-6">
        {isEmpty ? (
          <div className="flex flex-col items-center justify-center h-full gap-6 text-center">
            <div>
              <h2 className="text-xl font-semibold mb-1">Ask about any S&P 500 stock</h2>
              <p className="text-sm text-muted-foreground max-w-md">
                Search across analyst signals, price targets, fundamentals, insider activity,
                and seasonality data for ~450 stocks.
              </p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-lg">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  onClick={() => send(ex)}
                  className="text-left text-xs px-3 py-2.5 rounded-xl border border-border
                             bg-muted/40 hover:bg-muted hover:border-primary/30
                             transition-colors leading-snug"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((msg, i) => <ChatMessage key={i} msg={msg} />)
        )}

        {loading && (
          <div className="flex gap-3">
            <div className="w-8 h-8 rounded-full bg-muted border border-border
                            flex items-center justify-center flex-shrink-0">
              <Bot size={15} />
            </div>
            <div className="bg-muted border border-border rounded-2xl rounded-tl-sm px-4 py-3">
              <Loader2 size={15} className="animate-spin text-muted-foreground" />
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="py-3 border-t border-border">
        <div className="flex gap-2 items-end bg-muted/40 border border-border
                        rounded-2xl px-3 py-2 focus-within:border-primary/50 transition-colors">
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Ask about stocks, signals, sectors, fundamentals..."
            rows={1}
            className="flex-1 bg-transparent resize-none text-sm outline-none
                       placeholder:text-muted-foreground max-h-32 leading-relaxed"
            style={{ scrollbarWidth: "none" }}
          />
          <button
            onClick={() => send(input)}
            disabled={!input.trim() || loading}
            className="flex-shrink-0 w-8 h-8 rounded-xl bg-primary text-primary-foreground
                       flex items-center justify-center disabled:opacity-40
                       hover:opacity-90 transition-opacity"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          </button>
        </div>
        <p className="text-center text-xs text-muted-foreground mt-1.5">
          Enter to send · Shift+Enter for new line
        </p>
      </div>
    </div>
  );
}
