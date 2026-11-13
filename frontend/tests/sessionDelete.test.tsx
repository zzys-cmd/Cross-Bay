// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";
import HistorySearch from "../src/components/HistorySearch";
import SessionDeleteDialog from "../src/components/SessionDeleteDialog";
import type { SessionSummary } from "../src/types";

const records: SessionSummary[] = [
  { id: "session-new", title: "通勤耳机", updatedAt: 2000, source: "server" },
  { id: "session-old", title: "旅行背包", updatedAt: 1000, source: "server" },
];
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

describe("删除选购记录", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("删除当前会话：服务端 DELETE、清本机缓存并回到新的选购", async () => {
    const calls: { url: string; method: string }[] = [];
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run",
      storage: localStorage,
      fetch: async (input, init) => {
        const url = String(input), method = init?.method ?? "GET";
        calls.push({ url, method });
        if (method === "DELETE") return json({ deleted: true, sessionId: "session-new" });
        if (url.includes("/sessions?")) return json({ sessions: records });
        return json({});
      },
    });
    await client.initialize();
    expect(client.getSnapshot().sessionId).toBe("session-new");

    await client.deleteSession("session-new");
    expect(calls.some(call => call.method === "DELETE" && call.url.startsWith("/commerce/ag-ui/sessions/session-new?buyer_id="))).toBe(true);
    const snapshot = client.getSnapshot();
    expect(snapshot.history.map(item => item.id)).toEqual(["session-old"]);
    expect(snapshot.sessionId).not.toBe("session-new");
    expect(snapshot.messages).toHaveLength(0);
    // 本机缓存同步移除，刷新后不会复活已删除的会话。
    expect(localStorage.getItem("globex.agui.sessions.v1") ?? "").not.toContain("session-new");
  });

  it("纯本机缓存模式不发删除请求，直接移除本机会话", async () => {
    const local = [{ id: "local-1", title: "本机选购", updatedAt: 1, messages: [], products: [], searchCompleted: false, runId: null }];
    localStorage.setItem("globex.buyer", "ycx");
    localStorage.setItem("globex.agui.sessions.v1", JSON.stringify(local));
    const fetch = vi.fn();
    const client = new CommerceClient({ url: "/api/chat", storage: localStorage, fetch });
    expect(client.getSnapshot().history.map(item => item.id)).toEqual(["local-1"]);
    await client.deleteSession("local-1");
    expect(fetch).not.toHaveBeenCalled();
    expect(client.getSnapshot().history).toHaveLength(0);
  });

  it("删除非当前会话：保留当前会话与运行中的提示", async () => {
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run",
      storage: localStorage,
      fetch: async (input, init) => {
        if (init?.method === "DELETE") return new Response("", { status: 409 });
        if (String(input).includes("/sessions?")) return json({ sessions: records });
        return json({});
      },
    });
    await client.initialize();
    await expect(client.deleteSession("session-old")).rejects.toThrow("该会话仍在执行，请先停止后再删除。");
    expect(client.getSnapshot().history.map(item => item.id)).toEqual(["session-new", "session-old"]);
    expect(client.getSnapshot().sessionId).toBe("session-new");
  });
});

describe("删除入口的界面交互", () => {
  let host: HTMLDivElement, root: Root;
  let originalScrollIntoView: typeof Element.prototype.scrollIntoView;

  beforeEach(() => {
    (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
    originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = vi.fn();
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    Element.prototype.scrollIntoView = originalScrollIntoView;
    vi.restoreAllMocks();
  });

  it("确认弹窗默认聚焦“保留记录”，确认后才回调删除", async () => {
    const confirmed = vi.fn(), closed = vi.fn();
    await act(async () => root.render(
      <SessionDeleteDialog session={records[1]} busy={false} error="删除未能完成，请稍后重试。"
        onConfirm={confirmed} onClose={closed} />));
    const dialog = host.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("旅行背包");
    expect(dialog.querySelector('[role="alert"]')?.textContent).toContain("请稍后重试");
    expect(document.activeElement?.textContent).toBe("保留记录");
    expect(confirmed).not.toHaveBeenCalled();
    await act(async () => host.querySelector<HTMLButtonElement>(".session-delete-confirm")!.click());
    expect(confirmed).toHaveBeenCalledTimes(1);
  });

  it("历史搜索每条记录都有删除按钮，点击只把该条交给上层", async () => {
    const deleted = vi.fn(), opened = vi.fn();
    await act(async () => root.render(
      <HistorySearch history={records} currentSessionId={records[0].id} busy={false}
        onOpen={opened} onDelete={deleted} onClose={() => {}} />));
    const buttons = [...host.querySelectorAll<HTMLButtonElement>(".conversation-search-delete")];
    expect(buttons).toHaveLength(2);
    expect(buttons[1].getAttribute("aria-label")).toBe("删除选购记录：旅行背包");
    await act(async () => buttons[1].click());
    expect(deleted).toHaveBeenCalledWith(records[1]);
    expect(opened).not.toHaveBeenCalled();
  });
});
