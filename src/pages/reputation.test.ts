import { describe, test, expect } from "bun:test";
import { reputationPage } from "./reputation";

// the inline script auto-loads a report for ?actor= deep links. that call must
// come after every top-level const/let it transitively uses, or the page dies
// in the temporal dead zone — which the smoke gate's parse check cannot see.
describe("reputation page script", () => {
  const scripts = [...reputationPage().matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]!);
  const main = scripts[scripts.length - 1]!;

  test("every inline script parses", () => {
    for (const s of scripts) new Function(s);
  });

  test("the deep-link load runs after the last top-level declaration", () => {
    const loadAt = main.indexOf("load(initialActor)");
    expect(loadAt).toBeGreaterThan(0);
    const decls = [...main.matchAll(/^ {4}(?:const|let) /gm)].map((m) => m.index!);
    expect(Math.max(...decls)).toBeLessThan(loadAt);
  });
});
