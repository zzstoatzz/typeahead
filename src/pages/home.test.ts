import { expect, test } from "bun:test";
import { indexPage } from "./home";

test("homepage results are a selectable list that hands profiles to aturi.to", () => {
  const html = indexPage();
  expect(html).toContain('role="combobox"');
  expect(html).toContain('role="listbox"');
  expect(html).toContain('id="picked"');
  expect(html).toContain("'https://aturi.to/' + actor.did");
  expect(html).toContain("row.addEventListener('click', () => choose(actor));");
});

test("the homepage searches from the first character, as the API answers", () => {
  const html = indexPage();
  expect(html).toContain("if (!value) return;");
  expect(html).not.toMatch(/value\.length < \d/);
});
