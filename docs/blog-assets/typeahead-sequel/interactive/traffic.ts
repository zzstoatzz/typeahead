import evidence from "../traffic-evidence.json";
import { required } from "./dom";

export function initTraffic() {
  const root = document.querySelector<HTMLElement>("#traffic");
  if (!root) return;
  const baseline = required(evidence.data[0]);
  const count = required(root.querySelector<HTMLElement>("#comparison-count"));
  const countBar = required(root.querySelector<HTMLElement>("#count-bar"));
  const time = required(root.querySelector<HTMLElement>("#comparison-time"));
  const timeBar = required(root.querySelector<HTMLElement>("#time-bar"));
  const caption = required(root.querySelector<HTMLElement>("#comparison-caption"));
  const name = required(root.querySelector<HTMLElement>("#period-name"));
  const period = required(root.querySelector<HTMLElement>("#period-time"));
  const announcement = required(root.querySelector<HTMLElement>("#comparison-announcement"));
  const buttons = root.querySelectorAll<HTMLButtonElement>("[data-window]");
  const format = (n: number) => n.toLocaleString("en-US");
  for (const button of buttons) button.addEventListener("click", () => {
    const index = Number(button.dataset.window);
    const row = required(evidence.data[index]);
    count.textContent = format(row.searches);
    countBar.style.height = `${row.searches / 6000 * 100}%`;
    required(countBar.parentElement).style.setProperty("--fill", `${row.searches / 6000 * 100}%`);
    time.replaceChildren(document.createTextNode(`${row.p50_ms.toFixed(1)} `));
    const unit = document.createElement("span"); unit.textContent = "ms"; time.append(unit);
    timeBar.style.width = `${row.p50_ms / 5 * 100}%`;
    caption.textContent = `About ${Math.round(row.searches / baseline.searches)} times as many searches`;
    name.textContent = index === 7 ? "Busiest window" : "Voting picks up";
    period.textContent = index === 7 ? "18:30–18:45 UTC" : "17:30–17:45 UTC";
    for (const item of buttons) item.setAttribute("aria-pressed", String(item === button));
    announcement.textContent = `${name.textContent}: ${format(row.searches)} searches, median ${row.p50_ms.toFixed(1)} milliseconds. Before voting: 163 searches, median 4.0 milliseconds.`;
  });
  const table = required(root.querySelector<HTMLTableSectionElement>("#comparison-table"));
  table.replaceChildren();
  for (const row of evidence.data) {
    const tr = document.createElement("tr");
    for (const value of [row.window.slice(11, 16), format(row.searches), row.p50_ms.toFixed(2)]) {
      const td = document.createElement("td"); td.textContent = value; tr.append(td);
    }
    table.append(tr);
  }
}
