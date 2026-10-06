import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Pager, cursorParam } from "@/components/app/pager";

describe("pager", () => {
  it("shows nothing on a single page", () => {
    const { container } = render(<Pager path="/app/runs" next={null} paged={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("links to older items with the cursor, and back to the newest", () => {
    render(<Pager path="/app/runs" next="MjAyNi0xMC0wMQ==" paged />);
    expect(screen.getByRole("link", { name: /Older/ })).toHaveAttribute("href", "/app/runs?before=MjAyNi0xMC0wMQ%3D%3D");
    expect(screen.getByRole("link", { name: /Newest/ })).toHaveAttribute("href", "/app/runs");
  });

  it("passes on only cursor-shaped values from the address bar", () => {
    expect(cursorParam(undefined)).toBe("");
    expect(cursorParam("abc_-123==")).toBe("before=abc_-123%3D%3D");
    expect(cursorParam("x&limit=1000")).toBe("");
    expect(cursorParam("../../account")).toBe("");
  });
});
