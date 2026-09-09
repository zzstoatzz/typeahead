(() => {
  // docs/blog-assets/typeahead-sequel/traffic-evidence.json
  var traffic_evidence_default = {
    query: "SELECT date_bin(INTERVAL '15 minutes', start_timestamp) AS window, count(*) AS searches, percentile_cont(0.5) WITHIN GROUP (ORDER BY duration)*1000 AS p50_ms, percentile_cont(0.95) WITHIN GROUP (ORDER BY duration)*1000 AS p95_ms FROM records WHERE span_name='search' AND kind='span' GROUP BY 1 ORDER BY 1",
    min_timestamp: "2026-09-04T16:45:00Z",
    max_timestamp: "2026-09-04T19:00:00Z",
    data: [
      {
        window: "2026-09-04T16:45:00Z",
        searches: 163,
        p50_ms: 4.023614812500001,
        p95_ms: 17.986704
      },
      {
        window: "2026-09-04T17:00:00Z",
        searches: 100,
        p50_ms: 2.9867675,
        p95_ms: 20.027130500000002
      },
      {
        window: "2026-09-04T17:15:00Z",
        searches: 124,
        p50_ms: 4.007566291666667,
        p95_ms: 17.8079115
      },
      {
        window: "2026-09-04T17:30:00Z",
        searches: 1705,
        p50_ms: 3.5615873028074874,
        p95_ms: 16.620122741735535
      },
      {
        window: "2026-09-04T17:45:00Z",
        searches: 3496,
        p50_ms: 3.365942862142778,
        p95_ms: 13.678415975875602
      },
      {
        window: "2026-09-04T18:00:00Z",
        searches: 4076,
        p50_ms: 3.1115444246013126,
        p95_ms: 11.452792540633903
      },
      {
        window: "2026-09-04T18:15:00Z",
        searches: 4625,
        p50_ms: 3.0673288650060386,
        p95_ms: 9.887544257560746
      },
      {
        window: "2026-09-04T18:30:00Z",
        searches: 5423,
        p50_ms: 2.9571019235647524,
        p95_ms: 8.91260938234503
      },
      {
        window: "2026-09-04T18:45:00Z",
        searches: 4934,
        p50_ms: 2.893294580750481,
        p95_ms: 8.34266539012987
      }
    ]
  };

  // docs/blog-assets/typeahead-sequel/interactive/dom.ts
  function required(value) {
    if (value == null)
      throw new Error("An expected article element or observation is missing");
    return value;
  }
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // docs/blog-assets/typeahead-sequel/interactive/traffic.ts
  function initTraffic() {
    const root = document.querySelector("#traffic");
    if (!root)
      return;
    const baseline = required(traffic_evidence_default.data[0]);
    const count = required(root.querySelector("#comparison-count"));
    const countBar = required(root.querySelector("#count-bar"));
    const time = required(root.querySelector("#comparison-time"));
    const timeBar = required(root.querySelector("#time-bar"));
    const caption = required(root.querySelector("#comparison-caption"));
    const name = required(root.querySelector("#period-name"));
    const period = required(root.querySelector("#period-time"));
    const announcement = required(root.querySelector("#comparison-announcement"));
    const buttons = root.querySelectorAll("[data-window]");
    const format = (n) => n.toLocaleString("en-US");
    for (const button of buttons)
      button.addEventListener("click", () => {
        const index = Number(button.dataset.window);
        const row = required(traffic_evidence_default.data[index]);
        count.textContent = format(row.searches);
        countBar.style.height = `${row.searches / 6000 * 100}%`;
        required(countBar.parentElement).style.setProperty("--fill", `${row.searches / 6000 * 100}%`);
        time.replaceChildren(document.createTextNode(`${row.p50_ms.toFixed(1)} `));
        const unit = document.createElement("span");
        unit.textContent = "ms";
        time.append(unit);
        timeBar.style.width = `${row.p50_ms / 5 * 100}%`;
        caption.textContent = `About ${Math.round(row.searches / baseline.searches)} times as many searches`;
        name.textContent = index === 7 ? "Busiest window" : "Voting picks up";
        period.textContent = index === 7 ? "18:30–18:45 UTC" : "17:30–17:45 UTC";
        for (const item of buttons)
          item.setAttribute("aria-pressed", String(item === button));
        announcement.textContent = `${name.textContent}: ${format(row.searches)} searches, median ${row.p50_ms.toFixed(1)} milliseconds. Before voting: 163 searches, median 4.0 milliseconds.`;
      });
    const table = required(root.querySelector("#comparison-table"));
    table.replaceChildren();
    for (const row of traffic_evidence_default.data) {
      const tr = document.createElement("tr");
      for (const value of [row.window.slice(11, 16), format(row.searches), row.p50_ms.toFixed(2)]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      table.append(tr);
    }
  }

  // docs/blog-assets/typeahead-sequel/interactive/architecture.ts
  function initArchitecture() {
    const root = document.querySelector("#architecture");
    if (!root)
      return;
    const action = required(root.querySelector("#name-action"));
    const reset = required(root.querySelector("#name-reset"));
    const stored = required(root.querySelector("#stored-name"));
    const version = required(root.querySelector("#index-version"));
    const slip = required(root.querySelector("#correction-slip"));
    const empty = required(root.querySelector("#empty-slip"));
    const explanation = required(root.querySelector("#name-explanation"));
    const alex = required(root.querySelector("#alex-result"));
    const ari = required(root.querySelector("#ari-result"));
    const card = required(root.querySelector(".index-card"));
    let state = "original";
    let transition;
    let generation = 0;
    function render() {
      if (!root)
        return;
      root.dataset.state = state;
      const changed = state !== "original";
      const overlay = state === "changed";
      stored.textContent = state === "rebuilt" ? "Ari" : "Alex";
      version.textContent = state === "rebuilt" ? "Newly prepared index" : "Original index";
      slip.setAttribute("aria-hidden", String(!overlay));
      empty.textContent = state === "rebuilt" ? "Change included in the index" : "No changes to apply";
      alex.textContent = changed ? "No match" : "Alex";
      ari.textContent = changed ? "Ari" : "No match";
      alex.className = changed ? "result-empty" : "result-match";
      ari.className = changed ? "result-match" : "result-empty";
      action.textContent = state === "original" ? "Change Alex to Ari" : "Build the next index";
      action.hidden = state === "rebuilt";
      action.disabled = false;
      reset.hidden = state === "original";
      explanation.textContent = state === "original" ? "Alex is in the prepared index. Try changing the display name." : state === "changed" ? "The recent change corrects the old index: Ari is findable now, and Alex no longer matches. The next build can happen later." : "The new index includes Ari. The separate correction is no longer needed, and search results stay the same.";
    }
    action.addEventListener("click", async () => {
      if (state === "original") {
        state = "changed";
        render();
        if (!reducedMotion.matches)
          slip.animate([
            { transform: "translateY(18px) rotate(3deg)", opacity: 0 },
            { transform: "translateY(0) rotate(0)", opacity: 1 }
          ], { duration: 420, easing: "cubic-bezier(.22,1,.36,1)" });
        return;
      }
      if (state !== "changed")
        return;
      action.disabled = true;
      action.textContent = "Preparing the new index…";
      const token = ++generation;
      if (!reducedMotion.matches) {
        const from = slip.getBoundingClientRect();
        const to = card.getBoundingClientRect();
        transition = slip.animate([
          { transform: "translate(0,0) scale(1)", opacity: 1 },
          { transform: `translate(${to.x - from.x}px,${to.y - from.y}px) scale(.94)`, opacity: 1, offset: 0.8 },
          { transform: `translate(${to.x - from.x}px,${to.y - from.y}px) scale(.94)`, opacity: 0 }
        ], { duration: 900, easing: "cubic-bezier(.4,0,.2,1)", fill: "forwards" });
        await transition.finished.catch(() => {
          return;
        });
      }
      if (token !== generation)
        return;
      state = "rebuilt";
      transition?.cancel();
      transition = undefined;
      render();
    });
    reset.addEventListener("click", () => {
      generation++;
      transition?.cancel();
      transition = undefined;
      state = "original";
      render();
    });
    render();
  }

  // docs/blog-assets/typeahead-sequel/interactive/main.ts
  initTraffic();
  initArchitecture();
})();
