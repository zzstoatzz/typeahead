#!/usr/bin/env -S uv run --no-project python
"""Writes the pictures in docs/how-it-works.md. Run it from anywhere.

Every node names an icon slot. ICONS holds the bundled default for each slot,
drawn on a 24 by 24 grid. A slot is the one place to change what a thing looks
like in all five pictures.
"""
from pathlib import Path

HERE = Path(__file__).parent

ICONS = {
    "app": '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18h2"/>',
    "worker": '<path d="M7.5 18.5a4.5 4.5 0 0 1-.6-8.96 5.5 5.5 0 0 1 10.6 1.2 3.9 3.9 0 0 1-.5 7.76z"/>',
    "transport": '<path d="M3 12h6l5-6h6M9 12l5 6h6"/><path d="M17.5 3.5 20 6l-2.5 2.5M17.5 15.5 20 18l-2.5 2.5"/>',
    "route": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h4"/>',
    "store": '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.66 3.13 3 7 3s7-1.34 7-3V6"/><path d="M5 12c0 1.66 3.13 3 7 3s7-1.34 7-3"/>',
    "engine": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20.5 20.5-5.2-5.2"/>',
    "bridge": '<path d="M12 4v15M6 13l6 6 6-6"/>',
    "relay": '<circle cx="12" cy="12" r="1.8"/><path d="M8.1 8.1a5.5 5.5 0 0 0 0 7.8M15.9 8.1a5.5 5.5 0 0 1 0 7.8M5.3 5.3a9.5 9.5 0 0 0 0 13.4M18.7 5.3a9.5 9.5 0 0 1 0 13.4"/>',
    "plc": '<rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="8.5" cy="10.5" r="2"/><path d="M5.5 16c.5-1.5 1.6-2.2 3-2.2s2.5.7 3 2.2M14.5 10h4M14.5 14h4"/>',
    "labeler": '<path d="M3.5 11.5v-8h8l9 9-8 8z"/><circle cx="7.8" cy="7.8" r="1.3"/>',
    "appview": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c.8-3.8 3.6-5.7 7.5-5.7s6.700 1.900 7.500 5.700"/>',
    "control": '<path d="M4 7h9M19 7h1M4 17h1M11 17h9"/><circle cx="16" cy="7" r="2.5"/><circle cx="8" cy="17" r="2.5"/>',
    "stats": '<path d="M3.500 20.500h17M7 17v-6M12 17V5M17 17v-9"/>',
    "image": '<rect x="3" y="4.500" width="18" height="15" rx="2.500"/><circle cx="8.500" cy="9.500" r="1.600"/><path d="m20.500 15.500-4.500-4.500-8.500 8.500"/>',
    "slot": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.500h.01M7 16.500h.01M11 7.500h6M11 16.500h6"/>',
}

STYLE = """  <style>
    svg { --fg: #1f2328; --muted: #59636e; --line: #8c959f; --panel: #f6f8fa; --accent: #0969da; --accent-soft: #ddf4ff; --bg: #ffffff; }
    @media (prefers-color-scheme: dark) {
      svg { --fg: #e6edf3; --muted: #9198a1; --line: #6e7681; --panel: #161b22; --accent: #4493f8; --accent-soft: #121d2f; --bg: #0d1117; }
    }
    .ground { fill: var(--bg); }
    .box { fill: var(--panel); stroke: var(--line); stroke-width: 1.5; }
    .core { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 2; }
    .slot { fill: none; stroke: var(--line); stroke-width: 1.5; stroke-dasharray: 6 5; }
    .name { fill: var(--fg); font-size: 17px; font-weight: 600; }
    .sub { fill: var(--muted); font-size: 14px; }
    .body { fill: var(--fg); font-size: 15px; }
    .note { fill: var(--muted); font-size: 14px; }
    .wire { stroke: var(--line); stroke-width: 2; fill: none; }
    .later { stroke: var(--line); stroke-width: 2; fill: none; stroke-dasharray: 5 5; }
    .flow { stroke: var(--accent); stroke-width: 2.5; fill: none; }
    .head { fill: var(--line); }
    .flowhead { fill: var(--accent); }
    .label { fill: var(--fg); font-size: 14px; }
    .icon { fill: none; stroke: var(--muted); stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
    .icon.on { stroke: var(--accent); }
  </style>
"""


