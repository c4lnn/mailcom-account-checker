"use client";

import { FormEvent, ReactNode, useCallback, useEffect, useId, useState } from "react";
import Link from "next/link";
import { ArrowRight, RefreshCw, Upload, Play, Save, Search, Eye, EyeOff, Copy, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardAction } from "@/components/ui/card";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Empty, EmptyHeader, EmptyDescription } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldGroup, FieldLabel, FieldDescription, FieldSet, FieldLegend } from "@/components/ui/field";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

type Page = "dashboard" | "current-task" | "accounts" | "logs" | "proxies" | "settings" | "mocker";
type Account = { id: number; email: string; status: string; error?: string; job_id?: string; created_at?: string };
type Result = { id?: number; email: string; status: string; result: Record<string, unknown> };
type MockerPlan = { version: number; mode: string; scenario?: string; seed?: string; operations?: string[]; probability?: number; faults: { email: string; operation: string; sequence: number; fault: string }[] };
type Job = { id: string; status: string; created_at: string; completed_count: number; total_count: number; success_count: number; failure_count: number; results?: Result[]; options?: { mocker_config?: { mode?: string; scenario?: string; seed?: string }; mocker_plan?: MockerPlan } };
type Log = { id: number; payload: Record<string, unknown> };
type Settings = { ipinfo_key_configured: boolean };
type Candidate = { id: number; email: string; password: string; status: string; job_id: string; updated_at: string };

const labels: Record<string, string> = {
  queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", interrupted: "已中断",
  successful: "成功", pending: "待检查", prepared: "已预存", submitted: "已提交",
  uncertain: "待核实", confirmed: "已确认生效", unapplied: "未生效",
};
const errors: Record<string, string> = {
  unauthorized: "会话已失效，请重新登录", invalid_credentials: "登录密码不正确",
  proxy_file_empty: "请先在代理页面配置全局代理",
  job_already_running: "已有任务正在运行，请等待其完成", account_selection_invalid: "账号选择无效，请刷新后重试",
  candidate_not_uncertain: "该候选状态已变化，请刷新后查看", api_unavailable: "后端服务暂不可用，请稍后重试",
  mailbox_cleanup_confirmation_required: "请确认清空邮箱文件夹",
  alias_removal_confirmation_required: "请确认删除别名", password_change_confirmation_required: "请确认修改密码",
};

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init, cache: "no-store", headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : `请求失败 (${response.status})`;
    throw new Error(errors[detail] ?? detail);
  }
  return response.json() as Promise<T>;
}

function useLoad<T>(url: string | null) {
  const [state, setState] = useState<{ url: string | null; data: T | null; error: string }>({ url: null, data: null, error: "" });
  const reload = useCallback(async () => {
    if (!url) return;
    try { const data = await api<T>(url); setState({ url, data, error: "" }); }
    catch (cause) { setState({ url, data: null, error: cause instanceof Error ? cause.message : "读取失败" }); }
  }, [url]);
  useEffect(() => { let active = true;
    if (url) api<T>(url).then((data) => { if (active) setState({ url, data, error: "" }); })
      .catch((cause) => { if (active) setState({ url, data: null, error: cause instanceof Error ? cause.message : "读取失败" }); });
    return () => { active = false; };
  }, [url]);
  return { data: state.url === url ? state.data : null, error: state.url === url ? state.error : "", loading: !!url && state.url !== url, reload };
}

