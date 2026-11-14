# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib"]
# ///
import datetime as dt
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

root = Path(__file__).resolve().parent
rows = [json.loads(line) for line in (root / "observations.jsonl").read_text().splitlines()]
start = dt.datetime(2026, 11, 5, 4, 0, tzinfo=dt.UTC).timestamp()
finish = max(row["at"] for row in rows)
qualified = 1793852105
cutover = dt.datetime(2026, 11, 5, 4, 26, 8, tzinfo=dt.UTC)
fig, ax = plt.subplots(figsize=(11, 4.2), layout="constrained")
colors = {"a":"#64748b", "b":"#2563eb"}
for target, label in [("a", "A · original hourly recycling"), ("b", "B · continuous poller")]:
    samples = [row for row in rows if row["target"] == target and row["at"] >= start
               and (target == "a" or row["at"] >= qualified)]
    xs, ys = [], []
    previous = None
    for row in samples:
        if previous is not None and row["at"] - previous > 30:
            xs.append(dt.datetime.fromtimestamp(row["at"]-1, dt.UTC)); ys.append(math.nan)
        xs.append(dt.datetime.fromtimestamp(row["at"], dt.UTC))
        ys.append(row.get("data",{}).get("sync_lag_s", math.nan))
        previous = row["at"]
    ax.plot(xs, ys, color=colors[target], linewidth=1.3, label=label)
    failed = [row for row in samples if row.get("data",{}).get("ready") is False]
    if failed:
        ax.scatter([dt.datetime.fromtimestamp(r["at"],dt.UTC) for r in failed],
                   [r["data"]["sync_lag_s"] for r in failed], color="#dc2626", s=15, zorder=5)
ax.axhline(120, color="#dc2626", linestyle="--", linewidth=1, label="Readiness limit · 120 s")
ax.axvline(cutover, color="#16a34a", linewidth=1.3)
ax.text(cutover, .98, "  Production → B", color="#15803d", fontsize=9,
        transform=ax.get_xaxis_transform(), va="top")
renewal_file = root / "budget-after-renewal.json"
if renewal_file.exists():
    renewed = json.loads(renewal_file.read_bytes())["source-budget.json"]["window_started_at"]
    at = dt.datetime.fromtimestamp(renewed,dt.UTC)
    ax.axvline(at, color="#7c3aed", linewidth=1, linestyle=":")
    ax.text(at, .98, "  B quota renews", color="#6d28d9", fontsize=9,
            transform=ax.get_xaxis_transform(), va="top")
ax.set(title="Source freshness across the poller rollout", ylabel="Source watermark lag (seconds)")
ax.set_ylim(0, 210)
ax.set_xlim(dt.datetime.fromtimestamp(start,dt.UTC), dt.datetime.fromtimestamp(finish,dt.UTC))
ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[0,15,30,45], tz=dt.UTC))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=dt.UTC))
ax.set_xlabel("November 5, 2026 · UTC · read-only samples every 15 seconds")
ax.grid(axis="y", color="#e2e8f0", linewidth=.7)
ax.spines[["top","right"]].set_visible(False)
ax.legend(loc="upper center", bbox_to_anchor=(.5,-.23), ncol=3, frameon=False, fontsize=9)
fig.savefig(root / "readiness.png", dpi=160)
fig.savefig(root / "readiness.svg")
