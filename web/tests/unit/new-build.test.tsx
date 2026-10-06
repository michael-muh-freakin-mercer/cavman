import { CostEstimate } from "@/components/app/cost-estimate";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NewBuildForm } from "@/components/app/new-build-form";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
const linkSocial = vi.fn();
vi.mock("@/lib/auth-client", () => ({ linkSocial: (...args: unknown[]) => linkSocial(...args) }));
afterEach(() => vi.unstubAllGlobals());

const props = { initialPrompt: "Add a greeting endpoint", defaultBudget: 5, maxBudget: 100, disabledReason: null };

describe("NewBuildForm repository import", () => {
  it("sends a public GitHub repository for a new project", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: "r1" }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<NewBuildForm {...props} projectId={null} projectName={null} />);
    await userEvent.type(screen.getByLabelText("Start from a GitHub repository"), "https://github.com/octo/demo");
    await userEvent.click(screen.getByRole("button", { name: /Build it/ }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/app/runs/r1"));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).settings.repository_url).toBe("https://github.com/octo/demo");
  });

  it("refuses other addresses before calling the server", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<NewBuildForm {...props} projectId={null} projectName={null} />);
    await userEvent.type(screen.getByLabelText("Start from a GitHub repository"), "https://evil.test/octo/demo");
    await userEvent.click(screen.getByRole("button", { name: /Build it/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub repository address");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("offers GitHub access when the repository is private, and comes back to the filled-in form", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: "That repository is private. To import it, give Cavman access to your GitHub repositories.",
      needs_scope: "repo",
    }), { status: 422 })));
    render(<NewBuildForm {...props} projectId={null} projectName={null} githubEnabled />);
    await userEvent.type(screen.getByLabelText("Start from a GitHub repository"), "https://github.com/octo/secret");
    await userEvent.click(screen.getByRole("button", { name: /Build it/ }));
    await userEvent.click(await screen.findByRole("button", { name: /Give Cavman access to your GitHub repositories/ }));
    const [{ scopes, callbackURL }] = linkSocial.mock.calls[0];
    expect(scopes).toEqual(["repo"]);
    expect(callbackURL).toBe("/app/new?prompt=Add%20a%20greeting%20endpoint&repository=https%3A%2F%2Fgithub.com%2Focto%2Fsecret");
  });

  it("does not offer GitHub access when GitHub sign-in is not configured", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: "That repository is private.", needs_scope: "repo" }), { status: 422 })));
    render(<NewBuildForm {...props} projectId={null} projectName={null} />);
    await userEvent.type(screen.getByLabelText("Start from a GitHub repository"), "https://github.com/octo/secret");
    await userEvent.click(screen.getByRole("button", { name: /Build it/ }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: /Give Cavman access/ })).toBeNull();
  });

  it("offers no import for a run in an existing project", () => {
    render(<NewBuildForm {...props} projectId={"a".repeat(32)} projectName="Demo" />);
    expect(screen.queryByLabelText("Start from a GitHub repository")).toBeNull();
  });
});

describe("cost estimate", () => {
  const estimate = {
    window_days: 30,
    min_builds: 5,
    modes: {
      automatic: { builds: 10, median_usd: 0.09, low_usd: 0.04, high_usd: 0.15 },
      budget: { builds: 2, median_usd: null, low_usd: null, high_usd: null },
    },
    default_budget_usd: 5,
    max_budget_usd: 100,
    default_max_model_calls: 60,
    account: {
      remaining_usd: 24.5,
      limit_usd: 25,
      remaining_calls: 2900,
      max_model_calls: 3000,
      cost_complete: true,
      calls_without_cost: 0,
      exhausted: false,
    },
  };

  it("shows what recent builds cost, the ceiling and the allowance", () => {
    render(<CostEstimate estimate={estimate} mode="automatic" modeLabel="Automatic" budget={5} />);
    const box = screen.getByTestId("cost-estimate");
    expect(box).toHaveTextContent("Recent Automatic builds here cost about $0.09");
    expect(box).toHaveTextContent("most between $0.04 and $0.15; 10 builds in the last 30 days");
    expect(box).toHaveTextContent("This build stops at $5.00. You have $24.50 of $25.00 and 2900 of 3000 model calls left this month.");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("admits when there is too little history, and warns when the allowance is below the ceiling", () => {
    render(<CostEstimate estimate={estimate} mode="budget" modeLabel="Budget" budget={30} />);
    expect(screen.getByTestId("cost-estimate")).toHaveTextContent("No cost history for Budget builds yet");
    expect(screen.getByRole("status")).toHaveTextContent("may pause before finishing");
  });

  it("says the allowance may be lower when some calls reported no cost", () => {
    const partial = { ...estimate, account: { ...estimate.account, cost_complete: false, calls_without_cost: 3 } };
    render(<CostEstimate estimate={partial} mode="automatic" modeLabel="Automatic" budget={5} />);
    const box = screen.getByTestId("cost-estimate");
    expect(box).toHaveTextContent("You have at most $24.50 of $25.00");
    expect(box).toHaveTextContent("3 calls reported no cost, so you may have less.");
  });

  it("warns when the model-call allowance is low or used up, even with dollars left", () => {
    const low = { ...estimate, account: { ...estimate.account, remaining_calls: 10 } };
    const { unmount } = render(<CostEstimate estimate={low} mode="automatic" modeLabel="Automatic" budget={5} />);
    expect(screen.getByRole("status")).toHaveTextContent("may pause before finishing");
    unmount();
    const out = { ...estimate, account: { ...estimate.account, remaining_calls: 0, exhausted: true } };
    render(<CostEstimate estimate={out} mode="automatic" modeLabel="Automatic" budget={5} />);
    expect(screen.getByRole("status")).toHaveTextContent("allowance is used up");
  });
});
