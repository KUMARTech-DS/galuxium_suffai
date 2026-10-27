"""Clean page URLs: /geo, /ads-lab, … serve the shell; old .html links redirect without losing their query."""
from fastapi.testclient import TestClient

from command_center import main


def test_clean_urls_and_legacy_redirects():
    c = TestClient(main.app)
    for path in ("/", "/geo", "/ads-lab", "/campaigns", "/style-assistant", "/admin"):
        r = c.get(path)
        assert r.status_code == 200 and 'id="frame-geo"' in r.text, path  # the app shell
    r = c.get("/login")
    assert r.status_code == 200 and 'id="pass"' in r.text

    r = c.get("/index.html?tab=campaigns&campaign_id=abc", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/?tab=campaigns&campaign_id=abc"
    r = c.get("/login.html", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/login"
    r = c.get("/ads-lab/", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/ads-lab"

    # Tab pages and assets are still served as files (they load inside the shell's iframes)
    assert c.get("/geo.html").status_code == 200
    assert c.get("/session.js").status_code == 200
    assert c.get("/v1/metrics").status_code == 401  # APIs untouched
