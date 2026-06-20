#!/usr/bin/env python3
"""
Simple NotebookLM Question Interface
Based on MCP server implementation - simplified without sessions

Implements hybrid auth approach:
- Persistent browser profile (user_data_dir) for fingerprint consistency
- Manual cookie injection from state.json for session cookies (Playwright bug workaround)
See: https://github.com/microsoft/playwright/issues/36139
"""

import argparse
import sys
import time
import re
from pathlib import Path

from patchright.sync_api import sync_playwright

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from auth_manager import AuthManager
from notebook_manager import NotebookLibrary
from config import QUERY_INPUT_SELECTORS
from browser_utils import BrowserFactory, StealthUtils


# Follow-up reminder (adapted from MCP server for stateless operation)
# Since we don't have persistent sessions, we encourage comprehensive questions
FOLLOW_UP_REMINDER = (
    "\n\nEXTREMELY IMPORTANT: Is that ALL you need to know? "
    "You can always ask another question! Think about it carefully: "
    "before you reply to the user, review their original request and this answer. "
    "If anything is still unclear or missing, ask me another comprehensive question "
    "that includes all necessary context (since each question opens a new browser session)."
)


def _clear_chat(page) -> bool:
    """Delete NotebookLM's persisted chat history before asking (ip-ks-002).

    NotebookLM now persists chat between sessions, so prior answers — including the
    notebook_manager discovery queries (overview + JSON metadata) — pollute the conversation
    and get mis-extracted as the answer. Clearing first guarantees our reply is the only one.
    Non-fatal: returns False and continues if any step can't be found.
    """
    # 1. Open the chat-options (more_vert) menu.
    opened = False
    for sel in ('button[aria-label="Opções do chat"]', 'button[aria-label="Chat options"]'):
        try:
            b = page.query_selector(sel)
            if b and b.is_visible():
                b.click()
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False
    time.sleep(1)

    # 2. Click the "delete chat history" item (locale-tolerant text match).
    deleted = False
    try:
        for it in page.query_selector_all('[role="menuitem"], .mat-mdc-menu-item'):
            t = (it.inner_text() or "").lower()
            if ("histórico do chat" in t or "historico do chat" in t
                    or "delete chat" in t or "chat history" in t):
                it.click()
                deleted = True
                break
    except Exception:
        pass
    if not deleted:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    time.sleep(1)

    # 3. Confirm if a dialog appears.
    try:
        for b in page.query_selector_all('[role="dialog"] button, mat-dialog-container button'):
            t = (b.inner_text() or "").strip().lower()
            if any(k in t for k in ("eliminar", "delete", "confirm", "remove", "sim", "yes")):
                b.click()
                break
    except Exception:
        pass
    time.sleep(2)
    print("  🧹 cleared persisted chat history")
    return True