function PageTitle({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="page-title"><div><h1>{title}</h1><p>{description}</p></div>{action}</div>;
}
function Panel({ title, description, action, children }: { title: string; description?: string; action?: ReactNode; children: ReactNode }) {
  return <Card><CardHeader><CardTitle>{title}</CardTitle>{description && <CardDescription>{description}</CardDescription>}{action && <CardAction>{action}</CardAction>}</CardHeader><CardContent>{children}</CardContent></Card>;
}
function Loading() { return <div className="flex flex-col gap-3 p-4" role="status" aria-label="正在读取数据"><Skeleton className="h-5 w-40" /><Skeleton className="h-12 w-full" /></div>; }
function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return <Alert variant="destructive"><AlertTitle>暂时无法读取</AlertTitle><AlertDescription>{message}{retry && <Button type="button" variant="outline" size="sm" onClick={retry}><RefreshCw data-icon="inline-start" />重试</Button>}</AlertDescription></Alert>;
}
function EmptyState({ children }: { children: ReactNode }) { return <Empty><EmptyHeader><EmptyDescription>{children}</EmptyDescription></EmptyHeader></Empty>; }
function Notice({ message, error = false }: { message: string; error?: boolean }) {
  return message ? <Alert variant={error ? "destructive" : "default"} role={error ? "alert" : "status"}><AlertDescription>{message}</AlertDescription></Alert> : null;
}
function Status({ value }: { value: string }) {
  return <Badge variant={value === "failed" ? "destructive" : value === "completed" || value === "successful" || value === "confirmed" ? "default" : "secondary"}>{labels[value] ?? value}</Badge>;
}
function CheckField({ checked, onChange, children, disabled = false }: { checked: boolean; onChange: (value: boolean) => void; children: ReactNode; disabled?: boolean }) {
  const id = useId();
  return <Field orientation="horizontal" data-disabled={disabled}><Checkbox id={id} checked={checked} onCheckedChange={(value) => onChange(value)} disabled={disabled} /><FieldLabel htmlFor={id}>{children}</FieldLabel></Field>;
}

function Dashboard() {
  const state = useLoad<{ successful_count: number; failed_attempt_count: number; latest_job: Job | null; active_job: Job | null }>("/api/dashboard");
  if (state.loading) return <Loading />;
  if (state.error || !state.data) return <ErrorState message={state.error} retry={state.reload} />;
  const { data } = state;
  const job = data.active_job ?? data.latest_job;
  return <div className="flex flex-col gap-5">
    <PageTitle title="仪表盘" description="查看账号检查概况与最近一次任务。" action={<Button role="link" nativeButton={false} render={<Link href="/current-task" />}>开始任务<ArrowRight data-icon="inline-end" /></Button>} />
    <section className="metric-grid" aria-label="账号统计">
      <Panel title="成功账号" description="当前已验证凭据"><strong className="metric-number">{data.successful_count}</strong></Panel>
      <Panel title="失败尝试" description="保留历史，不覆盖成功凭据"><strong className="metric-number">{data.failed_attempt_count}</strong></Panel>
      <Panel title="最近任务" description={job?.created_at ?? "导入账号后可创建检查任务"}>{job ? <div className="flex items-center gap-3"><Status value={job.status} /><strong>{job.completed_count}/{job.total_count}</strong></div> : <EmptyState>暂无任务</EmptyState>}</Panel>
    </section>
    <Panel title="最近活动" description="最近任务执行状态">{job ? <div className="flex flex-wrap items-center gap-3"><strong>任务 {job.id.slice(0, 8)}</strong><Status value={job.status} /><Button role="link" nativeButton={false} variant="outline" size="sm" render={<Link href={data.active_job ? "/current-task" : "/logs"} />}>查看任务<ArrowRight data-icon="inline-end" /></Button></div> : <EmptyState>尚无历史记录</EmptyState>}</Panel>
  </div>;
}

