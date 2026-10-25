import { expect, test } from "bun:test";
import transport from "./search-transport";

test("transport rejects arbitrary origins, admin paths and mutations", async () => {
  for (const [url, method] of [
    ["https://example.com/search?q=nate", "GET"],
    ["http://typeahead-search-compact.fly.dev/search?q=nate", "GET"],
    ["https://typeahead-search-compact.fly.dev/admin/refresh", "GET"],
    ["https://typeahead-search-compact.fly.dev/search?q=nate", "POST"],
    ["http://typeahead-startup-20261024.fly.dev/search?q=nate", "GET"],
    ["https://typeahead-startup-20261024.fly.dev/admin/refresh", "GET"],
    ["https://typeahead-startup-20261024.fly.dev/search?q=nate", "POST"],
    ["https://typeahead-search-transport.n8-3e9.workers.dev/search?q=nate", "GET"],
  ]) {
    const response = await transport.fetch(new Request(url, { method }));
    expect(response.status).toBe(404);
  }
});
