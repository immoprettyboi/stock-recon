"""Opens the sync's Chrome on the Courier Guy portal so you can log in once."""
import time
from pathlib import Path
import stockrecon_sync as S
import portal

def main():
    cfg = S.load_config()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = portal.launch(p, cfg, False, S.PROFILE_DIR, S.DOWNLOADS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(portal.COURIER_URL)
        S.log("Chrome is open on the Courier Guy portal. Log in; the window closes by itself once you're in.")
        ok_since, deadline = None, time.time() + 15 * 60
        while time.time() < deadline:
            try:
                logged_in = "thecourierguy" in page.url and page.locator("input[type=password]").count() == 0 \
                            and "login" not in page.url.lower()
            except Exception:
                logged_in = False
            if logged_in:
                ok_since = ok_since or time.time()
                if time.time() - ok_since > 6:
                    S.log("Courier Guy: logged in. The sync will read your shipments from now on.")
                    ctx.close()
                    return
            else:
                ok_since = None
            page.wait_for_timeout(1500)
        S.log("Courier Guy: gave up waiting for the login after 15 minutes.")
        ctx.close()

if __name__ == "__main__":
    main()
