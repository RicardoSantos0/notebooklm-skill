#!/usr/bin/env python3
"""
Debug version of ask_question that:
1. Uses clipboard paste for long questions (faster than character-by-character)
2. Tries broader selectors for responses
3. Has a longer timeout
4. Dumps page content on failure
"""

import argparse
import sys
import time
import re
import json
from pathlib import Path

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent))

from patchright.sync_api import sync_playwright
from auth_manager import AuthManager
from notebook_manager import NotebookLibrary
from config import QUERY_INPUT_SELECTORS, BROWSER_PROFILE_DIR, STATE_FILE, BROWSER_ARGS, USER_AGENT

FOLLOW_UP_REMINDER = (
    "\n\nEXTREMELY IMPORTANT: Is that ALL you need to know? "
    "You can always ask another question! Think about it carefully: "
    "before you reply to the user, review their original request and this answer. "
    "If anything is still unclear or missing, ask me another comprehensive question "
    "that includes all necessary context (since each question opens a new browser session)."
)

# Broader set of response selectors to try
RESPONSE_SELECTORS_EXTENDED = [
    ".to-user-container .message-text-content",  # Primary (from config)
    "[data-message-author='bot']",
    "[data-message-author='assistant']",
    ".response-container .message-text-content",
    ".message-text-content",
    ".response-text",
    "message-text",
    ".chat-message-content",
    ".answer-text",
    # Try broader containers
    ".response-content",
    ".chat-response",
    # Try role-based
    "[role='article']",
]


def inject_cookies(context):
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                if 'cookies' in state and len(state['cookies']) > 0:
                    context.add_cookies(state['cookies'])
        except Exception as e:
            print(f"  Warning: Could not load state.json: {e}")


