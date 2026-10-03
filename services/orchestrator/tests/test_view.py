import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")   # never connected in this test
os.environ.setdefault("REDIS_URL", "redis://unused:6379/0")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_surgical_view_is_served_without_starting_the_stack():
    client = TestClient(app)                 # no `with`: lifespan (db, redis) isn't started
    r = client.get("/view")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert 'new WebSocket(' in r.text and '"/ask"' in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/view"


def test_the_phone_app_is_installable_and_its_worker_only_shows_notifications():
    from app import auth
    client = TestClient(app)
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    j = m.json()
    assert j["start_url"] == "/view" and j["display"] == "standalone" and {i["sizes"] for i in j["icons"]} >= {"192x192", "512x512"}
    for i in j["icons"]:
        r = client.get(i["src"])
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert i["src"] in auth.PUBLIC                                   # fetched without the pairing cookie
    sw = client.get("/sw.js")
    assert sw.headers["service-worker-allowed"] == "/" and "showNotification" in sw.text
    assert "caches." not in sw.text and '"fetch"' not in sw.text        # never a stale copy of the app
    assert client.get("/icons/../main.py").status_code == 404
    v = client.get("/view").text
    assert 'rel="manifest"' in v and 'apple-touch-icon' in v and 'id="notify"' in v
