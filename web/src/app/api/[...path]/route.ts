import type { NextRequest } from "next/server";

const API_ORIGIN = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
type RouteContext = { params: Promise<{ path: string[] }> };

async function forward(request: NextRequest, context: RouteContext) {
  if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
    const origin = request.headers.get("origin");
    let sameOrigin = false;
    try {
      // NextURL 将 loopback 地址归一化为 localhost；反向代理也会使用内部 URL。
      const host = request.headers.get("host") ?? request.nextUrl.host;
      const protocol = request.headers.get("x-forwarded-proto") ?? request.nextUrl.protocol.replace(":", "");
      sameOrigin = !!origin && new URL(origin).origin === new URL(`${protocol}://${host}`).origin;
    } catch {}
    if (!sameOrigin) {
      return Response.json({ detail: "cross_origin_request" }, { status: 403 });
    }
  }
  const { path } = await context.params;
  const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, API_ORIGIN);
  const headers = new Headers();
  for (const name of ["accept", "content-type", "cookie", "origin", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("host", new URL(API_ORIGIN).host);
  if (headers.has("origin")) headers.set("origin", API_ORIGIN);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let upstream: Response;
  try { upstream = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    cache: "no-store",
    redirect: "manual",
    signal: request.signal,
  }); } catch {
    return Response.json({ detail: "api_unavailable" }, { status: 502 });
  }
  const responseHeaders = new Headers();
  for (const name of ["content-type", "cache-control", "x-accel-buffering"]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  for (const cookie of upstream.headers.getSetCookie()) responseHeaders.append("set-cookie", cookie);
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
