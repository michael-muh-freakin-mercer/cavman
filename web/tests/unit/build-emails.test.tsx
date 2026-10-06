import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BuildEmailsToggle } from "@/components/app/build-emails-toggle";
import { deliverNotices, noticeEmail, type Notice } from "@/lib/build-emails";

const notice = (overrides: Partial<Notice> = {}): Notice => ({
  job_id: "j".repeat(32), run_id: "r".repeat(32), owner_id: "alice", project_name: "Tattoo booking",
  prompt: "Build me a booking core for a <tattoo> studio", state: "complete", label: "Complete",
  explanation: "Every task was accepted.", tasks_accepted: 2, tasks_total: 2, cost_usd: 0.123,
  finished_at: "2026-10-02T00:00:00+00:00", ...overrides,
});

describe("noticeEmail", () => {
  it("links to the run and to Settings, and escapes the prompt", () => {
    const email = noticeEmail("a@example.com", notice(), "https://cavman.dev/");
    expect(email.subject).toBe("Your build is ready: Tattoo booking");
    expect(email.text).toContain("2 of 2 tasks passed");
    expect(email.text).toContain(`https://cavman.dev/app/runs/${"r".repeat(32)}`);
    expect(email.text).toContain("https://cavman.dev/app/settings");
    expect(email.text).toContain("$0.12");
    expect(email.html).toContain("&lt;tattoo&gt;");
    expect(email.html).not.toContain("<tattoo>");
  });

  it("words each state the owner has to act on", () => {
    expect(noticeEmail("a@x", notice({ state: "approval_needed" }), "https://c").subject).toMatch(/^Cavman needs your decision/);
    expect(noticeEmail("a@x", notice({ state: "input_needed" }), "https://c").subject).toMatch(/^Cavman has questions/);
    expect(noticeEmail("a@x", notice({ state: "budget_reached" }), "https://c").subject).toMatch(/^Your build reached its budget/);
    const failed = noticeEmail("a@x", notice({ state: "failed", explanation: "The worker crashed.", cost_usd: null }), "https://c");
    expect(failed.subject).toMatch(/^Your build stopped/);
    expect(failed.text).toContain("The worker crashed.");
    expect(failed.text).not.toContain("Model cost");
  });
});

describe("deliverNotices", () => {
  it("emails verified owners who have not opted out, and marks every handled notice", async () => {
    const notices = [notice({ job_id: "1", owner_id: "alice" }), notice({ job_id: "2", owner_id: "bob" }),
      notice({ job_id: "3", owner_id: "carol" }), notice({ job_id: "4", owner_id: "gone" }),
      notice({ job_id: "5", owner_id: "dave" })];
    const users: Record<string, object> = {
      alice: { email: "alice@x", emailVerified: true },
      bob: { email: "bob@x", emailVerified: true, buildEmails: false },
      carol: { email: "carol@x", emailVerified: false },
      dave: { email: "dave@x", emailVerified: true, buildEmails: true },
    };
    const send = vi.fn(async (email: { to: string }) => {
      if (email.to === "dave@x") throw new Error("provider down");
    });
    const markSent = vi.fn<(jobId: string) => Promise<void>>(async () => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const result = await deliverNotices({
      fetchNotices: async () => notices, markSent, send,
      findUser: async (id) => (users[id] as never) ?? null, baseUrl: "https://cavman.dev",
    });
    expect(result).toEqual({ sent: 1, skipped: 3, failed: 1 });
    expect(send.mock.calls.map(([email]) => email.to)).toEqual(["alice@x", "dave@x"]);
    // dave's failed send stays unmarked so the next pass retries it.
    expect(markSent.mock.calls.map(([id]) => id)).toEqual(["1", "2", "3", "4"]);
  });
});

describe("BuildEmailsToggle", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("saves the choice and puts it back if saving fails", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: false });
    vi.stubGlobal("fetch", fetchMock);
    render(<BuildEmailsToggle initial />);
    const box = screen.getByRole("checkbox", { name: /Email me about my builds/ });
    expect(box).toBeChecked();
    await userEvent.click(box);
    await waitFor(() => expect(box).not.toBeChecked());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ buildEmails: false });
    await userEvent.click(box);
    expect(await screen.findByRole("alert")).toHaveTextContent("did not save");
    expect(box).not.toBeChecked();
  });
});
