import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { PublishPanel, slugify } from "@/components/run/publish-panel";
import { runDetail } from "./fixtures";

const linkSocial = vi.fn();
vi.mock("@/lib/auth-client", () => ({ linkSocial: (...args: unknown[]) => linkSocial(...args) }));

beforeAll(() => {
  // jsdom lacks <dialog> methods.
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
afterEach(() => vi.unstubAllGlobals());

const delivery = { status: "ready" as const, created_at: "", error: null, files: [], documents: [], deleted: [],
  total_files: 3, commit: "abcdef1234567890abcdef1234567890abcdef12", report: null, downloadable: true };

describe("PublishPanel", () => {
  it("states exactly what will be created and pushed, then publishes on confirmation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ publication: {} }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const onPublished = vi.fn().mockResolvedValue(undefined);
    render(<PublishPanel run={runDetail({ project_name: "Booking App!", delivery })} onPublished={onPublished} />);
    await userEvent.click(screen.getByRole("button", { name: /Publish to GitHub/ }));
    expect(screen.getByText("abcdef1")).toBeInTheDocument();
    expect(screen.getByText(/Nothing else is created, changed or overwritten/)).toBeInTheDocument();
    expect(screen.getByLabelText("Repository name")).toHaveValue("booking-app");
    await userEvent.click(screen.getByLabelText("Public"));
    await userEvent.click(screen.getByRole("button", { name: "Create repository and push" }));
    await waitFor(() => expect(onPublished).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ name: "booking-app", private: false, confirm: true });
  });

  it("offers a pull request on the repository an earlier build created", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ publication: {} }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const onPublished = vi.fn().mockResolvedValue(undefined);
    const run = runDetail({ delivery, project_publication: { repository: "alice/booking", private: true, commit: "1".repeat(40) } });
    render(<PublishPanel run={run} onPublished={onPublished} />);
    await userEvent.click(screen.getByRole("button", { name: /Open a pull request/ }));
    expect(screen.getByText("alice/booking")).toBeInTheDocument();
    expect(screen.queryByLabelText("Repository name")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Push branch and open pull request" }));
    await waitFor(() => expect(onPublished).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ mode: "pull_request", private: true, confirm: true });
  });

  it("can still publish a follow-up build to a new repository", async () => {
    const run = runDetail({ delivery, project_publication: { repository: "alice/booking", private: true, commit: "1".repeat(40) } });
    render(<PublishPanel run={run} onPublished={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /Open a pull request/ }));
    await userEvent.click(screen.getByRole("button", { name: "Publish to a new repository instead" }));
    expect(screen.getByLabelText("Repository name")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create repository and push" })).toBeInTheDocument();
  });

  it("asks for exactly the missing GitHub scope instead of publishing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "GitHub access is needed to create the repository.", needs_scope: "repo" }), { status: 409 })));
    render(<PublishPanel run={runDetail({ delivery })} onPublished={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /Publish to GitHub/ }));
    await userEvent.click(screen.getByRole("button", { name: "Create repository and push" }));
    await userEvent.click(await screen.findByRole("button", { name: /Grant GitHub access/ }));
    expect(linkSocial).toHaveBeenCalledWith(expect.objectContaining({ provider: "github", scopes: ["repo"] }));
  });

  it("links to the repository once published and offers nothing without a verified commit", () => {
    const { rerender } = render(<PublishPanel run={runDetail({ delivery, publication: {
      repository: "alice/booking", url: "https://github.com/alice/booking", commit: delivery.commit, private: true, created_at: "" } })}
      onPublished={vi.fn()} />);
    expect(screen.getByRole("link", { name: /View Repository/ })).toHaveAttribute("href", "https://github.com/alice/booking");
    rerender(<PublishPanel run={runDetail({ delivery: null })} onPublished={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Publish/ })).not.toBeInTheDocument();
  });

  it("slugifies project names into valid repository names", () => {
    expect(slugify("Tattoo Studio: Booking!")).toBe("tattoo-studio-booking");
    expect(slugify("!!!")).toBe("cavman-project");
  });
});
