import { expect, test } from "bun:test";
import { evalPage } from "./eval";
import { reportSchema, type Observation, type Report } from "../eval/model";

const row: Observation = { id: "a", run: "r", corpus: "v1", provider: "typeahead", q: "alice.test", category: "exact handle", round: 0,
  startedAt: 1000, finishedAt: 1100, ms: 100, outcome: "ok", status: 200,
  actors: [{ did: "did:plc:alice", handle: "alice.test", displayName: '<script>alert("x")</script>' }], headers: {}, error: null, cooldownUntil: 0 };
const report: Report = { version: 1, run: "r", corpus: "v1", location: "local", startedAt: 1000, updatedAt: 1100, expected: 4,
  observations: [row, { ...row, id: "b", provider: "bluesky", ms: 200 }] };

test("page renders measured values and safely escapes upstream profile text", () => {
  const page = evalPage(reportSchema.parse(report));
  expect(page).toContain("100 ms");
  expect(page).toContain("200 ms");
  expect(page).toContain("2 / 4 requests");
  expect(page).toContain("&lt;script&gt;");
  expect(page).not.toContain('<script>alert("x")');
  expect(page).not.toContain("fetch(");
  expect(page).not.toContain("<img");
});

test("filters apply to chart, summary and query details together", () => {
  const page = evalPage(report, new URLSearchParams({ pass: "2", metric: "results" }));
  expect(page).toContain("no observations match these filters");
  expect(page).not.toContain('<strong>alice.test</strong>');
  expect(page).toContain("results per query");
});

test("empty and fallback states don't imply native comparison evidence", () => {
  expect(evalPage(null)).toContain("no sample collected yet");
  const page = evalPage({ ...report, observations: [{ ...row, headers: { "x-typeahead-engine": "bluesky" } }, report.observations[1]] });
  expect(page).toContain("1 typeahead answers came from Bluesky fallback");
  expect(page).toContain("0 matched cases");
});

test("aggregate stays on the whole run while queries are inspected; forms pin that run", () => {
  const page = evalPage(report, new URLSearchParams({ category: "unmatched", view: "test-set" }));
  expect(page).toContain("no observations match these filters");
  expect(page).toContain("benchmark score 100");
  expect(page).toContain('name="run" value="r"');
  expect(page).toContain('id="test-set" open');
  expect(page).not.toContain('id="dimensions" open');
  expect(evalPage(report)).toContain('id="test-set"><summary>');
});

test("timing drill-down stays collapsed, preserves provenance and marks missing attribution", () => {
  const page = evalPage(report);
  expect(page).toContain('id="timing"><summary>where time goes');
  expect(page).toContain("unattributed");
  expect(page).toContain("not captured");
  expect(page).not.toContain("0% in the backend round trip");
  const selected = evalPage(report, new URLSearchParams({ view: "timing", sample: "a" }));
  expect(selected).toContain('id="timing" open');
  expect(selected).toContain('value="a" selected');
  expect(selected).toContain('name="run" value="r"');
  expect(selected).toContain("Do not add the bars");
  expect(page).not.toContain("NaN");
});

test("three-provider runs show field coverage while old snapshots mark discarded fields unknown", () => {
  expect(evalPage(report)).toContain("not captured");
  const page = evalPage({ ...report, providers: ["typeahead", "bluesky", "tern"], actorFieldsVersion: 1, expected: 3,
    observations: [...report.observations, { ...row, id: "tern", provider: "tern",
      actors: [{ did: "did:plc:alice", handle: "alice.test", labels: [],
        verification: { verifiedStatus: "none", trustedVerifierStatus: "none" } }] }] });
  expect(page).toContain("Tern benchmark score");
  expect(page).toContain("labels coverage");
  expect(page).toContain("verification coverage");
  expect(page).toContain("tern-text");
  expect(page).not.toContain("not captured</td>");
});

test("method describes the selected manifest and normalized weights", () => {
  const page = evalPage({ ...report, queries: [{ q: row.q, category: row.category }] }, new URLSearchParams({ speed: "10", availability: "20", exact: "10" }));
  expect(page).toContain("1 query × 2 passes × 2 services");
  expect(page).toContain("25% response speed · 50% successful answers · 25% exact-handle rank");
  expect(page).toContain("Relevance and field coverage are not scored");
  expect(page).toContain("Missing verification means unknown, not unverified");
  expect(page).toContain("Page views only read saved data");
  expect(page).not.toContain("20 queries · 2 passes");
});
