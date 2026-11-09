"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Activity, Bug, LayoutDashboard, ListChecks, LogOut, Network, Settings2, UsersRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const baseNavigation = [
  { href: "/dashboard", label: "仪表盘", icon: LayoutDashboard },
  { href: "/current-task", label: "当前任务", icon: ListChecks },
  { href: "/accounts", label: "账号", icon: UsersRound },
  { href: "/logs", label: "日志", icon: Activity },
  { href: "/proxies", label: "代理", icon: Network },
  { href: "/settings", label: "配置", icon: Settings2 },
];
const titles: Record<string, string> = {
  "/dashboard": "仪表盘", "/current-task": "当前任务", "/accounts": "账号",
  "/logs": "日志", "/proxies": "代理", "/settings": "配置", "/mocker": "Mocker 测试",
};

export function ConsoleShell({ children, mockerEnabled = false }: { children: React.ReactNode; mockerEnabled?: boolean }) {
  const pathname = usePathname();
  const router = useRouter();
  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
  }
  return <div className="console-layout">
    <aside className="sidebar">
      <Link className="brand" href="/dashboard">
        <span className="brand-mark"><Activity aria-hidden="true" /></span>
        <span><strong>MailOps</strong><small>账号检查</small></span>
      </Link>
      <p className="nav-caption">工作区</p>
      <nav aria-label="主导航" className="nav-list">
        {[...baseNavigation, ...(mockerEnabled ? [{ href: "/mocker", label: "Mocker 测试", icon: Bug }] : [])].map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return <Link key={href} href={href} aria-current={active ? "page" : undefined}
            className={cn("nav-link", active && "nav-link-active")}>
            <Icon aria-hidden="true" /><span>{label}</span>
          </Link>;
        })}
      </nav>
      <div className="sidebar-bottom">
        <span className="server-indicator"><i />单用户工作区</span>
        <Button variant="ghost" className="logout-button" onClick={logout}>
          <LogOut data-icon="inline-start" />退出登录
        </Button>
      </div>
    </aside>
    <main className="main-area">
      <header className="topbar"><div><span className="breadcrumb">MailOps /</span>
        <strong>{titles[pathname] ?? "账号检查"}</strong></div>
        <span className="topbar-status"><i />本地服务</span>
      </header>
      <div className="page-content">{children}</div>
    </main>
  </div>;
}
