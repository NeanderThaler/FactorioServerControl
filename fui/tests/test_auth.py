from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from app.auth import is_authenticated, issue_token


class AuthenticationModeTests(TestCase):
    def test_empty_password_disables_authentication(self) -> None:
        self.assertTrue(is_authenticated("", "x" * 32, None))

    def test_nonempty_password_requires_valid_session(self) -> None:
        secret = "x" * 32
        with patch("app.auth.time.time", return_value=1_000):
            token = issue_token(secret, 60)
            self.assertTrue(is_authenticated("secure-password", secret, token))
            self.assertFalse(is_authenticated("secure-password", secret, None))

    def test_logout_control_is_hidden_when_authentication_is_disabled(self) -> None:
        templates = Path(__file__).parents[1] / "app" / "templates"
        template = Environment(loader=FileSystemLoader(templates)).get_template("index.html")
        context = {"request": SimpleNamespace(url_for=lambda *args, **kwargs: "/static")}

        self.assertNotIn("Sign out", template.render(logout_url=None, **context))
        self.assertIn("Sign out", template.render(logout_url="/fui/logout", **context))
