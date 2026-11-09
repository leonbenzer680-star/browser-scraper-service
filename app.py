"""
Browser Scraper Web Service (for Render + n8n / Make.com)
Call it with:  GET /search?q=your+words      or   POST /search {"query": "...", "max_results": 10}
Returns JSON: {"success": true, "engine": "...", "data": [{"title","url","snippet"}], "error": null}
Tries DuckDuckGo first, then Bing as a backup (DuckDuckGo often blocks cloud servers).
"""
import base64
import os
from urllib.parse import urlparse, parse_qs, unquote, quote_plus

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Browser Scraper Service")

# Optional password: set API_KEY in Render's Environment tab, then send header  x-api-key: <value>
API_KEY = os.environ.get("API_KEY", "")


class SearchRequest(BaseModel):
    query: str
    max_results: int = 10


def check_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing x-api-key")


def clean_ddg(href: str) -> str:
    """DuckDuckGo wraps links in a redirect; pull out the real URL."""
    if "uddg=" in href:
        return unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
    return "https:" + href if href.startswith("//") else href


def clean_bing(href: str) -> str:
    """Bing sometimes wraps links as bing.com/ck/a?...&u=a1<base64>; decode it."""
    if "bing.com/ck/" in href:
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            b = u[2:]
            try:
                return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
            except Exception:
                pass
    return href


async def text_of(el, selector):
    loc = el.locator(selector)
    return (await loc.first.inner_text()).strip() if await loc.count() else ""


async def attr_of(el, selector, name):
    loc = el.locator(selector)
    return (await loc.first.get_attribute(name) or "") if await loc.count() else ""


async def search_ddg(page, query, max_results):
    await page.goto("https://html.duckduckgo.com/html/?q=" + quote_plus(query),
                    wait_until="domcontentloaded", timeout=25000)
    await page.wait_for_selector(".result", state="attached", timeout=8000)
    items = page.locator(".result:not(.result--ad)")
    out = []
    for i in range(min(await items.count(), max_results)):
        el = items.nth(i)
        title = await text_of(el, ".result__title")
        if title:
            out.append({"title": title, "url": clean_ddg(await attr_of(el, ".result__a", "href")),
                        "snippet": (await text_of(el, ".result__snippet"))[:500]})
    return out


async def search_bing(page, query, max_results):
    await page.goto("https://www.bing.com/search?setlang=en-US&q=" + quote_plus(query),
                    wait_until="domcontentloaded", timeout=25000)
    await page.wait_for_selector("li.b_algo", state="attached", timeout=12000)
    items = page.locator("li.b_algo")
    out = []
    for i in range(min(await items.count(), max_results)):
        el = items.nth(i)
        title = await text_of(el, "h2")
        if title:
            out.append({"title": title, "url": clean_bing(await attr_of(el, "h2 a", "href")),
                        "snippet": (await text_of(el, ".b_caption p, p"))[:500]})
    return out


async def run_search(query: str, max_results: int) -> dict:
    payload = {"success": False, "query": query, "engine": None, "data": [], "error": None}
    max_results = max(1, min(max_results, 50))
    errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
            context = await browser.new_context(viewport={"width": 1280, "height": 800},
                                                user_agent=ua, locale="en-US")
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            # Skip images/fonts/media to save memory and time
            await context.route("**/*", lambda r: r.abort()
                                if r.request.resource_type in ("image", "font", "media")
                                else r.continue_())
            page = await context.new_page()

            for name, engine in (("duckduckgo", search_ddg), ("bing", search_bing)):
                try:
                    results = await engine(page, query, max_results)
                    if results:
                        payload.update(success=True, engine=name, data=results)
                        break
                    errors.append(f"{name}: no results")
                except Exception as e:
                    errors.append(f"{name}: {str(e).splitlines()[0]}")
        finally:
            await browser.close()

    if not payload["success"]:
        payload["error"] = " | ".join(errors)
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
