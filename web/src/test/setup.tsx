import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(cleanup);

vi.mock("next/navigation", () => ({
  usePathname: () => "/accounts",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) =>
    <a href={href} {...props}>{children}</a>,
}));

export class MockEventSource {
  static instances: MockEventSource[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  readyState = MockEventSource.OPEN;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  listeners = new Map<string, () => void>();
  constructor(public url: string) { MockEventSource.instances.push(this); }
  close() { this.readyState = MockEventSource.CLOSED; }
  addEventListener(type: string, callback: () => void) { this.listeners.set(type, callback); }
  receive(id: number, data: unknown) { this.onmessage?.(new MessageEvent("message", { lastEventId: String(id), data: JSON.stringify(data) })); }
  complete() { this.listeners.get("complete")?.(); }
}
Object.defineProperty(globalThis, "EventSource", { value: MockEventSource, configurable: true });