function TaskComposer({ accounts, active, onStarted }: { accounts: Account[]; active: boolean; onStarted: () => void }) {
  const [selected, setSelected] = useState<number[]>([]);
  const [workers, setWorkers] = useState(8);
  const [verbose, setVerbose] = useState(false);
  const [clear, setClear] = useState(false);
  const [remove, setRemove] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [change, setChange] = useState(false);
  const [confirmChange, setConfirmChange] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setMessage("");
    if (!clear || (remove && !confirmRemove) || (change && !confirmChange)) { setError("请逐项确认所有启用的邮箱操作。"); return; }
    setBusy(true);
    try {
      const created = await api<{ id: string }>("/api/jobs", { method: "POST", body: JSON.stringify({
        account_ids: selected, workers, verbose, remove_aliases: remove, change_password: change,
        confirm_mailbox_cleanup: clear, confirm_remove_aliases: confirmRemove, confirm_change_password: confirmChange,
      }) });
      setMessage(`任务 ${created.id.slice(0, 8)} 已加入队列`); setSelected([]); onStarted();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "任务启动失败"); }
    finally { setBusy(false); }
  }
  return <Panel title="新建检查任务" description="本次任务选项仅影响当前任务；代理使用全局配置" action={<Badge variant="secondary">已选 {selected.length}</Badge>}>
    <form onSubmit={start} className="max-w-lg"><FieldGroup>
      <FieldSet><FieldLegend>选择已导入账号</FieldLegend>
        {accounts.length ? <div className="select-list"><FieldGroup>{accounts.map((account) => <CheckField key={account.id} checked={selected.includes(account.id)} onChange={(checked) => setSelected((rows) => checked ? [...rows, account.id] : rows.filter((id) => id !== account.id))}>{account.email}<Badge variant="outline">待检查</Badge></CheckField>)}</FieldGroup></div> : <EmptyState>没有待检查账号，请先在账号页导入</EmptyState>}
      </FieldSet>
      <Field><FieldLabel htmlFor="workers">并发数</FieldLabel><Input id="workers" className="max-w-32" type="number" min={1} max={256} value={workers} onChange={(event) => setWorkers(Number(event.target.value))} /></Field>
      <FieldSet><FieldLegend>本次任务选项</FieldLegend><FieldGroup>
        <CheckField checked={verbose} onChange={setVerbose}>详细进度日志</CheckField>
        <CheckField checked={remove} onChange={(value) => { setRemove(value); setConfirmRemove(false); }}>删除邮箱别名</CheckField>
        {remove && <CheckField checked={confirmRemove} onChange={setConfirmRemove}>我确认删除所选账号的别名</CheckField>}
        <CheckField checked={change} onChange={(value) => { setChange(value); setConfirmChange(false); }}>自动修改密码</CheckField>
        {change && <CheckField checked={confirmChange} onChange={setConfirmChange}>我确认修改所选账号密码，并会留存候选密码</CheckField>}
        <CheckField checked={clear} onChange={setClear}>我确认清空 Drafts、INBOX、OUTBOX、Sent、Spam 和 Trash 文件夹</CheckField>
      </FieldGroup></FieldSet>
      <Notice message={error} error /><Notice message={message} />
      <Button type="submit" disabled={busy || active || !selected.length}><Play data-icon="inline-start" />{active ? "已有任务运行中" : busy ? "正在启动…" : "开始任务"}</Button>
    </FieldGroup></form>
  </Panel>;
}

function useJobEvents(jobId: string | null, onUpdate?: () => void) {
  const [state, setState] = useState<{ jobId: string; logs: Log[] }>({ jobId: "", logs: [] });
  const [connection, setConnection] = useState("正在连接");
  useEffect(() => {
    if (!jobId) return;
    let closed = false;
    let cursor = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const source = new EventSource(`/api/jobs/${jobId}/events`);
    source.onopen = () => setConnection("已连接");
    source.onmessage = (event) => {
      const id = Number(event.lastEventId);
      if (id <= cursor) return;
      cursor = id;
      const payload = JSON.parse(event.data) as Record<string, unknown>;
      setState((current) => ({ jobId, logs: current.jobId === jobId ? [...current.logs, { id, payload }] : [{ id, payload }] }));
      if (onUpdate && !timer) timer = setTimeout(() => { timer = undefined; if (!closed) onUpdate(); }, 250);
    };
    source.onerror = () => { setConnection(source.readyState === EventSource.CLOSED ? "连接已关闭" : "连接中断，正在重连"); };
    source.addEventListener("complete", () => { source.close(); setConnection("任务已结束"); onUpdate?.(); });
    return () => { closed = true; if (timer) clearTimeout(timer); source.close(); };
  }, [jobId, onUpdate]);
  return { logs: state.jobId === jobId ? state.logs : [], connection };
}

