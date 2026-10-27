import json
import re
import time
import urllib.parse

import requests
from openhost_test_harness import OpenhostStack

HTML = {"Accept": "text/html"}


def _logs(stack: OpenhostStack) -> str:
    return f"\n\napp logs:\n{stack.app_logs()[-5000:]}"


def _owner_get(stack: OpenhostStack, path: str) -> requests.Response:
    """GET as the owner, retrying until Synapse and the admin UI behind Caddy answer."""
    deadline = time.monotonic() + 120
    while True:
        try:
            response = stack.owner_session.get(f"{stack.url}{path}", headers=HTML, timeout=30)
            if response.status_code == 200 or time.monotonic() > deadline:
                return response
        except requests.ConnectionError:
            if time.monotonic() > deadline:
                raise
        time.sleep(2)


def _js_string(page: str, name: str) -> str:
    match = re.search(rf"var {name} = (\"[^\"]*\");", page)
    assert match, f"no `var {name}` in the SSO bootstrap page: {page[:1500]}"
    return str(json.loads(match.group(1)))


def test_owner_onboards_and_is_signed_in_as_admin(stack: OpenhostStack) -> None:
    onboarding = _owner_get(stack, "/_openhost/community/onboarding")
    assert onboarding.status_code == 200, f"onboarding: {onboarding.status_code}{_logs(stack)}"
    # The suggested account name is the owner's username, sanitized to a Matrix localpart.
    username = re.sub(r"[^a-z0-9._=-]", "", stack.owner_username.lower()) or "owner"
    assert f'value="{username}"' in onboarding.text, f"onboarding doesn't suggest {username}: {onboarding.text[:2000]}"
    # No community join: CI can't federate with the hub server.
    done = stack.owner_session.post(
        f"{stack.url}/_openhost/community/onboarding", data={"username": username}, headers=HTML, timeout=60
    )
    assert done.status_code == 200 and "Invalid username" not in done.text, (
        f"onboarding POST: {done.status_code}: {done.text[:1000]}{_logs(stack)}"
    )

    login = _owner_get(stack, "/_openhost/community/login")
    assert login.status_code == 200, f"SSO login: {login.status_code}: {login.text[:500]}{_logs(stack)}"
    token = _js_string(login.text, "token")
    # The web client talks to the homeserver at this URL, so it must be the app's own URL.
    assert _js_string(login.text, "hsBase") == stack.url

    whoami = requests.get(
        f"{stack.url}/_matrix/client/v3/account/whoami", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    assert whoami.status_code == 200, f"whoami: {whoami.status_code}: {whoami.text[:300]}"
    user_id = whoami.json()["user_id"]
    assert user_id == f"@{username}:{stack.manifest.name}.{stack.zone_domain}", user_id

    user = stack.owner_session.get(
        f"{stack.url}/_synapse/admin/v2/users/{urllib.parse.quote(user_id)}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert user.status_code == 200 and user.json().get("admin") is True, (
        f"owner account isn't a Synapse admin: {user.status_code}: {user.text[:300]}"
    )


def test_web_client_is_served_with_the_sso_guard(stack: OpenhostStack) -> None:
    page = _owner_get(stack, "/")
    assert page.status_code == 200 and "openhost-firstrun-guard" in page.text, f"/: {page.text[:1000]}"
    config = _owner_get(stack, "/config.json")
    assert config.status_code == 200, config.text[:300]
    assert config.json()["homeserverList"] == [f"{stack.manifest.name}.{stack.zone_domain}"], config.text


def test_anonymous_access(stack: OpenhostStack) -> None:
    _owner_get(stack, "/")
    # Matrix APIs and discovery are public so other clients and servers can reach them.
    for path in ("/_matrix/client/versions", "/.well-known/matrix/client"):
        response = requests.get(f"{stack.url}{path}", timeout=30)
        assert response.status_code == 200, f"anonymous GET {path}: {response.status_code}: {response.text[:300]}"
    # The SSO endpoint mints an owner session, so it and the admin UI must stay behind the router login.
    for path in ("/_openhost/community/login", "/_openhost/admin", "/"):
        response = requests.get(f"{stack.url}{path}", headers=HTML, allow_redirects=False, timeout=30)
        assert response.status_code in (302, 303, 307, 401), (
            f"anonymous GET {path}: {response.status_code}: {response.text[:300]}"
        )
        assert "access_token" not in response.text and "var token" not in response.text
