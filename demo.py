#!/usr/bin/env python3
"""Run the demo story in the terminal: python demo.py"""
import asyncio

import scenario
from core.world import World


def show(row: dict) -> None:
    if row["kind"] == "event":
        prefix = "\n==" if row["event"] == "step" else "  ·"
        print(f"{prefix} {row['text']}")
    else:
        mark = "✔" if row["result"] == "delivered" else "✘"
        who = f"{row['sender'].split('@')[0]} → {row['recipient'].split('@')[0]}"
        extra = "" if row["result"] == "delivered" else f"  BLOCKED: {row['reason']}"
        print(f"  {mark} {row['type']:<18} {who}{extra}")


async def main() -> None:
    world = World()
    world.listeners.append(show)
    await scenario.run(world, pause=0)
    print("\nSource trust:")
    for t in world.trust.table():
        print(f"  {t['source']:<22} {t['score']:.2f}  {t['label']}")


if __name__ == "__main__":
    asyncio.run(main())
