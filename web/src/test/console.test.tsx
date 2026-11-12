import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConsoleShell } from "@/components/console-shell";
import { ConsoleView, ResultLists } from "@/components/console-view";
import { MockEventSource } from "./setup";

function json(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json" },
  }));
}

afterEach(() => vi.unstubAllGlobals());

describe("控制台路由与交互", () => {
  it("显示六个入口并标出当前路由", () => {
    render(<ConsoleShell><div>内容</div></ConsoleShell>);
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(navigation.querySelectorAll("a")).toHaveLength(6);
    expect(navigation.querySelector('a[href="/proxies"]')).toHaveTextContent("代理");
    const activeLink = navigation.querySelector('a[href="/accounts"]');
    expect(activeLink).toHaveAttribute("aria-current", "page");
  });

  it("仅在服务端启用 Mocker 时显示导航入口", () => {
    const view = render(<ConsoleShell><div>内容</div></ConsoleShell>);
    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(navigation.querySelector('a[href="/mocker"]')).not.toBeInTheDocument();
    view.rerender(<ConsoleShell mockerEnabled><div>内容</div></ConsoleShell>);
    expect(navigation.querySelector('a[href="/mocker"]')).toHaveTextContent("Mocker 测试");
  });

  it("配置随机故障参数并保存到运行时 API", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        return json({ ...body, scenarios: { success: "正常" }, available_operations: ["login", "imap_select"] });
      }
      return json({ mode: "scenario", scenario: "success", operations: ["login"], probability: 0.1,
        scenarios: { success: "正常" }, available_operations: ["login", "imap_select"] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="mocker" />);
    fireEvent.change(await screen.findByLabelText("模式"), { target: { value: "random" } });
    fireEvent.change(screen.getByLabelText("随机 seed"), { target: { value: "seed-42" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "imap_select" }));
    fireEvent.click(screen.getByRole("button", { name: "保存 Mocker 配置" }));
    expect(await screen.findByText(/新任务会使用该配置/)).toBeInTheDocument();
    const request = fetchMock.mock.calls.find((call) => call[1]?.method === "PUT");
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({
      mode: "random", seed: "seed-42", operations: ["login", "imap_select"], probability: 0.1,
    });
  });

  it("在任务详情显示固定故障计划并调用服务端重放", async () => {
    const plan = { version: 1, mode: "random", seed: "seed-42", operations: ["imap_select"], probability: 1,
      faults: [{ email: "good@example.test", operation: "imap_select", sequence: 2, fault: "503" }] };
    const detail = { id: "job-1", status: "completed", created_at: "now", completed_count: 1,
      total_count: 1, success_count: 0, failure_count: 1, results: [], options: { mocker_plan: plan } };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/accounts?status=pending") return json({ items: [] });
      if (url === "/api/jobs") return json({ items: [detail] });
      if (url === "/api/jobs/job-1" && !init?.method) return json(detail);
      if (url === "/api/jobs/job-1/replay") return json({ id: "job-2", status: "queued" }, 202);
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="current-task" />);
    expect(await screen.findByText(/seed seed-42/)).toBeInTheDocument();
    expect(screen.getByText(/"sequence": 2/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "按原计划重放" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/jobs/job-1/replay", expect.objectContaining({ method: "POST" })));
  });

  it("展示成功和失败结果分组", () => {
    render(<ResultLists results={[
      { email: "good@example.test", status: "ready", result: { ok: true } },
      { email: "bad@example.test", status: "login_failed", result: { ok: false, error: "login_failed" } },
    ]} />);
    expect(screen.getByText("good@example.test")).toBeInTheDocument();
    expect(screen.getByText("bad@example.test")).toBeInTheDocument();
    expect(screen.getByText("login_failed")).toBeInTheDocument();
  });

  it("显示导入行错误并保留已导入数量", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/api/accounts?status=successful")) return json({ items: [] });
      if (init?.method === "POST") return json({ imported: 1, errors: [{ line: 2, error: "account_format" }] });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="accounts" />);
    const textarea = await screen.findByRole("textbox", { name: "账号凭据" });
    fireEvent.change(textarea, { target: { value: "one@example.test----pw\ninvalid" } });
    fireEvent.click(screen.getByRole("button", { name: "导入记录" }));
    expect(await screen.findByRole("status")).toHaveTextContent("已导入 1 条；1 行格式无效");
  });
  it("点击复选项标签可切换整行选择", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => String(input).includes("status=pending")
      ? json({ items: [{ id: 9, email: "label@example.test", status: "pending" }] })
      : json({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="current-task" />);
    const checkbox = await screen.findByRole("checkbox", { name: /label@example.test/ });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(screen.getByText("label@example.test"));
    expect(checkbox).toBeChecked();
  });

  it("未确认清空操作时阻止当前任务提交", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return json({ detail: "unexpected_post" }, 422);
      const url = String(input);
      if (url.includes("status=pending")) return json({ items: [{ id: 7, email: "one@example.test", status: "pending" }] });
      if (url === "/api/jobs") return json({ items: [] });
      return json({ id: "job-1", status: "queued" });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="current-task" />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /one@example.test/ }));
    expect(screen.queryByLabelText("代理列表")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请确认清空邮箱文件夹");
    await waitFor(() => expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false));
  });

  it("仪表盘提供加载、空白与实际统计状态", async () => {
    let respond!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { respond = resolve; })));
    const view = render(<ConsoleView page="dashboard" />);
    expect(screen.getByRole("status", { name: "正在读取数据" })).toBeInTheDocument();
    await act(async () => respond(await json({ successful_count: 0, failed_attempt_count: 0, latest_job: null, active_job: null })));
    expect(screen.getByText("暂无任务")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "开始任务" })).toHaveAttribute("href", "/current-task");
    view.unmount();
    vi.stubGlobal("fetch", vi.fn(() => json({ successful_count: 12, failed_attempt_count: 3,
      latest_job: { id: "history-1", status: "completed", completed_count: 15, total_count: 15 }, active_job: null })));
    render(<ConsoleView page="dashboard" />);
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.getByText("15/15")).toBeInTheDocument();
  });

  it("读取失败可重试，账号列表根据状态分别查询", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => String(input).includes("status=failed")
      ? json({ items: [{ id: 2, email: "bad@example.test", status: "failed", error: "bad_credentials", job_id: "job-2" }] })
      : json({ detail: "service_unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="accounts" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("service_unavailable");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("tab", { name: "失败尝试" }));
    expect(await screen.findByText("bad@example.test")).toBeInTheDocument();
    expect(screen.getByText("bad_credentials")).toBeInTheDocument();
  });

  it("点击回显按钮读取并显示已保存的 IPinfo 密钥", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => String(input) === "/api/settings/ipinfo-key"
      ? json({ ipinfo_key: "saved-ipinfo-secret" })
      : json({ ipinfo_key_configured: true }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="settings" />);
    const input = await screen.findByLabelText("IPinfo API 密钥");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示 IPinfo API 密钥" }));
    expect(await screen.findByDisplayValue("saved-ipinfo-secret")).toBeInTheDocument();
    expect(input).toHaveAttribute("type", "text");
    fireEvent.click(screen.getByRole("button", { name: "隐藏 IPinfo API 密钥" }));
    expect(input).toHaveAttribute("type", "password");
    expect(fetchMock).toHaveBeenCalledWith("/api/settings/ipinfo-key", expect.anything());
  });

  it("代理页面保存全局代理并只显示状态", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/proxies" && init?.method === "PUT") return json({ ok: true });
      return json({ configured: true, count: 2 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="proxies" />);
    const textarea = await screen.findByLabelText("代理列表");
    fireEvent.change(textarea, { target: { value: "http://user:proxy-secret@127.0.0.1:8080" } });
    fireEvent.click(screen.getByRole("button", { name: "保存全局代理" }));
    expect(await screen.findByText(/全局代理已更新/)).toBeInTheDocument();
    expect(screen.getByText(/已配置 2 条代理/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/proxies", expect.objectContaining({ method: "PUT" }));
    expect(screen.queryByDisplayValue(/proxy-secret/)).not.toBeInTheDocument();
  });

  it("任务选项顺序明确且可选修改只需启用对应开关", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return json({ id: "created-job" }, 202);
      if (String(input).includes("status=pending")) return json({ items: [{ id: 8, email: "selected@example.test" }] });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="current-task" />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /selected@example.test/ }));
    fireEvent.change(screen.getByLabelText("并发数"), { target: { value: "2" } });

    const verbose = screen.getByRole("checkbox", { name: "详细进度日志" });
    const clear = screen.getByRole("checkbox", { name: /我确认清空/ });
    const remove = screen.getByRole("checkbox", { name: /删除邮箱别名/ });
    const change = screen.getByRole("checkbox", { name: /自动修改密码/ });
    expect(verbose.compareDocumentPosition(clear) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(clear.compareDocumentPosition(remove) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(remove.compareDocumentPosition(change) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(remove).toHaveAccessibleName(/会删除所选账号的别名/);
    expect(change).toHaveAccessibleName(/会更改所选账号密码，并留存候选密码/);

    fireEvent.click(remove);
    fireEvent.click(change);
    fireEvent.click(screen.getByRole("button", { name: "开始任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请确认清空邮箱文件夹");
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(0);

    fireEvent.click(clear);
    fireEvent.click(screen.getByRole("button", { name: "开始任务" }));
    expect(await screen.findByText(/已加入队列/)).toBeInTheDocument();
    const request = fetchMock.mock.calls.find((call) => call[1]?.method === "POST")!;
    const body = JSON.parse(String(request[1]?.body));
    expect(body).toMatchObject({ account_ids: [8], workers: 2, verbose: false,
      remove_aliases: true, change_password: true, confirm_mailbox_cleanup: true });
    expect(body).not.toHaveProperty("confirm_remove_aliases");
    expect(body).not.toHaveProperty("confirm_change_password");
    expect(body).not.toHaveProperty("proxies");
  });

  it("实时日志保留原始值、去重并在结束时关闭连接", async () => {
    vi.stubGlobal("fetch", vi.fn(() => json({ items: [{ id: "log-job", status: "running" }] })));
    render(<ConsoleView page="logs" />);
    await screen.findByRole("option", { name: /log-job/ });
    const source = MockEventSource.instances.at(-1)!;
    expect(source.url).toBe("/api/jobs/log-job/events");
    act(() => { source.receive(1, { password: "RawPassword!" }); source.receive(1, { password: "RawPassword!" }); source.onerror?.(); });
    expect(screen.getAllByText(/RawPassword!/)).toHaveLength(1);
    expect(screen.getByText("连接中断，正在重连")).toBeInTheDocument();
    act(() => { source.receive(2, { stage: "结束" }); source.complete(); });
    expect(screen.getByText("任务已结束")).toBeInTheDocument();
    expect(source.readyState).toBe(MockEventSource.CLOSED);
  });

  it("按邮箱查询候选并显式确认后刷新状态", async () => {
    let confirmed = false;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") { confirmed = true; return json({ ok: true }); }
      if (String(input).includes("password-candidates")) return json({ items: [{ id: 9, email: "candidate@example.test",
        password: "CandidatePassword!", status: confirmed ? "confirmed" : "uncertain", job_id: "job-9" }] });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleView page="accounts" />);
    fireEvent.change(screen.getByLabelText("邮箱地址"), { target: { value: "candidate@example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(await screen.findByText("CandidatePassword!")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "已确认生效" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "已确认生效" })).not.toBeInTheDocument());
    expect(fetchMock.mock.calls.find((call) => call[1]?.method === "POST")?.[1]?.body).toBe('{"applied":true}');
  });
});
