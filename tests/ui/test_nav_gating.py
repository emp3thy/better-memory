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


def test_sqlite_root_redirects_to_episodes(client: FlaskClient) -> None:
    """Root redirect target is unchanged on sqlite: still /episodes."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/episodes")


def test_agentcore_root_redirects_to_reflections_not_episodes(
    agentcore_client: FlaskClient,
) -> None:
    """C1: root() must not dead-end into the gated /episodes 404 in
    agentcore mode -- it should redirect to /reflections (always visible)
    and following the redirect must yield 200, not 404."""
    resp = agentcore_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/reflections")
    followed = agentcore_client.get("/", follow_redirects=True)
    assert followed.status_code == 200


def test_agentcore_episodes_drawer_and_close_route_404(
    agentcore_client: FlaskClient,
) -> None:
    """M2: guarded episode-detail routes also 404 in agentcore mode."""
    assert agentcore_client.get("/episodes/some-id/drawer").status_code == 404
    resp = agentcore_client.post(
        "/episodes/some-id/close?outcome=success",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 404


def test_agentcore_observations_drawer_and_promote_route_404(
    agentcore_client: FlaskClient,
) -> None:
    """M2: guarded observation-detail routes also 404 in agentcore mode."""
    assert agentcore_client.get("/observations/some-id/drawer").status_code == 404
    resp = agentcore_client.post(
        "/observations/some-id/promote-to-semantic",
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 404
