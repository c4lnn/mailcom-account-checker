// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { GET, POST } from "@/app/api/[...path]/route";

afterEach(() => vi.unstubAllGlobals());

describe("同源 API 路由", () => {
  it("阻止跨源和无效 Origin 的写入", async () => {
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    for (const origin of ["https://attacker.invalid", "null", ""]) {
      const request = new NextRequest("https://console.example.test/api/jobs", { method: "POST", headers: { origin } });
      expect((await POST(request, { params: Promise.resolve({ path: ["jobs"] }) })).status).toBe(403);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("登录时转发请求并保留 HttpOnly 会话 cookie", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response('{"ok":true}', { headers: {
      "content-type": "application/json", "set-cookie": "session=signed; HttpOnly; Secure; SameSite=Lax; Path=/",
    } })));
    vi.stubGlobal("fetch", fetchMock);
    const response = await POST(new NextRequest("https://console.example.test/api/auth/login", {
      method: "POST", headers: { origin: "https://console.example.test", "content-type": "application/json" }, body: '{"password":"synthetic"}',
    }), { params: Promise.resolve({ path: ["auth", "login"] }) });
    expect(response.headers.get("set-cookie")).toContain("HttpOnly");
    expect(fetchMock.mock.calls[0]).toBeDefined();
    expect(await response.json()).toEqual({ ok: true });
  });

  it("支持直连 HTTP 以及 HTTP/HTTPS 反向代理的公开 Host", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(Response.json({ ok: true }))));
    for (const headers of [
      { host: "127.0.0.1:3000", origin: "http://127.0.0.1:3000", "x-forwarded-proto": "http" },
      { host: "console.example.test", origin: "http://console.example.test", "x-forwarded-proto": "http" },
      { host: "console.example.test", origin: "https://console.example.test", "x-forwarded-proto": "https" },
    ]) {
      const response = await POST(new NextRequest("http://localhost:3000/api/auth/login", {
        method: "POST", headers,
      }), { params: Promise.resolve({ path: ["auth", "login"] }) });
      expect(response.status).toBe(200);
    }
  });

  it("SSE 转发 cookie 和重连游标并直接保留事件流", async () => {
    const fetchMock = vi.fn((_url: URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(headers.get("cookie")).toBe("session=signed");
      expect(headers.get("last-event-id")).toBe("40");
      return Promise.resolve(new Response('id: 41\ndata: {"password":"raw"}\n\n', { headers: {
        "content-type": "text/event-stream", "x-accel-buffering": "no",
      } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const response = await GET(new NextRequest("https://console.example.test/api/jobs/job-1/events", {
      headers: { cookie: "session=signed", "last-event-id": "40" },
    }), { params: Promise.resolve({ path: ["jobs", "job-1", "events"] }) });
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    expect(await response.text()).toContain('"password":"raw"');
  });

  it("保留后端未认证响应并报告上游连接失败", async () => {
    const context = { params: Promise.resolve({ path: ["settings"] }) };
    const request = new NextRequest("https://console.example.test/api/settings");
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(Response.json({ detail: "unauthorized" }, { status: 401 }))));
    expect((await GET(request, context)).status).toBe(401);
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    expect((await GET(request, context)).status).toBe(502);
  });
});
