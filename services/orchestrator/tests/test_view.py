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
