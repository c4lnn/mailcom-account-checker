import { cookies } from "next/headers";
import { notFound } from "next/navigation";
import { ConsoleView } from "@/components/console-view";

export default async function MockerPage() {
  const session = (await cookies()).get("session")?.value;
  const response = await fetch(`${process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000"}/api/runtime-config`, {
    headers: session ? { cookie: `session=${session}` } : {}, cache: "no-store",
  });
  if (!response.ok || (await response.json()).mocker_enabled !== true) notFound();
  return <ConsoleView page="mocker" />;
}
