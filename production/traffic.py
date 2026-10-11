"""Steady checkout traffic so the service always has fresh request telemetry.

OnCall refuses to call a silent service healthy, so production needs traffic.
Real deployments get it from users; this keeps a low-traffic demo honest.
"""
from __future__ import annotations

import argparse
import asyncio
import random

import httpx


async def run(url: str, rps: float) -> None:
    interval = 1 / rps
    async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
        while True:
            try:
                await client.post(url)
            except httpx.HTTPError:
                pass  # failures are visible in the service's own metrics
            await asyncio.sleep(interval * random.uniform(.6, 1.4))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--rps', type=float, default=2.0)
    args = parser.parse_args()
    if not 0.1 <= args.rps <= 20:
        raise SystemExit('--rps must be between 0.1 and 20')
    asyncio.run(run(args.url, args.rps))


if __name__ == '__main__':
    main()
