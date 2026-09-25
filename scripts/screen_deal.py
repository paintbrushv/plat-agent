"""Screen a deal using the SDK orchestrator.

Usage:
    python3 scripts/screen_deal.py [deal_id]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import asyncio
from plat_agent.orchestrator.runner import screen_deal


if __name__ == "__main__":
    deal_id = sys.argv[1] if len(sys.argv) > 1 else "TEST-001"
    print(asyncio.run(screen_deal(deal_id)))
