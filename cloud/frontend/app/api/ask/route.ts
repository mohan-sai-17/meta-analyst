import { NextRequest, NextResponse } from "next/server";

const RAG_URL = process.env.NEXT_PUBLIC_RAG_URL ?? "http://localhost:8001";

export async function POST(req: NextRequest) {
  const body = await req.json();
  try {
    const resp = await fetch(`${RAG_URL}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { answer: "RAG server unavailable. Start it with: uvicorn chatbot.rag_server:app --port 8001", sources: [], search_used: false },
      { status: 503 }
    );
  }
}
