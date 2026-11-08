"""Known deep links always resolve to an authorized renderer across auth changes."""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from state import session
from views import admin, analysis, auth, monitoring, records


def route_app(monkeypatch, route: str, role: str | None):
    calls = []
    declarations = {}
    monkeypatch.setattr(session, "validate_session", lambda: role is not None)
    monkeypatch.setattr(
        session, "get_user", lambda: {"id": 1, "email": "qa@example.com", "role": role}
    )
    monkeypatch.setattr(session, "stop_receiver", lambda: None)
    for name, module in {
        "auth": auth,
        "monitoring": monitoring,
        "records": records,
        "analysis": analysis,
        "admin": admin,
    }.items():

        def render(name=name):
            calls.append(name)
            st.write(f"Rendered {name}")

        monkeypatch.setattr(module, "render", render)
    real_navigation = st.navigation

    def navigation(pages, *, position):
        assert position == "hidden"
        declarations.update({page.url_path: page for page in pages})
        real_navigation(pages, position=position)
        selected = declarations[route]
        # AppTest cannot set a callable page URL; execute the page selected by
        # this deep link after the real navigation has registered every route.
        selected._can_be_called = True
        return selected

    monkeypatch.setattr(st, "navigation", navigation)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15)
    app.run()
    assert not app.exception
    return app, calls, declarations


@pytest.mark.parametrize("route", ["", "monitoring", "records", "analysis", "admin"])
def test_logged_out_deep_links_only_render_login(monkeypatch, route):
    _app, calls, pages = route_app(monkeypatch, route, None)
    assert calls == ["auth"]
    assert set(pages) == {"", "monitoring", "records", "analysis", "admin"}
    assert all(page._page is auth.render for page in pages.values())


@pytest.mark.parametrize("role", ["user", "viewer"])
def test_non_admin_login_on_admin_url_shows_safe_permission_notice(monkeypatch, role):
    app, calls, _pages = route_app(monkeypatch, "admin", role)
    assert not calls
    assert any("沒有系統管理權限" in item.value for item in app.warning)


@pytest.mark.parametrize(
    "route,expected",
    [
        ("", "monitoring"),
        ("monitoring", "monitoring"),
        ("records", "records"),
        ("analysis", "analysis"),
        ("admin", "admin"),
    ],
)
def test_authenticated_deep_links_use_expected_renderer(monkeypatch, route, expected):
    _app, calls, _pages = route_app(monkeypatch, route, "admin")
    assert calls == [expected]
