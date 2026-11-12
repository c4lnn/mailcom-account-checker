import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LoginPage from "@/app/login/page";

const router = vi.hoisted(() => ({ replace: vi.fn(), refresh: vi.fn() }));

vi.mock("next/navigation", () => ({ useRouter: () => router }));

function json(status: number) {
  return Promise.resolve(new Response(null, { status }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("登录页", () => {
  it("一次成功登录仅导航到仪表盘，不刷新路由", async () => {
    const fetchMock = vi.fn(() => json(200));
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("控制台密码"), { target: { value: "valid-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledTimes(1));
    expect(router.replace).toHaveBeenCalledWith("/dashboard");
    expect(router.refresh).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("拒绝无效密码并留在登录页显示错误", async () => {
    const fetchMock = vi.fn(() => json(401));
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("控制台密码"), { target: { value: "invalid-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("密码不正确或服务暂不可用")).toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
    expect(router.refresh).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