function TaskMonitor({ jobId, onJobsChanged }: { jobId: string; onJobsChanged: () => void }) {
  const job = useLoad<Job>(`/api/jobs/${jobId}`);
  const reload = job.reload;
  const update = useCallback(() => { void reload(); onJobsChanged(); }, [reload, onJobsChanged]);
  const events = useJobEvents(jobId, update);
  const [replayError, setReplayError] = useState("");
  const [replayBusy, setReplayBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  async function replay() {
    setReplayBusy(true); setReplayError("");
    try { await api(`/api/jobs/${jobId}/replay`, { method: "POST" }); onJobsChanged(); }
    catch (cause) { setReplayError(cause instanceof Error ? cause.message : "重放失败"); }
    finally { setReplayBusy(false); }
  }
  async function copyPlan(plan: MockerPlan) {
    try { await navigator.clipboard.writeText(JSON.stringify(plan, null, 2)); setCopied(true); }
    catch { setReplayError("无法访问剪贴板"); }
  }
  const plan = job.data?.options?.mocker_plan;
  return <Panel title="任务进度" description={`任务 ${jobId}`} action={job.data ? <Status value={job.data.status} /> : undefined}>
    {job.loading ? <Loading /> : job.error || !job.data ? <ErrorState message={job.error} retry={job.reload} /> : <div className="flex flex-col gap-5">
      <div className="task-stats"><div className="stat-row"><span>进度</span><strong>{job.data.completed_count}/{job.data.total_count}</strong></div><div className="stat-row"><span>成功</span><strong>{job.data.success_count}</strong></div><div className="stat-row"><span>失败</span><strong>{job.data.failure_count}</strong></div></div>
      {plan && <Panel title="Mocker 故障计划" description={`版本 ${plan.version} · ${plan.mode === "scenario" ? `场景 ${plan.scenario ?? "未知"}` : `seed ${plan.seed ?? "未知"}`} · ${plan.faults.length} 个计划故障`}>
        <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" size="sm" onClick={() => void copyPlan(plan)}><Copy data-icon="inline-start" />{copied ? "已复制计划" : "复制计划 JSON"}</Button><Button type="button" variant="outline" size="sm" disabled={replayBusy || !["completed", "failed", "interrupted"].includes(job.data.status)} onClick={() => void replay()}><RotateCcw data-icon="inline-start" />{replayBusy ? "正在重放…" : "按原计划重放"}</Button></div>
        <pre className="mt-3 max-h-72 overflow-auto rounded-md border p-3 text-xs">{JSON.stringify(plan, null, 2)}</pre>
      </Panel>}
      <Notice message={replayError} error />
      <ResultLists results={job.data.results ?? []} />
      <div><h3>实时日志 · {events.connection}</h3><LogList logs={events.logs} /></div>
      <CandidatePanel jobId={jobId} live={!!job.data && ["queued", "running"].includes(job.data.status)} />
    </div>}
  </Panel>;
}
function CurrentTask() {
  const accounts = useLoad<{ items: Account[] }>("/api/accounts?status=pending");
  const jobs = useLoad<{ items: Job[] }>("/api/jobs");
  const active = jobs.data?.items.find((job) => ["queued", "running"].includes(job.status));
  const jobId = active?.id ?? jobs.data?.items[0]?.id;
  if (accounts.loading || jobs.loading) return <Loading />;
  if (accounts.error || jobs.error) return <ErrorState message={accounts.error || jobs.error} retry={() => { void accounts.reload(); void jobs.reload(); }} />;
  return <div className="flex flex-col gap-5"><PageTitle title="当前任务" description="为本次任务选择执行选项并确认邮箱操作；代理使用全局配置。" /><div className="task-grid">
    {accounts.data && <TaskComposer accounts={accounts.data.items} active={!!active} onStarted={jobs.reload} />}
    {jobId ? <TaskMonitor jobId={jobId} onJobsChanged={jobs.reload} /> : <Panel title="最近任务"><EmptyState>启动任务后，进度与结果会显示在这里</EmptyState></Panel>}
  </div></div>;
}

export function ResultLists({ results }: { results: Result[] }) {
  return <div className="result-columns">{[true, false].map((success) => {
    const rows = results.filter((row) => (row.result.ok === true) === success);
    return <div key={String(success)}><h3>{success ? "成功账号" : "失败账号"} · {rows.length}</h3>{rows.length ? rows.map((row, index) => <div className="result-row" key={row.id ?? `${row.email}-${index}`}><span>{row.email}</span><Badge variant={success ? "default" : "destructive"}>{success ? "成功" : String(row.result.error ?? row.status)}</Badge></div>) : <EmptyState>{success ? "暂无成功账号" : "暂无失败账号"}</EmptyState>}</div>;
  })}</div>;
}
function LogList({ logs }: { logs: Log[] }) {
  return logs.length ? <div className="log-list" aria-live="polite">{logs.map((log) => <pre key={log.id}>#{log.id} {JSON.stringify(log.payload)}</pre>)}</div> : <EmptyState>等待任务事件</EmptyState>;
}

function CandidatePanel({ jobId, live = false }: { jobId?: string; live?: boolean }) {
  const [email, setEmail] = useState("");
  const [query, setQuery] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const candidates = useLoad<{ items: Candidate[] }>(jobId ? `/api/password-candidates?job_id=${encodeURIComponent(jobId)}` : query ? `/api/password-candidates?email=${encodeURIComponent(query)}` : null);
  const reload = candidates.reload;
  useEffect(() => {
    if (!jobId || !live) { if (jobId) void reload(); return; }
    const timer = setInterval(() => void reload(), 5000);
    return () => clearInterval(timer);
  }, [jobId, live, reload]);
  async function verify(candidate: Candidate, applied: boolean) {
    setBusy(candidate.id); setFailure("");
    try { await api(`/api/password-candidates/${candidate.id}/verify`, { method: "POST", body: JSON.stringify({ applied }) }); await reload(); }
    catch (cause) { setFailure(cause instanceof Error ? cause.message : "核实状态保存失败"); }
    finally { setBusy(null); }
  }
  return <div className="flex flex-col gap-3"><h3>改密候选查询</h3>
    {!jobId && <form onSubmit={(event) => { event.preventDefault(); if (query === email) void reload(); else setQuery(email); }} className="max-w-xl"><FieldGroup><Field><FieldLabel htmlFor="candidate-email">邮箱地址</FieldLabel><Input id="candidate-email" className="max-w-md" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></Field><Button type="submit" variant="outline"><Search data-icon="inline-start" />查询</Button></FieldGroup></form>}
    <Notice message={failure} error />
    {candidates.loading ? <Loading /> : candidates.error ? <ErrorState message={candidates.error} retry={reload} /> : candidates.data?.items.length ? candidates.data.items.map((candidate) => <div className="candidate-row" key={candidate.id}>
      <strong>{candidate.password}</strong><Status value={candidate.status} /><small>{candidate.email} · {candidate.updated_at} · {candidate.job_id}</small>
      {candidate.status === "uncertain" && <div className="candidate-actions"><Button type="button" size="sm" disabled={busy === candidate.id} onClick={() => void verify(candidate, true)}>已确认生效</Button><Button type="button" size="sm" variant="outline" disabled={busy === candidate.id} onClick={() => void verify(candidate, false)}>确认未生效</Button></div>}
    </div>) : <EmptyState>{query || jobId ? "没有候选密码记录" : "查询后可查看候选密码及状态"}</EmptyState>}
  </div>;
}

function Accounts() {
  const [tab, setTab] = useState("successful");
  const accounts = useLoad<{ items: Account[] }>(`/api/accounts?status=${tab}`);
  const [content, setContent] = useState("");
  const [feedback, setFeedback] = useState("");
  const [failure, setFailure] = useState("");
  const [lineErrors, setLineErrors] = useState<Array<{ line: number; error: string }>>([]);
  const [busy, setBusy] = useState(false);
  async function importAccounts(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setFeedback(""); setFailure("");
    try { const result = await api<{ imported: number; errors: Array<{ line: number; error: string }> }>("/api/accounts/import", { method: "POST", body: JSON.stringify({ content }) });
      setFeedback(`已导入 ${result.imported} 条${result.errors.length ? `；${result.errors.length} 行格式无效` : ""}`);
      setLineErrors(result.errors); if (!result.errors.length) setContent(""); await accounts.reload();
    } catch (cause) { setFailure(cause instanceof Error ? cause.message : "导入失败"); }
    finally { setBusy(false); }
  }
  return <div className="flex flex-col gap-5"><PageTitle title="账号" description="成功凭据与失败尝试分别保存，失败密码不会覆盖已验证密码。" />
    <Panel title="账号记录"><Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
      <TabsList aria-label="账号状态"><TabsTrigger value="successful">成功账号</TabsTrigger><TabsTrigger value="failed">失败尝试</TabsTrigger><TabsTrigger value="pending">待检查</TabsTrigger></TabsList>
      <TabsContent value={tab}>
        {accounts.loading ? <Loading /> : accounts.error ? <ErrorState message={accounts.error} retry={accounts.reload} /> : accounts.data?.items.length ? <Table><TableHeader><TableRow><TableHead>邮箱</TableHead>{tab === "failed" && <><TableHead>错误分类</TableHead><TableHead>任务 ID</TableHead></>}<TableHead>时间</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{accounts.data.items.map((account, index) => <TableRow key={account.id ?? `${account.email}-${index}`}><TableCell>{account.email}</TableCell>{tab === "failed" && <><TableCell>{account.error ?? "检查失败"}</TableCell><TableCell>{account.job_id}</TableCell></>}<TableCell>{account.created_at}</TableCell><TableCell><Status value={account.status} /></TableCell></TableRow>)}</TableBody></Table> : <EmptyState>{tab === "successful" ? "还没有成功验证的账号" : tab === "failed" ? "还没有失败尝试" : "没有待检查账号"}</EmptyState>}
      </TabsContent></Tabs></Panel>
    <div className="two-column"><Panel title="导入账号" description="每行一组邮箱和密码，格式为 email----password">
      <form onSubmit={importAccounts} className="max-w-xl"><FieldGroup><Field data-invalid={lineErrors.length > 0}><FieldLabel htmlFor="account-content">账号凭据</FieldLabel><Textarea id="account-content" className="max-w-xl" aria-invalid={lineErrors.length > 0} rows={6} required value={content} onChange={(event) => { setContent(event.target.value); setLineErrors([]); }} placeholder="account@example.com----password" /></Field><Button type="submit" disabled={busy}><Upload data-icon="inline-start" />{busy ? "正在导入…" : "导入记录"}</Button><Notice message={feedback} /><Notice message={failure} error />{lineErrors.length > 0 && <Notice message={lineErrors.map((row) => `第 ${row.line} 行：账号格式不正确`).join("；")} error />}</FieldGroup></form>
    </Panel><Panel title="候选密码" description="核对外部邮箱状态后确认密码是否生效"><CandidatePanel /></Panel></div>
  </div>;
}

function JobLogs({ jobId }: { jobId: string }) {
  const events = useJobEvents(jobId);
  return <div className="flex flex-col gap-3"><Badge variant="outline">{events.connection}</Badge><LogList logs={events.logs} /></div>;
}
function Logs() {
  const jobs = useLoad<{ items: Job[] }>("/api/jobs");
  const [selection, setSelection] = useState("");
  const selected = selection || jobs.data?.items[0]?.id || "";
  return <div className="flex flex-col gap-5"><PageTitle title="日志" description="按原始内容查看持久化任务事件。断线后会从事件游标继续接收。" />
    <Panel title="任务日志" action={<Button variant="outline" size="sm" onClick={() => void jobs.reload()}><RefreshCw data-icon="inline-start" />刷新</Button>}>
      <FieldGroup><Field><FieldLabel htmlFor="job-select">任务记录</FieldLabel><NativeSelect id="job-select" className="max-w-xl" value={selected} onChange={(event) => setSelection(event.target.value)}><NativeSelectOption value="">选择任务</NativeSelectOption>{jobs.data?.items.map((job) => <NativeSelectOption key={job.id} value={job.id}>{job.created_at} · {labels[job.status] ?? job.status} · {job.id.slice(0, 8)}</NativeSelectOption>)}</NativeSelect></Field>
        {jobs.loading ? <Loading /> : jobs.error ? <ErrorState message={jobs.error} retry={jobs.reload} /> : selected ? <JobLogs key={selected} jobId={selected} /> : <EmptyState>尚无任务日志</EmptyState>}
      </FieldGroup>
    </Panel></div>;
}

function SettingsForm({ initial }: { initial: Settings }) {
  const [ipinfoKey, setIpinfoKey] = useState("");
  const [showIpinfoKey, setShowIpinfoKey] = useState(false);
  const [ipinfoKeyConfigured, setIpinfoKeyConfigured] = useState(initial.ipinfo_key_configured);
  const [feedback, setFeedback] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setFeedback(""); setFailure("");
    try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ ipinfo_key: ipinfoKey }) }); setFeedback("配置已保存。留空保持原值。"); if (ipinfoKey) setIpinfoKeyConfigured(true); setIpinfoKey(""); setShowIpinfoKey(false); }
    catch (cause) { setFailure(cause instanceof Error ? cause.message : "保存失败"); }
    finally { setBusy(false); }
  }
  async function toggleIpinfoKeyVisibility() {
    if (showIpinfoKey) { setShowIpinfoKey(false); return; }
    if (!ipinfoKey && ipinfoKeyConfigured) {
      setFailure("");
      try {
        const saved = await api<{ ipinfo_key: string }>("/api/settings/ipinfo-key");
        setIpinfoKey(saved.ipinfo_key);
      } catch (cause) {
        setFailure(cause instanceof Error ? cause.message : "读取密钥失败");
        return;
      }
    }
    setShowIpinfoKey(true);
  }
  return <form onSubmit={save} className="max-w-2xl"><FieldGroup>
    <Panel title="IP 查询" description="密钥加密保存在服务器；点击眼睛可查看已保存值"><FieldGroup><Field><FieldLabel htmlFor="ipinfo-key">IPinfo API 密钥</FieldLabel><div className="flex max-w-md min-w-0 gap-2"><Input id="ipinfo-key" type={showIpinfoKey ? "text" : "password"} autoComplete="new-password" value={ipinfoKey} onChange={(event) => setIpinfoKey(event.target.value)} placeholder={ipinfoKeyConfigured ? "已配置；留空保持原值" : "尚未配置"} /><Button type="button" variant="ghost" size="icon" aria-label={showIpinfoKey ? "隐藏 IPinfo API 密钥" : "显示 IPinfo API 密钥"} title={showIpinfoKey ? "隐藏密钥" : "显示密钥"} onClick={() => void toggleIpinfoKeyVisibility()}>{showIpinfoKey ? <EyeOff /> : <Eye />}</Button></div></Field></FieldGroup></Panel>
    <Notice message={failure} error /><Notice message={feedback} /><Button type="submit" disabled={busy}><Save data-icon="inline-start" />{busy ? "正在保存…" : "保存配置"}</Button>
  </FieldGroup></form>;
}
function SettingsView() {
  const state = useLoad<Settings>("/api/settings");
  return <div className="flex flex-col gap-5"><PageTitle title="配置" description="保存服务器级 IP 查询参数。" />{state.loading ? <Loading /> : state.error || !state.data ? <ErrorState message={state.error} retry={state.reload} /> : <SettingsForm initial={state.data} />}</div>;
}
function ProxyForm({ initial }: { initial: { configured: boolean; count: number } }) {
  const [proxies, setProxies] = useState("");
  const [configured, setConfigured] = useState(initial.configured);
  const [count, setCount] = useState(initial.count);
  const [feedback, setFeedback] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setFeedback(""); setFailure("");
    try {
      await api("/api/proxies", { method: "PUT", body: JSON.stringify({ proxies }) });
      const saved = await api<{ configured: boolean; count: number }>("/api/proxies");
      setConfigured(saved.configured); setCount(saved.count); setProxies("");
      setFeedback("全局代理已更新；仅之后创建的任务使用新配置。");
    } catch (cause) { setFailure(cause instanceof Error ? cause.message : "保存失败"); }
    finally { setBusy(false); }
  }
  return <form onSubmit={save} className="max-w-2xl"><FieldGroup>
    <Panel title="全局代理列表" description={configured ? `已配置 ${count} 条代理。输入新列表会整体替换，代理凭据不会回显。` : "尚未配置代理。新建任务前必须先保存代理列表。"}>
      <Field><FieldLabel htmlFor="global-proxies">代理列表</FieldLabel><Textarea id="global-proxies" className="max-w-2xl" required rows={6} value={proxies} onChange={(event) => setProxies(event.target.value)} placeholder="http://127.0.0.1:8080" /><FieldDescription>每行一条代理；支持代理 URL 或 host:port。保存后新任务统一使用此列表。</FieldDescription></Field>
    </Panel>
    <Notice message={failure} error /><Notice message={feedback} /><Button type="submit" disabled={busy}><Save data-icon="inline-start" />{busy ? "正在保存…" : "保存全局代理"}</Button>
  </FieldGroup></form>;
}
function ProxiesView() {
  const state = useLoad<{ configured: boolean; count: number }>("/api/proxies");
  return <div className="flex flex-col gap-5"><PageTitle title="代理" description="维护唯一的全局代理列表，任务创建时固定当前列表。" />{state.loading ? <Loading /> : state.error || !state.data ? <ErrorState message={state.error} retry={state.reload} /> : <ProxyForm initial={state.data} />}</div>;
}

