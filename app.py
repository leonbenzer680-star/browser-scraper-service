"""
Browser Scraper Web Service (for Render + n8n / Make.com)
Call it with:  GET /search?q=your+words      or   POST /search {"query": "...", "max_results": 10}
Returns JSON: {"success": true, "data": [{"title","url","snippet"}], "error": null}
"""
import os
from urllib.parse import urlparse, parse_qs, unquote, quote_plus

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

app = FastAPI(title="Browser Scraper Service")

# Optional password: set API_KEY in Render's Environment tab, then send header  x-api-key: <value>
API_KEY = os.environ.get("API_KEY", "")


class SearchRequest(BaseModel):
    query: str
    max_results: int = 10


def check_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")


def clean_link(href: str) -> str:
    """DuckDuckGo wraps links in a redirect; pull out the real URL."""
    if "uddg=" in href:
        return unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
    if href.startswith("//"):
        return "https:" + href
    return href


async def run_search(query: str, max_results: int) -> dict:
    payload = {"success": False, "query": query, "data": [], "error": None}
    max_results = max(1, min(max_results, 50))
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
            context = await browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=ua)
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            # Skip images/fonts/media to save memory and time
            await context.route("**/*", lambda r: r.abort()
                                if r.request.resource_type in ("image", "font", "media")
                                else r.continue_())
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_selector(".result", timeout=15000)

            items = page.locator(".result:not(.result--ad)")
            count = await items.count()
            for i in range(min(count, max_results)):
                el = items.nth(i)
                title = (await el.locator(".result__title").inner_text()).strip() \
                    if await el.locator(".result__title").count() else ""
                href = await el.locator(".result__a").first.get_attribute("href") \
                    if await el.locator(".result__a").count() else ""
                snippet = (await el.locator(".result__snippet").inner_text()).strip() \
                    if await el.locator(".result__snippet").count() else ""
                if title:
                    payload["data"].append({"title": title, "url": clean_link(href or ""),
                                            "snippet": snippet[:500]})
            payload["success"] = True
        except PlaywrightTimeoutError as e:
            payload["error"] = f"Timeout: {e}"
        except Exception as e:
            payload["error"] = f"Error: {e}"
        finally:
            await browser.close()
    return payload


@app.get("/")
def health():
    return {"status": "ok", "usage": "GET /search?q=words  or  POST /search {query, max_results}"}


@app.get("/search")
async def search_get(q: str, max_results: int = 10, x_api_key: str | None = Header(default=None)):
    check_key(x_api_key)
    return await run_search(q, max_results)


@app.post("/search")
async def search_post(body: SearchRequest, x_api_key: str | None = Header(default=None)):
    check_key(x_api_key)
    return await run_search(body.query, body.max_results)
