"""Nav + route gating: agentcore hides Episodes/Observations, sqlite shows all."""

from __future__ import annotations

from html.parser import HTMLParser

from flask.testing import FlaskClient


class _RailLinkCounter(HTMLParser):
    """Count <a class="rail-link ...">; assert presence/absence, not text."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        d = dict(attrs)
        cls = d.get("class") or ""
        if "rail-link" in cls.split():
            self.hrefs.append(d.get("href") or "")


def _rail_hrefs(html: str) -> list[str]:
    p = _RailLinkCounter()
    p.feed(html)
    return p.hrefs


def test_sqlite_shows_all_five_rail_links(client: FlaskClient) -> None:
    html = client.get("/reflections").get_data(as_text=True)
    hrefs = _rail_hrefs(html)
    assert len(hrefs) == 5
    assert any("/episodes" in h for h in hrefs)
    assert any("/observations" in h for h in hrefs)


def test_agentcore_hides_episodes_and_observations_links(
    agentcore_client: FlaskClient,
) -> None:
    # [[server-boot-real-call]] -- drives a real route through the stubbed backend.
    resp = agentcore_client.get("/reflections")
    assert resp.status_code == 200
    hrefs = _rail_hrefs(resp.get_data(as_text=True))
    assert len(hrefs) == 3
    assert not any("/episodes" in h for h in hrefs)
    assert not any("/observations" in h for h in hrefs)


def test_agentcore_episodes_routes_404(agentcore_client: FlaskClient) -> None:
    assert agentcore_client.get("/episodes").status_code == 404
    assert agentcore_client.get("/episodes/panel").status_code == 404
    assert agentcore_client.get("/episodes/banner").status_code == 404


def test_agentcore_observations_routes_404(agentcore_client: FlaskClient) -> None:
    assert agentcore_client.get("/observations").status_code == 404
    assert agentcore_client.get("/observations/panel").status_code == 404


def test_sqlite_episodes_and_observations_reachable(client: FlaskClient) -> None:
    assert client.get("/episodes").status_code == 200
    assert client.get("/observations").status_code == 200
