"""cached_metrics(): one BigQuery load per TTL, private copies per request, never stale across loaders."""
from command_center import main


def test_cached_metrics_reuses_copies_and_expires(monkeypatch):
    calls = []

    def loader():
        calls.append(1)
        return {"pins": [{"pin": "560102", "brands": {"Zara": 3}}]}

    monkeypatch.setattr(main, "compute_metrics", loader)
    monkeypatch.setattr(main, "METRICS_CACHE_SEC", 120.0)
    a = main.cached_metrics()
    a["pins"][0]["brands"]["Zara"] = 999  # e.g. brand masking edits its copy
    b = main.cached_metrics()
    assert len(calls) == 1 and b["pins"][0]["brands"]["Zara"] == 3

    monkeypatch.setattr(main, "METRICS_CACHE_SEC", 0.0)  # expired → reload
    main.cached_metrics()
    assert len(calls) == 2

    monkeypatch.setattr(main, "METRICS_CACHE_SEC", 120.0)
    monkeypatch.setattr(main, "compute_metrics", lambda: {"pins": []})  # different loader → no stale payload
    assert main.cached_metrics() == {"pins": []}
