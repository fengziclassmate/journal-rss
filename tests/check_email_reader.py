"""Manual browser smoke test; no network or paid services."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]
output=Path('F:/Zotero/rss-reader-verification-20260929')
output.mkdir(exist_ok=True)
page_path=root/'arxiv-email-archive/2026-09-29.html'
total=len(json.loads(page_path.with_suffix('.json').read_text('utf-8'))['papers'])
with sync_playwright() as engine:
    browser=engine.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1360,'height':900})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(page_path.as_uri())
    page.wait_for_function("document.querySelector('#count').textContent.includes('/')")
    assert page.locator('article').count()==total
    assert page.locator('article:visible').count()==total
    assert page.locator('#toc a').count()==total
    page.screenshot(path=str(output/'desktop.png'))
    page.locator('#search').fill('this-phrase-cannot-match-any-paper-93827')
    page.wait_for_timeout(400)
    assert page.locator('article:visible').count()==0
    page.locator('#search').fill('learning')
    page.wait_for_timeout(500)
    assert page.locator('article:visible').count()>0
    assert page.locator('article:visible mark').count()>0
    page.locator('#search').fill('')
    page.wait_for_timeout(400)
    page.locator('#category').select_option(index=1)
    assert 0<page.locator('article:visible').count()<=total
    page.locator('#category').select_option(index=0)
    assert page.locator('article:visible').count()==total
    page.locator('#abstracts').check()
    assert page.locator('article details[open]').count()==total
    page.locator('#abstracts').uncheck()
    assert page.locator('article details[open]').count()==0
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(output/'mobile.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
print(json.dumps({'papers':total,'desktop_mobile':'passed','errors':errors,'screenshots':str(output)}))