def ask_notebooklm(question: str, notebook_url: str, headless: bool = True) -> str:
    """
    Ask a question to NotebookLM

    Args:
        question: Question to ask
        notebook_url: NotebookLM notebook URL
        headless: Run browser in headless mode

    Returns:
        Answer text from NotebookLM
    """
    auth = AuthManager()

    if not auth.is_authenticated():
        print("⚠️ Not authenticated. Run: python auth_manager.py setup")
        return None

    print(f"💬 Asking: {question}")
    print(f"📚 Notebook: {notebook_url}")

    playwright = None
    context = None

    try:
        # Start playwright
        playwright = sync_playwright().start()

        # Launch persistent browser context using factory
        context = BrowserFactory.launch_persistent_context(
            playwright,
            headless=headless
        )

        # Navigate to notebook
        page = context.new_page()
        print("  🌐 Opening notebook...")
        page.goto(notebook_url, wait_until="domcontentloaded", timeout=60000)

        # Wait for NotebookLM. goto() already navigated to the notebook URL, so this is only a
        # secondary confirmation — give the heavy SPA room and never hard-fail on it (a slow
        # URL-settle should not abort an otherwise-loaded page). (ip-ks-002)
        try:
            page.wait_for_url(re.compile(r"^https://notebooklm\.google\.com/"), timeout=45000)
        except Exception:
            print("  ⚠️ URL settle slow; continuing (page already navigated)")

        # Honest auth check: if Google bounced us to a sign-in/login page, the saved
        # session is no longer valid (typically the rotating __Secure-*PSIDTS cookies were
        # lost on save — see AUTHENTICATION.md). Report this clearly instead of failing
        # later with the misleading "Could not find query input".
        cur = page.url or ""
        if "accounts.google.com" in cur or "/login" in cur or "ServiceLogin" in cur:
            print("  ❌ NotebookLM session is NOT authenticated (redirected to Google sign-in).")
            print("     Refresh auth, then retry:")
            print("       • close Brave, then:  python scripts/run.py bootstrap_auth.py --browser brave")
            print("       • or log into Google in Chrome, then:  ... bootstrap_auth.py --browser chrome")
            print("       • or interactive login:  python scripts/run.py auth_manager.py setup")
            context.close()
            return None

        # ip-ks-002: clear the persisted chat first so our reply is the only answer in the
        # conversation (NotebookLM persists chat between sessions; prior discovery answers
        # otherwise get mis-extracted). Non-fatal if the control isn't found.
        time.sleep(3)
        try:
            _clear_chat(page)
        except Exception as _e:
            print(f"  ⚠️ clear-chat skipped: {_e}")

        # Wait for query input (MCP approach)
        print("  ⏳ Waiting for query input...")
        query_element = None

        for selector in QUERY_INPUT_SELECTORS:
            try:
                query_element = page.wait_for_selector(
                    selector,
                    timeout=20000,  # ip-ks-002: was 10000 — SPA input can be slow to mount
                    state="visible"  # Only check visibility, not disabled!
                )
                if query_element:
                    print(f"  ✓ Found input: {selector}")
                    break
            except:
                continue

        if not query_element:
            print("  ❌ Could not find query input")
            return None

        # Type question (human-like, fast)
        print("  ⏳ Typing question...")
        
        # Use primary selector for typing
        input_selector = QUERY_INPUT_SELECTORS[0]
        StealthUtils.human_type(page, input_selector, question)

        # ip-ks-002: anchor extraction to the answer that appears AFTER this submission.
        # NotebookLM's chat panel accumulates prior answers (e.g. notebook_manager's discovery
        # queries leave an overview + JSON-metadata answer), so taking the "latest" element can
        # return a stale answer. Record the pre-submit answer count and only accept a newer one.
        ANSWER_SELECTOR = ".to-user-container .message-text-content"
        def _answer_texts():
            try:
                return [(e.inner_text() or "").strip()
                        for e in page.query_selector_all(ANSWER_SELECTOR)]
            except Exception:
                return []
        pre_answer_count = len(_answer_texts())

        # Submit
        print("  📤 Submitting...")
        page.keyboard.press("Enter")

        # Small pause
        StealthUtils.random_delay(500, 1500)

        # Wait for the NEW answer (ip-ks-002): poll until a fresh answer message appears
        # (answer count grows past the pre-submit count), then take the newest and wait for
        # its streamed text to stabilise.
        print("  ⏳ Waiting for answer...")

        answer = None
        stable_count = 0
        last_text = None
        deadline = time.time() + 240  # NotebookLM answers can stream slowly

        while time.time() < deadline:
            # Still thinking? keep waiting.
            try:
                thinking_element = page.query_selector('div.thinking-message')
                if thinking_element and thinking_element.is_visible():
                    time.sleep(1)
                    continue
            except Exception:
                pass

            texts = _answer_texts()
            # Accept only an answer that appeared AFTER our question (count grew), and walk
            # newest-first skipping NotebookLM's persistent notebook-overview summary card —
            # it renders as a trailing answer element but is not a reply to our question.
            candidate_text = None
            if len(texts) > pre_answer_count:
                for t in reversed(texts):
                    if not t:
                        continue
                    low = t.lower()
                    if ("sources in this notebook" in low
                            or "this notebook provide" in low
                            or "this notebook explore" in low
                            or "this notebook is a comprehensive" in low
                            or "this notebook outlines" in low
                            or "topics are covered" in low):
                        continue
                    candidate_text = t
                    break

            if candidate_text:
                if candidate_text == last_text:
                    stable_count += 1
                    if stable_count >= 3:  # stable for 3 polls -> streaming finished
                        answer = candidate_text
                        break
                else:
                    stable_count = 0
                    last_text = candidate_text

            time.sleep(1)

        if not answer:
            print("  ❌ Timeout waiting for answer")
            return None

        print("  ✅ Got answer!")
        # Add follow-up reminder to encourage Claude to ask more questions
        return answer + FOLLOW_UP_REMINDER

    except Exception as e:
        print(f"  ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return None

    finally:
        # Always clean up
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
    parser = argparse.ArgumentParser(description='Ask NotebookLM a question')

    parser.add_argument('--question', required=True, help='Question to ask')
    parser.add_argument('--notebook-url', help='NotebookLM notebook URL')
    parser.add_argument('--notebook-id', help='Notebook ID from library')
    parser.add_argument('--show-browser', action='store_true', help='Show browser')

    args = parser.parse_args()

    # Resolve notebook URL
    notebook_url = args.notebook_url

    if not notebook_url and args.notebook_id:
        library = NotebookLibrary()
        notebook = library.get_notebook(args.notebook_id)
        if notebook:
            notebook_url = notebook['url']
        else:
            print(f"❌ Notebook '{args.notebook_id}' not found")
            return 1

    if not notebook_url:
        # Check for active notebook first
        library = NotebookLibrary()
        active = library.get_active_notebook()
        if active:
            notebook_url = active['url']
            print(f"📚 Using active notebook: {active['name']}")
        else:
            # Show available notebooks
            notebooks = library.list_notebooks()
            if notebooks:
                print("\n📚 Available notebooks:")
                for nb in notebooks:
                    mark = " [ACTIVE]" if nb.get('id') == library.active_notebook_id else ""
                    print(f"  {nb['id']}: {nb['name']}{mark}")
                print("\nSpecify with --notebook-id or set active:")
                print("python scripts/run.py notebook_manager.py activate --id ID")
            else:
                print("❌ No notebooks in library. Add one first:")
                print("python scripts/run.py notebook_manager.py add --url URL --name NAME --description DESC --topics TOPICS")
            return 1

    # Ask the question
    answer = ask_notebooklm(
        question=args.question,
        notebook_url=notebook_url,
        headless=not args.show_browser
    )

    if answer:
        print("\n" + "=" * 60)
        print(f"Question: {args.question}")
        print("=" * 60)
        print()
        print(answer)
        print()
        print("=" * 60)
        return 0
    else:
        print("\n❌ Failed to get answer")
        return 1


if __name__ == "__main__":
    sys.exit(main())
