"""AC-01：GET / 返回 web/index.html（200，text/html）；GET /js/<名>.mjs 返回 web/js/ 下的模块。

机制（tech §7.1）：create_app() 先注册 GET / 与 /js 静态路由，最后把 MCP 挂到 "/"。
网页目录取 Settings.web_dir（这里是临时目录里的夹具页面），不读仓库里的 web/。
"""

import httpx
import pytest

from a_support import BASE_URL, WEB_APP_MJS, WEB_INDEX, build_app


@pytest.fixture
def app(tmp_path):
    app, _settings, store = build_app(tmp_path)
    yield app
    store.close()


async def _get(app, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        return await client.get(path)


async def test_ac01_root_serves_index_html(app):
    r = await _get(app, "/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.text == WEB_INDEX


async def test_ac01_js_module_is_served_from_web_js(app):
    r = await _get(app, "/js/app.mjs")
    assert r.status_code == 200
    assert r.text == WEB_APP_MJS
