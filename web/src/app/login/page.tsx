"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { KeyRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!response.ok) { setError("密码不正确或服务暂不可用"); return; }
      router.replace("/dashboard");
    } catch { setError("无法连接服务，请稍后重试"); }
    finally { setBusy(false); }
  }

  return <main className="login-page">
    <Card className="w-full max-w-sm"><CardHeader>
      <div className="brand-mark"><KeyRound aria-hidden="true" /></div>
      <CardTitle>账号检查控制台</CardTitle><CardDescription>登录单用户工作区</CardDescription>
    </CardHeader><CardContent><form onSubmit={submit}><FieldGroup>
      <Field data-invalid={!!error}><FieldLabel htmlFor="password">控制台密码</FieldLabel>
      <Input id="password" autoComplete="current-password" type="password" required aria-invalid={!!error}
        value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      <Button type="submit" className="w-full" disabled={busy}>{busy ? "正在验证…" : "登录"}</Button>
    </FieldGroup></form></CardContent></Card>
  </main>;
}