def icon(slot, x, y, size=28, on=False):
    scale = size / 24
    return f'  <g class="icon{" on" if on else ""}" transform="translate({x} {y}) scale({scale:.3f})">{ICONS[slot]}</g>\n'


def node(slot, x, y, w, name, sub, core=False, h=60):
    """An icon, a name and one line under it."""
    cls = "core" if core else "box"
    return (
        f'  <rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>\n'
        + icon(slot, x + 14, y + (h - 28) / 2, on=core)
        + f'  <text class="name" x="{x + 54}" y="{y + h / 2 - 3}">{name}</text>\n'
        + f'  <text class="sub" x="{x + 54}" y="{y + h / 2 + 17}">{sub}</text>\n'
    )


def panel(slot, y, h, name, lines, note=None, x=50, w=500, core=True):
    """A titled box with a few lines of explanation."""
    out = f'  <rect class="{"core" if core else "box"}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>\n'
    out += icon(slot, x + 20, y + 14, on=core)
    out += f'  <text class="name" x="{x + 60}" y="{y + 34}">{name}</text>\n'
    at = y + 68
    for line in lines:
        out += f'  <text class="body" x="{x + 24}" y="{at}">{line}</text>\n'
        at += 26
    if note:
        out += f'  <text class="note" x="{x + 24}" y="{at - 2}">{note}</text>\n'
    return out


def down(x, y1, y2, cls="wire"):
    head = "flowhead" if cls == "flow" else "head"
    return (
        f'  <path class="{cls}" d="M{x} {y1} V{y2 - 12}"/>\n'
        f'  <polygon class="{head}" points="{x},{y2 - 2} {x - 7},{y2 - 14} {x + 7},{y2 - 14}"/>\n'
    )


def up(x, y1, y2, cls="wire"):
    head = "flowhead" if cls == "flow" else "head"
    return (
        f'  <path class="{cls}" d="M{x} {y1} V{y2 + 12}"/>\n'
        f'  <polygon class="{head}" points="{x},{y2 + 2} {x - 7},{y2 + 14} {x + 7},{y2 + 14}"/>\n'
    )


def right(x1, x2, y):
    return (
        f'  <path class="wire" d="M{x1} {y} H{x2 - 12}"/>\n'
        f'  <polygon class="head" points="{x2 - 2},{y} {x2 - 14},{y - 7} {x2 - 14},{y + 7}"/>\n'
    )


def text(cls, x, y, value, anchor=None):
    attr = f' text-anchor="{anchor}"' if anchor else ""
    return f'  <text class="{cls}" x="{x}" y="{y}"{attr}>{value}</text>\n'


def group(x, y, w, h):
    return f'  <rect class="slot" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>\n'


def write(name, height, title, desc, body):
    (HERE / f"{name}.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 {height}" role="img" '
        f'aria-labelledby="{name}-title {name}-desc" '
        "font-family=\"ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif\">\n"
        f'  <title id="{name}-title">{title}</title>\n'
        f'  <desc id="{name}-desc">{desc}</desc>\n'
        f"{STYLE}\n"
        f'  <rect class="ground" width="600" height="{height}" rx="12"/>\n'
        f"{body}</svg>\n"
    )


def search():
    b = node("app", 150, 20, 300, "An app", "someone types in a search field")
    b += down(300, 80, 132) + text("label", 314, 110, 'search for "nat"')
    b += panel("worker", 132, 130, "Public Worker", [
        "Checks the parameters and the rate limit.",
        "Answers from its cache if it saw the same query",
        "in the last 60 seconds.",
    ])
    b += down(300, 262, 324) + text("label", 314, 297, "on a cache miss")
    b += panel("transport", 324, 130, "Search transport", [
        "Reads the route record to learn",
        "which slot is serving.",
    ], note="its copy is at most 1 second old", w=320)
    b += right(370, 392, 389)
    b += node("route", 392, 359, 188, "Route record", "which slot serves")
    b += down(210, 454, 542, "flow") + text("label", 226, 490, "the query")
    b += group(20, 512, 560, 196)
    b += panel("engine", 542, 122, "Search engine", [
        "Finds handles and names that start with the query.",
        "Ranks them, an exact handle first, and returns profiles.",
    ])
    b += icon("slot", 50, 674, size=22) + text("name", 82, 691, "The serving slot")
    b += text("note", 20, 740, "The answer returns the same way, and the Worker caches it for 60 seconds.")
    write("search", 762, "A search request",
          "An app sends a search to the public Worker, which checks parameters and rate limits and answers "
          "from its cache when it can. On a cache miss the search transport reads the route record to learn "
          "which slot is serving and sends the query to that slot's search engine, which matches handles and "
          "names by prefix, ranks them and returns profiles.", b)


