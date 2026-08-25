import { describe, expect, test } from "bun:test";
import { indexPage } from "./home";
import { llmsTxt } from "./llms";

describe("service messaging", () => {
  test("llms.txt carries no experimental disclaimer", () => {
    const text = llmsTxt();

    expect(text).toContain("community-run actor search");
    expect(text).not.toContain("experimental");
    expect(text).not.toContain("don't depend on it");
  });

  test("the homepage no longer carries the experimental badge", () => {
    const html = indexPage();

    expect(html).not.toContain("experimental");
    expect(html).not.toContain("don't depend on it");
  });
});
