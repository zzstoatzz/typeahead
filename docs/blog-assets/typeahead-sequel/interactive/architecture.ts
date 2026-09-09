import { required, reducedMotion } from "./dom";

export function initArchitecture() {
  const root = document.querySelector<HTMLElement>("#architecture");
  if (!root) return;
  const action = required(root.querySelector<HTMLButtonElement>("#name-action"));
  const reset = required(root.querySelector<HTMLButtonElement>("#name-reset"));
  const stored = required(root.querySelector<HTMLElement>("#stored-name"));
  const version = required(root.querySelector<HTMLElement>("#index-version"));
  const slip = required(root.querySelector<HTMLElement>("#correction-slip"));
  const empty = required(root.querySelector<HTMLElement>("#empty-slip"));
  const explanation = required(root.querySelector<HTMLElement>("#name-explanation"));
  const alex = required(root.querySelector<HTMLElement>("#alex-result"));
  const ari = required(root.querySelector<HTMLElement>("#ari-result"));
  const card = required(root.querySelector<HTMLElement>(".index-card"));
  let state: "original" | "changed" | "rebuilt" = "original";
  let transition: Animation | undefined;
  let generation = 0;
  function render() {
    if (!root) return;
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
    explanation.textContent = state === "original"
      ? "Alex is in the prepared index. Try changing the display name."
      : state === "changed"
        ? "The recent change corrects the old index: Ari is findable now, and Alex no longer matches. The next build can happen later."
        : "The new index includes Ari. The separate correction is no longer needed, and search results stay the same.";
  }
  action.addEventListener("click", async () => {
    if (state === "original") {
      state = "changed"; render();
      if (!reducedMotion.matches) slip.animate([
        { transform: "translateY(18px) rotate(3deg)", opacity: 0 },
        { transform: "translateY(0) rotate(0)", opacity: 1 },
      ], { duration: 420, easing: "cubic-bezier(.22,1,.36,1)" });
      return;
    }
    if (state !== "changed") return;
    action.disabled = true;
    action.textContent = "Preparing the new index…";
    const token = ++generation;
    if (!reducedMotion.matches) {
      const from = slip.getBoundingClientRect();
      const to = card.getBoundingClientRect();
      transition = slip.animate([
        { transform: "translate(0,0) scale(1)", opacity: 1 },
        { transform: `translate(${to.x - from.x}px,${to.y - from.y}px) scale(.94)`, opacity: 1, offset: .8 },
        { transform: `translate(${to.x - from.x}px,${to.y - from.y}px) scale(.94)`, opacity: 0 },
      ], { duration: 900, easing: "cubic-bezier(.4,0,.2,1)", fill: "forwards" });
      await transition.finished.catch(() => undefined);
    }
    if (token !== generation) return;
    state = "rebuilt"; transition?.cancel(); transition = undefined; render();
  });
  reset.addEventListener("click", () => {
    generation++; transition?.cancel(); transition = undefined; state = "original"; render();
  });
  render();
}