def slot():
    b = node("relay", 20, 20, 164, "Relay", "account events")
    b += node("plc", 196, 20, 208, "PLC directory", "identity changes")
    b += node("labeler", 416, 20, 164, "Labeler", "labels")
    b += node("appview", 20, 94, 272, "Bluesky AppView", "profiles and counts")
    b += node("worker", 308, 94, 272, "Worker", "policy, index requests")
    b += down(300, 154, 244) + text("label", 314, 190, "the store follows or asks;")
    b += text("label", 314, 208, "nothing pushes into a slot")
    b += group(20, 218, 560, 556)
    b += panel("store", 244, 182, "Store process", [
        "One record per account, every field, on disk.",
        "Applies events, verifies handles, refreshes profiles.",
        "Applies moderation policy.",
        "Keeps a log of every change it makes.",
    ], note="its port is private: health checks only")
    b += down(300, 426, 504, "flow") + icon("bridge", 316, 440, size=20, on=True)
    b += text("label", 342, 455, "index bridge:") + text("label", 316, 477, "sends each changed account")
    b += panel("engine", 504, 156, "Search engine", [
        "One document per account: what a result shows.",
        "Prefix search and ranking (Tantivy).",
        "Stops answering if the store falls 2 minutes behind.",
    ], note="its port is the only way into a slot")
    b += icon("slot", 50, 680, size=22) + text("name", 82, 697, "One slot")
    b += text("note", 50, 722, "A Fly machine with its own disk. A supervisor restarts")
    b += text("note", 50, 742, "either process without stopping the other.")
    b += up(500, 820, 660) + text("label", 486, 802, "search, profile and count requests", "end")
    b += node("worker", 150, 820, 400, "Worker", "through the search transport")
    write("slot", 900, "Inside one slot",
          "A slot is one machine running a store process and a search engine. The store follows the relay, "
          "the PLC directory and the labeler, asks Bluesky for profiles and the Worker for policy, and keeps "
          "one record per account on disk. An index bridge sends each changed account to the search engine, "
          "which holds one document per account and answers search, profile and count requests from the Worker.", b)


def accounts():
    b = node("relay", 20, 20, 164, "Relay", "account event")
    b += node("plc", 196, 20, 208, "PLC directory", "identity change")
    b += node("worker", 416, 20, 164, "Worker", "index request")
    b += down(300, 80, 160) + text("label", 314, 112, "any of these starts it")
    b += group(20, 134, 560, 596)
    steps = [
        ("store", "Record", "The store creates or updates the account's record."),
        ("plc", "Handle", "Verifies the handle against the account's DID."),
        ("appview", "Profile", "Asks Bluesky's AppView for name, avatar and counts."),
        ("labeler", "Policy", "Applies labels, domain blocks and overrides."),
        ("bridge", "Index", "The bridge sends the account to the search engine."),
    ]
    y = 160
    for index, (glyph, name, line) in enumerate(steps):
        b += f'  <rect class="box" x="50" y="{y}" width="500" height="66" rx="10"/>\n'
        b += icon(glyph, 66, y + 19)
        b += text("name", 108, y + 28, f"{index + 1}. {name}") + text("body", 108, y + 51, line)
        b += down(300, y + 66, y + 92, "flow" if index == len(steps) - 1 else "wire")
        y += 92
    b += f'  <rect class="core" x="50" y="{y}" width="500" height="66" rx="10"/>\n'
    b += icon("engine", 66, y + 19, on=True)
    b += text("name", 108, y + 28, "Searchable") + text("body", 108, y + 51, "The search engine can now return the account.")
    b += text("note", 50, y + 98, "Each slot does all of this on its own, from the same sources.")
    write("accounts", 750, "How an account gets in",
          "An account event from the relay, an identity change from the PLC directory or an index request "
          "from the Worker starts the work. Inside a slot, the store creates or updates the record, verifies "
          "the handle, asks Bluesky for the profile, applies moderation policy, and the index bridge sends "
          "the account to the search engine, after which it is searchable.", b)