def ask_notebooklm(question: str, notebook_url: str, headless: bool = True, timeout_seconds: int = 180) -> str:
    auth = AuthManager()
    if not auth.is_authenticated():
        print("Not authenticated. Run: python auth_manager.py setup")
        return None

    print(f"Asking: {question[:100]}...")
    print(f"Notebook: {notebook_url}")

    playwright = None
    context = None

    try:
        playwright = sync_playwright().start()

        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            channel="chrome",
            headless=headless,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            user_agent=USER_AGENT,
            args=BROWSER_ARGS
        )

        inject_cookies(context)

        page = context.new_page()
        print("  Opening notebook...")
        page.goto(notebook_url, wait_until="domcontentloaded", timeout=30000)

        # Wait a moment for any redirects to settle
        time.sleep(3)
        current_url = page.url
        print(f"  Page URL after load: {current_url}")

        if "accounts.google.com" in current_url or "login" in current_url:
            print("  ERROR: Redirected to login page - auth may have expired")
            print("  Re-authenticate with: python auth_manager.py setup")
            return None

        if "notebooklm.google.com" not in current_url:
            print(f"  WARNING: Unexpected URL: {current_url}")
            # Wait a bit more for any further redirects
            time.sleep(5)
            current_url = page.url
            print(f"  URL after extra wait: {current_url}")

        # Wait for query input
        print("  Waiting for query input...")
        query_element = None

        for selector in QUERY_INPUT_SELECTORS:
            try:
                query_element = page.wait_for_selector(selector, timeout=15000, state="visible")
                if query_element:
                    print(f"  Found input: {selector}")
                    break
            except:
                continue

        if not query_element:
            print("  Could not find query input - dumping page content...")
            print("  Current URL:", page.url)
            # Get all textareas
            textareas = page.query_selector_all("textarea")
            print(f"  Found {len(textareas)} textareas on page")
            for i, ta in enumerate(textareas):
                print(f"    textarea[{i}]: class={ta.get_attribute('class')}, aria-label={ta.get_attribute('aria-label')}")
            return None

        # Use clipboard paste for long questions (much faster)
        print("  Filling question via clipboard...")
        query_element.click()
        page.evaluate(f"""
            const ta = document.querySelector('textarea.query-box-input') ||
                       document.querySelector('textarea[aria-label="Feld für Anfragen"]') ||
                       document.querySelector('textarea[aria-label="Input for queries"]');
            if (ta) {{
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                nativeInputValueSetter.call(ta, {json.dumps(question)});
                ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        """)
        time.sleep(0.5)

        # Verify the text was set
        val = page.evaluate("document.querySelector('textarea.query-box-input')?.value || ''")
        print(f"  Question set in textarea: {len(val)} chars")

        # Submit
        print("  Submitting...")
        page.keyboard.press("Enter")

        time.sleep(1.5)

        # Wait for response
        print(f"  Waiting for answer (up to {timeout_seconds}s)...")

        answer = None
        stable_count = 0
        last_text = None
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            # Check if still thinking
            try:
                thinking_element = page.query_selector('div.thinking-message')
                if thinking_element and thinking_element.is_visible():
                    time.sleep(1)
                    continue
            except:
                pass

            # Try all response selectors
            for selector in RESPONSE_SELECTORS_EXTENDED:
                try:
                    elements = page.query_selector_all(selector)
                    if elements:
                        latest = elements[-1]
                        text = latest.inner_text().strip()

                        if text and len(text) > 50:  # Must be substantial
                            if text == last_text:
                                stable_count += 1
                                if stable_count >= 3:
                                    answer = text
                                    print(f"  Found stable answer via: {selector}")
                                    break
                            else:
                                stable_count = 0
                                last_text = text
                except:
                    continue

            if answer:
                break

            # Every 30s, report status and dump selectors found
            elapsed = timeout_seconds - (deadline - time.time())
            if int(elapsed) % 30 == 0 and elapsed > 5:
                print(f"  Still waiting... {int(elapsed)}s elapsed")
                # Quick debug dump
                for sel in RESPONSE_SELECTORS_EXTENDED[:4]:
                    try:
                        els = page.query_selector_all(sel)
                        if els:
                            print(f"    Found {len(els)} elements for: {sel}")
                            print(f"    Latest text preview: {els[-1].inner_text()[:100]}")
                    except:
                        pass

            time.sleep(1)

        if not answer:
            print("  Timeout - dumping page state...")
            print("  Current URL:", page.url)
            # Dump all available selectors
            for sel in RESPONSE_SELECTORS_EXTENDED:
                try:
                    els = page.query_selector_all(sel)
                    if els:
                        print(f"  Selector '{sel}': {len(els)} elements")
                        for el in els[-2:]:
                            txt = el.inner_text().strip()
                            if txt:
                                print(f"    -> {txt[:200]}")
                except:
                    pass

            # Try to dump the full chat area
            try:
                chat = page.query_selector('.chat-area') or page.query_selector('[role="main"]')
                if chat:
                    print("  Chat area content preview:")
                    print(chat.inner_text()[:500])
            except:
                pass

            return None

        print("  Got answer!")
        return answer + FOLLOW_UP_REMINDER

    except Exception as e:
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if context:
            try:
                context.close()
            except:
                pass
        if playwright:
            try:
                playwright.stop()
            except:
                pass


def main():
    parser = argparse.ArgumentParser(description='Ask NotebookLM a question (debug version)')
    parser.add_argument('--question', required=True)
    parser.add_argument('--notebook-url')
    parser.add_argument('--notebook-id')
    parser.add_argument('--show-browser', action='store_true')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()

    notebook_url = args.notebook_url
    if not notebook_url and args.notebook_id:
        library = NotebookLibrary()
        notebook = library.get_notebook(args.notebook_id)
        if notebook:
            notebook_url = notebook['url']
        else:
            print(f"Notebook '{args.notebook_id}' not found")
            return 1

    if not notebook_url:
        print("Specify --notebook-url or --notebook-id")
        return 1

    answer = ask_notebooklm(
        question=args.question,
        notebook_url=notebook_url,
        headless=not args.show_browser,
        timeout_seconds=args.timeout
    )

    if answer:
        print("\n" + "=" * 60)
        print(f"Question: {args.question[:100]}...")
        print("=" * 60)
        print()
        print(answer)
        print()
        print("=" * 60)
        return 0
    else:
        print("\nFailed to get answer")
        return 1


if __name__ == "__main__":
    sys.exit(main())
