export function required<T>(value: T | null | undefined): T {
  if (value == null) throw new Error("An expected article element or observation is missing");
  return value;
}
export function text(tag: "div" | "p" | "span" | "strong" | "li" | "small", content: string, className = "") {
  const node = document.createElement(tag);
  node.textContent = content;
  node.className = className;
  return node;
}
type SvgAttributes = Record<string, string | number>;
export function svg<K extends keyof SVGElementTagNameMap>(tag: K, attributes: SvgAttributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}
export function svgText(x: number, y: number, content: string, className: string, anchor = "start") {
  const node = svg("text", { x, y, class: className, "text-anchor": anchor });
  node.textContent = content;
  return node;
}
export const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
