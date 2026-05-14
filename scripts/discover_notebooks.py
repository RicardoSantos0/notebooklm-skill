#!/usr/bin/env python3
"""
Discover all notebooks on NotebookLM homepage and compare with local library.
Outputs new notebooks (present on site but not in library.json) as JSON.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from patchright.sync_api import sync_playwright
from browser_utils import BrowserFactory
from config import LIBRARY_FILE


def get_library_urls() -> set:
    if not LIBRARY_FILE.exists():
        return set()
    data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    notebooks = data.get("notebooks", data) if isinstance(data, dict) else data
    if isinstance(notebooks, dict):
        notebooks = list(notebooks.values())
    return {nb.get("url", "").rstrip("/") for nb in notebooks if nb.get("url")}


def discover(headless: bool = True):
    library_urls = get_library_urls()

    with sync_playwright() as p:
        context = BrowserFactory.launch_persistent_context(p, headless=headless)
        try:
            page = context.new_page()
            print("Navigating to NotebookLM home...", flush=True)
            page.goto("https://notebooklm.google.com/", wait_until="networkidle", timeout=30000)

            if "accounts.google.com" in page.url:
                print("ERROR: Not authenticated. Run auth_manager.py setup first.")
                sys.exit(1)

            # Wait for notebook cards to appear
            page.wait_for_timeout(2000)

            # Extract notebook links and titles
            notebooks = page.evaluate("""() => {
                const results = [];
                // Try various selectors for notebook cards
                const selectors = [
                    'a[href*="/notebook/"]',
                    '[data-notebook-id]',
                    '.notebook-card a',
                    'mat-card a[href*="notebook"]'
                ];
                const seen = new Set();
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const href = el.href || el.getAttribute('href') || '';
                        if (href.includes('/notebook/') && !seen.has(href)) {
                            seen.add(href);
                            // Walk up to find a title element
                            const card = el.closest('[class*="card"], [class*="item"], [class*="notebook"]') || el.parentElement;
                            const titleEl = card ? card.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"]') : null;
                            results.push({
                                url: href.split('?')[0],
                                name: titleEl ? titleEl.textContent.trim() : el.textContent.trim() || 'Unnamed'
                            });
                        }
                    });
                }
                return results;
            }""")

            if not notebooks:
                # Fallback: dump all hrefs containing /notebook/
                all_hrefs = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href*="notebook"]'))
                        .map(a => ({ url: a.href.split('?')[0], name: a.textContent.trim() || 'Unnamed' }))
                        .filter(x => x.url.includes('notebooklm.google.com'));
                }""")
                notebooks = all_hrefs

            # Deduplicate
            seen_urls = set()
            unique = []
            for nb in notebooks:
                url = nb["url"].rstrip("/")
                if url not in seen_urls:
                    seen_urls.add(url)
                    unique.append({"url": url, "name": nb["name"]})

            new_notebooks = [nb for nb in unique if nb["url"] not in library_urls]

            print(f"\nFound {len(unique)} notebook(s) on NotebookLM.")
            print(f"Already in library: {len(unique) - len(new_notebooks)}")
            print(f"New (not in library): {len(new_notebooks)}\n")

            if new_notebooks:
                print("NEW NOTEBOOKS:")
                for nb in new_notebooks:
                    print(f"  Name: {nb['name']}")
                    print(f"  URL:  {nb['url']}")
                    print()
            else:
                print("No new notebooks found.")

            # Output JSON for scripting
            output = {"all": unique, "new": new_notebooks}
            output_file = Path(__file__).parent.parent / "data" / "discovered_notebooks.json"
            output_file.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Results saved to: {output_file}")

            return new_notebooks

        finally:
            context.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args()
    discover(headless=not args.show_browser)