def route():
    b = node("worker", 150, 20, 300, "Public Worker", "search requests")
    b += down(200, 80, 114)
    b += node("transport", 50, 114, 300, "Search transport", "follows the route record", core=True, h=66)
    b += right(350, 392, 147)
    b += f'  <rect class="box" x="392" y="102" width="188" height="90" rx="10"/>\n'
    b += icon("route", 406, 112) + text("name", 446, 131, "Route record")
    b += text("sub", 408, 158, "current: slot A") + text("sub", 408, 178, "previous: slot B")
    b += down(230, 180, 300, "flow") + text("label", 216, 226, "searches", "end")
    b += '  <path class="later" d="M300 180 V218 H520 V288"/>\n'
    b += '  <polygon class="head" points="520,298 513,286 527,286"/>\n'
    b += text("label", 366, 240, "after a flip")
    for x, name in ((20, "Slot A, serving"), (310, "Slot B, standby")):
        b += group(x, 250, 270, 264)
        b += icon("slot", x + 20, 264, size=22) + text("name", x + 52, 281, name)
        b += node("engine", x + 20, 300, 230, "Search engine", "answers searches", core=True)
        b += up(x + 135, 400, 360, "flow")
        b += node("store", x + 20, 400, 230, "Store process", "keeps the accounts", core=True)
        b += up(x + 135, 554, 460)
    b += '  <rect class="box" x="20" y="554" width="560" height="70" rx="10"/>\n'
    for i, glyph in enumerate(("relay", "plc", "labeler", "appview", "control")):
        b += icon(glyph, 40 + i * 30, 566, size=20)
    b += text("name", 200, 582, "The sources")
    b += text("sub", 40, 608, "each slot follows them on its own; the slots never talk to each other")
    for i, line in enumerate((
        "A flip is one write to the route record. It is allowed only when the",
        "standby reports the same build and policy as the serving slot,",
        "data less than 2 minutes behind, and an account count within 2%.",
    )):
        b += text("note", 20, 660 + i * 22, line)
    write("route", 730, "Two slots and the route",
          "The public Worker sends searches to the search transport, which follows a route record naming the "
          "current and previous slot. Slot A serves and slot B stands by; each runs a store process and a "
          "search engine and follows the relay, the PLC directory, the labeler, Bluesky and Worker policy on "
          "its own. A flip is one write to the route record, allowed only when the standby matches the "
          "serving slot.", b)


def outside():
    b = group(20, 20, 560, 318)
    b += text("name", 40, 52, "Cloudflare") + text("note", 140, 52, "everything except the accounts")
    for i, (glyph, name, sub) in enumerate((
        ("worker", "Public Worker", "API, pages, limits, cache"),
        ("transport", "Search transport", "finds the serving slot"),
        ("route", "Route record", "R2: which slot serves"),
        ("control", "Control database", "D1: policy, index requests"),
        ("stats", "Statistics", "D1 and KV: usage counts"),
        ("image", "Image renderer", "share images and icons"),
    )):
        b += node(glyph, 40 + (i % 2) * 270, 72 + (i // 2) * 84, 250, name, sub, h=68)
    b += group(20, 358, 560, 134)
    b += text("name", 40, 390, "Fly") + text("note", 76, 390, "the accounts and the search index, twice")
    b += node("slot", 40, 408, 250, "Slot A", "store + search engine", core=True, h=68)
    b += node("slot", 310, 408, 250, "Slot B", "store + search engine", core=True, h=68)
    b += group(20, 512, 560, 216)
    b += text("name", 40, 544, "Run by others") + text("note", 178, 544, "read by the slots, never written")
    for i, (glyph, name, sub) in enumerate((
        ("relay", "Relay", "account events"),
        ("plc", "PLC directory", "identity changes"),
        ("labeler", "Labeler", "moderation labels"),
        ("appview", "Bluesky AppView", "profiles and counts"),
    )):
        b += node(glyph, 40 + (i % 2) * 270, 564 + (i // 2) * 76, 250, name, sub)
    write("outside", 748, "What runs where",
          "Cloudflare holds the public Worker, the search transport, the route record in R2, the control "
          "database in D1, statistics in D1 and KV, and the image renderer. Fly holds the two slots, each a "
          "store process and a search engine. The relay, the PLC directory, the labeler and the Bluesky "
          "AppView are run by others and only read.", b)


if __name__ == "__main__":
    for draw in (search, slot, accounts, route, outside):
        draw()
