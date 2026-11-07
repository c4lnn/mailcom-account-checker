import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { ConsoleShell } from "@/components/console-shell";

export const instant = false;

export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  const cookieStore = await cookies();
  const session = cookieStore.get("session")?.value;
  if (!session) redirect("/login");
  try {
    const response = await fetch(`${process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000"}/api/auth/session`, {
      headers: { cookie: `session=${session}` }, cache: "no-store",
    });
    if (!response.ok) redirect("/login");
  } catch (error) {
    if (error && typeof error === "object" && "digest" in error) throw error;
    redirect("/login");
  }
  return <ConsoleShell>{children}</ConsoleShell>;
}