type MockerConfig = {
  mode: "scenario" | "random";
  scenario: string;
  seed: string;
  operations: string[];
  probability: number;
  scenarios: Record<string, string>;
  available_operations: string[];
};

function MockerForm({ initial }: { initial: MockerConfig }) {
  const [mode, setMode] = useState(initial.mode);
  const [scenario, setScenario] = useState(initial.scenario);
  const [seed, setSeed] = useState(initial.seed ?? "");
  const [operations, setOperations] = useState(initial.operations ?? ["login", "oauth", "imap_select"]);
  const [probability, setProbability] = useState(initial.probability ?? 0.1);
  const [feedback, setFeedback] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setFeedback(""); setFailure("");
    try {
      const saved = await api<MockerConfig>("/api/mocker/config", {
        method: "PUT", body: JSON.stringify({ mode, scenario, seed, operations, probability }),
      });
      setMode(saved.mode); setScenario(saved.scenario); setSeed(saved.seed ?? "");
      setOperations(saved.operations ?? []); setProbability(saved.probability ?? 0.1);
      setFeedback("Mocker 场景配置已保存；新任务会使用该配置生成并保存故障计划。");
    } catch (cause) { setFailure(cause instanceof Error ? cause.message : "保存失败"); }
    finally { setBusy(false); }
  }
  return <form onSubmit={save} className="max-w-2xl"><FieldGroup>
    <Panel title="故障模式" description="仅影响隔离的合成账号与本地 mail mocker。预设场景用于确定性验收；随机模式按 seed 生成可重放计划。">
      <FieldGroup>
        <Field><FieldLabel htmlFor="mocker-mode">模式</FieldLabel><NativeSelect id="mocker-mode" value={mode} onChange={(event) => setMode(event.target.value as MockerConfig["mode"])}><NativeSelectOption value="scenario">预设场景</NativeSelectOption><NativeSelectOption value="random">随机组合</NativeSelectOption></NativeSelect></Field>
        {mode === "scenario" ? <Field><FieldLabel htmlFor="mocker-scenario">预设场景</FieldLabel><NativeSelect id="mocker-scenario" value={scenario} onChange={(event) => setScenario(event.target.value)}>{Object.entries(initial.scenarios).map(([id, label]) => <NativeSelectOption key={id} value={id}>{label}</NativeSelectOption>)}</NativeSelect></Field> : <>
          <Field><FieldLabel htmlFor="mocker-seed">随机 seed</FieldLabel><Input id="mocker-seed" required value={seed} onChange={(event) => setSeed(event.target.value)} /><FieldDescription>相同 seed、账号和参数会生成相同故障计划。</FieldDescription></Field>
          <Field><FieldLabel htmlFor="mocker-probability">每个操作发生故障的概率</FieldLabel><Input id="mocker-probability" type="number" min={0} max={1} step={0.01} value={probability} onChange={(event) => setProbability(Number(event.target.value))} /></Field>
          <FieldSet><FieldLegend>纳入随机计划的操作</FieldLegend><FieldGroup>{initial.available_operations.map((operation) => <CheckField key={operation} checked={operations.includes(operation)} onChange={(checked) => setOperations((current) => checked ? [...current, operation] : current.filter((item) => item !== operation))}>{operation}</CheckField>)}</FieldGroup></FieldSet>
        </>}
      </FieldGroup>
    </Panel>
    <Notice message={failure} error /><Notice message={feedback} />
    <Button type="submit" disabled={busy || (mode === "random" && (!seed.trim() || !operations.length))}><Save data-icon="inline-start" />{busy ? "正在保存…" : "保存 Mocker 配置"}</Button>
  </FieldGroup></form>;
}
function MockerView() {
  const state = useLoad<MockerConfig>("/api/mocker/config");
  return <div className="flex flex-col gap-5"><PageTitle title="Mocker 测试配置" description="配置确定性故障场景或可重放的随机故障计划。" />{state.loading ? <Loading /> : state.error || !state.data ? <ErrorState message={state.error} retry={state.reload} /> : <MockerForm initial={state.data} />}</div>;
}

export function ConsoleView({ page }: { page: Page }) {
  if (page === "mocker") return <MockerView />;
  if (page === "dashboard") return <Dashboard />;
  if (page === "current-task") return <CurrentTask />;
  if (page === "accounts") return <Accounts />;
  if (page === "logs") return <Logs />;
  if (page === "proxies") return <ProxiesView />;
  return <SettingsView />;
}
