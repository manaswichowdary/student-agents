#!/usr/bin/env python3
"""Record the demo scenario into a static, shareable replay page: web/replay.html

  python tools/export_replay.py

It runs the real agents, takes a snapshot of every phone, the trust panel and the log after each step, and
embeds them in a copy of the web UI that replays them. Nothing is simulated in the page itself.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scenario  # noqa: E402
from core.util import fmt_time  # noqa: E402
from core.world import World  # noqa: E402
from data import seed  # noqa: E402


def snapshot(world: World) -> dict:
    phones = []
    for email in seed.PHONES:
        if email in world.agents:
            phones.append({"verified": True, **world.agents[email].view()})
        else:
            sent = next((o for o in reversed(world.identity.outbox) if o["to"] == email), None)
            phones.append({"verified": False, "email": email, "code_sent": bool(sent),
                           "mock_code": sent["code"] if sent else None})
    return {"clock": fmt_time(world.clock.now()), "phones": phones, "trust": world.trust.table(),
            "n": len(world.log), "names": {e: a.first for e, a in world.agents.items()}}


async def record() -> tuple[list, list]:
    world = World()
    snaps = [snapshot(world)]
    world.listeners.append(lambda row: snaps.append(snapshot(world)))
    await scenario.run(world, pause=0)
    snaps.append(snapshot(world))
    return world.log, snaps


def build(log: list, snaps: list) -> str:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    script = re.search(r"<script>(.*?)</script>", body, re.S).group(1)
    body = body.replace(f"<script>{script}</script>", "")
    # theme blocks that also respect an explicit light or dark choice
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{ :root \{(.*?)\} \}", style, re.S).group(1)
    style = style.replace(re.search(r"@media \(prefers-color-scheme: dark\) \{ :root \{.*?\} \}", style, re.S).group(0),
                          f'@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{{dark}}} }}\n'
                          f':root[data-theme="dark"] {{{dark}}}')
    body = body.replace('<button id="digest" type="button">Send 6 pm digest</button>',
                        '<button id="digest" type="button">Next step</button>')
    body = body.replace('<button class="primary" id="run" type="button">Run demo scenario</button>',
                        '<button class="primary" id="run" type="button">Play demo</button>')
    body = body.replace('<button id="reset" type="button">Reset</button>',
                        '<button id="reset" type="button">Restart</button>')
    body = body.replace("<p>Personal agents that share opportunities only between students who both said yes.</p>",
                        "<p>Personal agents that share opportunities only between students who both said yes. "
                        "This page replays a real run of the agents; tap any message to see what was shared.</p>")
    script = script.replace("connect();\nrefresh();", "")
    data = json.dumps({"log": log, "snaps": snaps}, ensure_ascii=False).replace("</", "<\\/")
    replay = """
const REC = __DATA__;
const LISTINGS = [];
let at = 0, playing = null;
api = async (path) => {
  if (path === "/api/state") {
    const s = REC.snaps[at];
    return { ...s, running: !!playing, log: REC.log.slice(0, s.n), listings: LISTINGS, event: {} };
  }
  throw new Error("This page is a recording. Run the app from GitHub to try this yourself.");
};
function stop() { clearInterval(playing); playing = null; refresh() }
function step() { if (at >= REC.snaps.length - 1) return stop(); at++; refresh() }
$("run").onclick = () => {
  if (playing) return stop();
  if (at >= REC.snaps.length - 1) at = 0;
  playing = setInterval(step, matchMedia("(prefers-reduced-motion: reduce)").matches ? 300 : 650);
  refresh();
};
$("digest").onclick = () => { if (playing) stop(); step() };
$("reset").onclick = () => { if (playing) stop(); at = 0; refresh() };
const _refresh = refresh;
refresh = async () => { await _refresh(); $("run").disabled = false; $("run").textContent = playing ? "Pause" : (at ? "Play" : "Play demo");
  $("clock").textContent = `Step ${at} of ${REC.snaps.length - 1}`;
  document.querySelectorAll('.phone [data-a="find"], .phone [data-a="squad"], .phone select').forEach(e => e.hidden = true); };
at = REC.snaps.length - 1;
refresh();
""".replace("__DATA__", data)
    return (f'<title>Student Agents</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n'
            '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@600;800'
            '&family=Source+Sans+3:wght@400;600&family=JetBrains+Mono:wght@400;500&display=swap">\n'
            f"<style>{style}</style>\n{body}\n<script>{script}\n{replay}</script>\n")


def main() -> int:
    log, snaps = asyncio.run(record())
    out = ROOT / "web" / "replay.html"
    out.write_text(build(log, snaps), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes, {len(snaps)} steps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
