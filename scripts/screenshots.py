"""Take the README screenshots (screenshots/*.png) from the web interface.

Optional developer tool, not needed to run ExitPilot:

    pip install playwright && playwright install chromium
    python scripts/screenshots.py                    # example loja-mista, AI as configured in .env
    python scripts/screenshots.py --example loja-lixo

It starts the web app on a free port, analyses the example like a user would and saves report.png,
market.png and migration.png. Run it after `python -m exitpilot.record` so the images show the
AI's text. Set CHROME_PATH to use an installed Chrome instead of Playwright's Chromium.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def take(example: str, out: Path, width: int = 1280, prepare: Optional[Callable] = None) -> list[Path]:
    from playwright.sync_api import sync_playwright

    from exitpilot import webapp

    server = webapp.make_server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    def clip(page, selector: str, name: str, height: int) -> None:
        """A window of `height` pixels starting at the element, under the page's sticky header."""
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("""sel => {
            const el = document.querySelector(sel), header = document.querySelector("header");
            const top = el.getBoundingClientRect().top + window.scrollY;
            window.scrollTo(0, top - (header ? header.offsetHeight : 0) - 16);
        }""", selector)
        page.wait_for_timeout(500)
        path = out / name
        page.screenshot(path=str(path))
        saved.append(path)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv("CHROME_PATH") or None)
            page = browser.new_page(viewport={"width": width, "height": 900}, device_scale_factor=2)
            if prepare:
                prepare(page)
            page.goto(url)
            page.locator(f'.example[data-name="{example}"]').click()
            page.locator("#analyse").click()
            page.wait_for_selector("#results:not(.hidden) #metrics .metric", timeout=600_000)
            page.wait_for_timeout(800)
            path = out / "report.png"
            page.screenshot(path=str(path))
            saved.append(path)

            page.locator('#tabs button[data-tab="market"]').click()
            page.wait_for_timeout(1500)
            clip(page, "#tab-market", "market.png", 1000)

            page.locator("#mg-go").click()
            page.wait_for_selector("#phase2 .metrics", timeout=120_000)
            page.wait_for_timeout(800)
            clip(page, "#phase2", "migration.png", 1000)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return saved


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Take the README screenshots from the web interface.")
    parser.add_argument("--example", default="loja-mista")
    parser.add_argument("--out", default=str(ROOT / "screenshots"))
    args = parser.parse_args(argv)
    for path in take(args.example, Path(args.out)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
