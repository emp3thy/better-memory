"""The Reflections project dropdown sources from backend.distinct_projects()."""

from __future__ import annotations

from html.parser import HTMLParser
from unittest.mock import patch

from flask.testing import FlaskClient


class _ProjectOptions(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_project_select = False
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        d = dict(attrs)
        if tag == "select" and d.get("name") == "project":
            self._in_project_select = True
        elif tag == "option" and self._in_project_select:
            self.values.append(d.get("value") or "")

    def handle_endtag(self, tag) -> None:
        if tag == "select":
            self._in_project_select = False


def _project_options(html: str) -> list[str]:
    p = _ProjectOptions()
    p.feed(html)
    return p.values


def test_dropdown_sources_from_backend_distinct_projects(
    agentcore_client: FlaskClient,
) -> None:
    fake = agentcore_client.application.extensions["backend"]
    with patch.object(
        fake, "distinct_projects", return_value=["zeta", "alpha"],
    ):
        html = agentcore_client.get("/reflections").get_data(as_text=True)
    opts = _project_options(html)
    # project_name() is unioned in, and the set is sorted casefold.
    assert "alpha" in opts
    assert "zeta" in opts
    assert opts == sorted(opts, key=lambda s: s.casefold())
