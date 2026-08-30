"""
Omni Agentic API demo — hosted for free on Render (deploys from GitHub via render.yaml).

Pipeline (matches the writeup):
    1. Upsert embed user via generate-session, resolve their Omni userId via SCIM  (§1)
    2. Create an AI job as that user, poll, fetch result                           (§2)
    3. GET /ai/jobs/{id}/vis for a chart image (422 = scalar answer, no chart)      (§3)

Config comes from environment variables (Render: service Environment tab):
    OMNI_BASE_URL   https://<instance>.omniapp.co
    OMNI_API_KEY    Organization API key (not a PAT — userId impersonation needs an org key)
    OMNI_MODEL_ID   model to query against
    DEMO_PASSWORD   password visitors must enter before they can use the app
    SECRET_KEY      random string used to sign the login cookie
    OMNI_EMBED_SECRET     Embed secret (Admin -> Embed). With it set, "Link to this chat
                          in Omni" is a signed SSO embed URL that logs the embed user in;
                          without it, a plain /chat?chat=<conversationId> link.
    OMNI_EMBED_LOGIN_URL  optional override for the embed login URL
                          (default: https://<host with .embed-omniapp.co>/embed/login)

If any OMNI_* var is missing the app runs in MOCK MODE (fake answers, same code path).
If DEMO_PASSWORD is missing the password gate is disabled (fine locally; don't do that publicly).
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from urllib.parse import urlencode, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv(override=True)

OMNI_BASE_URL = os.environ.get("OMNI_BASE_URL", "").rstrip("/")
OMNI_API_KEY = os.environ.get("OMNI_API_KEY", "")
OMNI_MODEL_ID = os.environ.get("OMNI_MODEL_ID", "")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "")
OMNI_EMBED_SECRET = os.environ.get("OMNI_EMBED_SECRET", "")          # Admin -> Embed -> secret
OMNI_EMBED_LOGIN_URL = os.environ.get("OMNI_EMBED_LOGIN_URL", "")    # e.g. https://<org>.embed-omniapp.co/embed/login
OMNI_EMBED_CONNECTION_ROLES = os.environ.get("OMNI_EMBED_CONNECTION_ROLES", "")  # optional JSON, e.g. {"<conn-id>":"RESTRICTED_QUERIER"}
OMNI_CHAT_CONTENT_PATH = os.environ.get("OMNI_CHAT_CONTENT_PATH", "/chat?chat={conversation_id}")
DEFAULT_EMAIL = os.environ.get("DEFAULT_EMBED_EMAIL", "demo.user@example.com")
DEFAULT_NAME = os.environ.get("DEFAULT_EMBED_NAME", "Demo User")

MOCK_MODE = not (OMNI_BASE_URL and OMNI_API_KEY and OMNI_MODEL_ID)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

HEADERS = {"Authorization": f"Bearer {OMNI_API_KEY}", "Content-Type": "application/json"}

# email -> Omni user id. Per-process cache; avoids re-running generate-session +
# a paginated SCIM listing on every message.
_EMBED_USER_CACHE = {}


def log(step, detail):
    print(f"\n[{step}] {detail}", flush=True)


def raise_with_body(resp):
    if not resp.ok:
        log("HTTP ERROR", f"{resp.status_code} {resp.request.method} {resp.request.url}\nBody: {resp.text[:2000]}")
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------
def gate_enabled():
    return bool(DEMO_PASSWORD)


def is_authed():
    return not gate_enabled() or session.get("authed") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    if not gate_enabled() or is_authed():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), DEMO_PASSWORD):
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Wrong password."
    return render_template("login.html", error=error), (401 if error else 200)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Step 1 — upsert embed user + resolve their Omni user id (§1)
# ---------------------------------------------------------------------------
def upsert_embed_user(email, name, groups=None, user_attributes=None, connection_roles=None):
    """1a. POST /api/v1/embed/sso/generate-session — creates the embed user if
    missing, no-op otherwise. Permissions (groups / userAttributes /
    connectionRoles) are set here on every call; your backend is the source
    of truth for what they should be."""
    body = {"contentPath": "/my", "externalId": email, "name": name}
    if groups:
        body["groups"] = groups
    if user_attributes:
        body["userAttributes"] = user_attributes
    if connection_roles:
        body["connectionRoles"] = connection_roles
    log("1a. generate-session", f"POST {OMNI_BASE_URL}/api/v1/embed/sso/generate-session externalId={email}")
    resp = requests.post(f"{OMNI_BASE_URL}/api/v1/embed/sso/generate-session", headers=HEADERS, json=body, timeout=15)
    raise_with_body(resp)
    return resp.json()


def find_embed_user_by_filter(email):
    """1b (fast path). SCIM filter lookup — one call, no pagination, if the
    instance honours filters. Returns the user id or None."""
    for field in ("embedExternalId", "userName"):
        log("1b. scim filter", f'GET {OMNI_BASE_URL}/api/scim/v2/embed/Users filter={field} eq "{email}"')
        resp = requests.get(
            f"{OMNI_BASE_URL}/api/scim/v2/embed/Users",
            headers=HEADERS,
            params={"filter": f'{field} eq "{email}"'},
            timeout=15,
        )
        if not resp.ok:
            log("1b. scim filter", f"{resp.status_code} — filter not supported, falling back to pagination")
            return None
        payload = resp.json()
        users = payload.get("Resources", [])
        log("1b. scim filter", f"{field}: {len(users)} rows back (totalResults={payload.get('totalResults')})")
        for u in users:
            if email in (u.get("embedExternalId"), u.get("externalId"), u.get("userName")):
                return u["id"]
    return None


def resolve_embed_user_id(email):
    """1b (fallback). GET /api/scim/v2/embed/Users — paginated; match on
    embedExternalId (falling back to externalId / userName)."""
    start_index, page_size = 1, 200
    for _ in range(10):
        log("1b. list embed users", f"GET {OMNI_BASE_URL}/api/scim/v2/embed/Users startIndex={start_index}")
        resp = requests.get(
            f"{OMNI_BASE_URL}/api/scim/v2/embed/Users",
            headers=HEADERS,
            params={"startIndex": start_index, "count": page_size},
            timeout=15,
        )
        raise_with_body(resp)
        payload = resp.json()
        users = payload.get("Resources", [])
        total = payload.get("totalResults")
        for u in users:
            if email in (u.get("embedExternalId"), u.get("externalId"), u.get("userName")):
                return u["id"]
        if not users or (total is not None and start_index + len(users) > total):
            break
        start_index += page_size
    raise RuntimeError(f"No embed user found for {email} after generate-session")


def get_embed_user_id(email, name):
    if MOCK_MODE:
        log("1. resolve embed user", f"MOCK: pretending to upsert {email}")
        return "mock-user-" + str(uuid.uuid5(uuid.NAMESPACE_DNS, email))[:8]
    if email in _EMBED_USER_CACHE:
        log("1. resolve embed user", f"cache hit for {email}")
        return _EMBED_USER_CACHE[email]
    upsert_embed_user(email, name)
    user_id = find_embed_user_by_filter(email) or resolve_embed_user_id(email)
    log("1. resolve embed user", f"{email} -> {user_id}")
    _EMBED_USER_CACHE[email] = user_id
    return user_id


# ---------------------------------------------------------------------------
# Step 2 — create job, poll, fetch result (§2)
# ---------------------------------------------------------------------------
def ask_omni(prompt, embed_user_id, conversation_id=None):
    if MOCK_MODE:
        log("2. ask_omni", f"MOCK: pretending to answer '{prompt}'")
        time.sleep(1)
        return {
            "message": (
                f"**(mock response)** Here's a made-up answer to *\"{prompt}\"* — "
                "total sales last quarter were $1.2M, up 15% from Q3."
            ),
            "conversation_id": conversation_id or "mock-convo-1234",
            "job_id": "mock-job-5678",
            "actions": [],
            "omni_chat_url": "/chat/mock-convo-1234",
        }

    body = {"modelId": OMNI_MODEL_ID, "prompt": prompt}
    if conversation_id:
        body["conversationId"] = conversation_id
    log("2a. create job", f"POST {OMNI_BASE_URL}/api/v1/ai/jobs?userId={embed_user_id}")
    resp = requests.post(f"{OMNI_BASE_URL}/api/v1/ai/jobs", headers=HEADERS, params={"userId": embed_user_id}, json=body, timeout=15)
    raise_with_body(resp)
    job = resp.json()
    job_id = job["jobId"]
    log("2a. create job", f"jobId={job_id} conversationId={job.get('conversationId')}")

    deadline = time.time() + 90
    while True:
        status_resp = requests.get(f"{OMNI_BASE_URL}/api/v1/ai/jobs/{job_id}", headers=HEADERS, timeout=15)
        raise_with_body(status_resp)
        status = status_resp.json()
        log("2b. poll status", f"state={status['state']}")
        if status["state"] in ("COMPLETE", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            raise RuntimeError(f"Job {job_id} still {status['state']} after 90s")
        time.sleep(2)

    if status["state"] != "COMPLETE":
        raise RuntimeError(f"Job {status['state']}: {status.get('error', {}).get('message')}")

    log("2c. result", f"GET {OMNI_BASE_URL}/api/v1/ai/jobs/{job_id}/result")
    result_resp = requests.get(f"{OMNI_BASE_URL}/api/v1/ai/jobs/{job_id}/result", headers=HEADERS, timeout=15)
    raise_with_body(result_resp)
    result = result_resp.json()

    # Save omniChatUrl — the conversation's home inside Omni. Which response
    # carries it varies, so check status, result and the create response.
    omni_chat_url = status.get("omniChatUrl") or result.get("omniChatUrl") or job.get("omniChatUrl")
    log("2c. result", f"omniChatUrl={omni_chat_url}")
    if not omni_chat_url:
        # Show exactly what came back so the field can be located.
        log("2c. result", f"create keys={sorted(job)} status keys={sorted(status)} result keys={sorted(result)}")
    return {
        "message": result.get("message", ""),
        "conversation_id": job.get("conversationId"),
        "actions": result.get("actions", []),
        "job_id": job_id,
        "omni_chat_url": omni_chat_url,
    }


def embed_login_url():
    """The embed login endpoint lives on the embed host, which is separate from the
    app host (omni.omniapp.co -> omni.embed-omniapp.co). Override with
    OMNI_EMBED_LOGIN_URL if the instance's embed host doesn't follow that pattern."""
    if OMNI_EMBED_LOGIN_URL:
        return OMNI_EMBED_LOGIN_URL.rstrip("/")
    host = urlparse(OMNI_BASE_URL).netloc
    if ".omniapp.co" in host and ".embed-omniapp.co" not in host:
        host = host.replace(".omniapp.co", ".embed-omniapp.co")
    return f"https://{host}/embed/login"


def signed_embed_url(content_path, email, name, **optional):
    """Standard-SSO 'magic URL' per docs.omni.co/embed/setup/standard-sso:
    HMAC-SHA256(secret) over newline-joined [loginUrl, contentPath, externalId,
    name, nonce] + optional params in alphabetical key order, base64url-encoded,
    appended as ?signature=. No API call — the URL is signed locally."""
    login_url = embed_login_url()
    nonce = secrets.token_urlsafe(24)
    optional = {k: v for k, v in optional.items() if v}
    parts = [login_url, content_path, email, name, nonce] + [optional[k] for k in sorted(optional)]
    data = "\n".join(parts).encode("utf-8")
    sig = base64.urlsafe_b64encode(hmac.new(OMNI_EMBED_SECRET.encode("utf-8"), data, hashlib.sha256).digest()).decode("ascii").rstrip("=")
    params = {"contentPath": content_path, "externalId": email, "name": name, "nonce": nonce, **optional, "signature": sig}
    return f"{login_url}?{urlencode(params)}"


def sso_url_for_chat(conversation_id, email, name):
    """Signed embed link that drops the embed user straight into this conversation,
    authenticated. Requires OMNI_EMBED_SECRET; otherwise returns None and the
    caller falls back to the plain /chat?chat=<id> URL."""
    if MOCK_MODE:
        return "https://example.embed-omniapp.co/embed/login?mock=1"
    if not OMNI_EMBED_SECRET or not conversation_id:
        return None
    content_path = OMNI_CHAT_CONTENT_PATH.format(conversation_id=conversation_id)
    url = signed_embed_url(content_path, email, name, connectionRoles=OMNI_EMBED_CONNECTION_ROLES)
    log("2d. signed embed url", f"contentPath={content_path} login={embed_login_url()}")
    return url


# ---------------------------------------------------------------------------
# Step 3 — chart image (§3)
# ---------------------------------------------------------------------------
def render_chart_image(job_id):
    if MOCK_MODE:
        return {"status": "mock", "image_url": "/static/mock-chart.svg"}

    log("3. render vis", f"GET {OMNI_BASE_URL}/api/v1/ai/jobs/{job_id}/vis")
    resp = requests.get(f"{OMNI_BASE_URL}/api/v1/ai/jobs/{job_id}/vis", headers=HEADERS, timeout=30)
    if resp.status_code == 422:
        log("3. render vis", f"422 — scalar answer, nothing to chart: {resp.text[:200]}")
        return {"status": "no_chart", "note": "This answer doesn't have an associated chart."}
    raise_with_body(resp)

    content_type = resp.headers.get("Content-Type", "")
    if "image" in content_type:
        b64 = base64.b64encode(resp.content).decode("ascii")
        log("3. render vis", f"got image ({content_type}, {len(resp.content)} bytes)")
        return {"status": "ok", "content_type": content_type, "data_url": f"data:{content_type};base64,{b64}"}
    body = resp.json()
    log("3. render vis", f"got JSON: {list(body) if isinstance(body, dict) else type(body)}")
    return {"status": "ok", "raw": body}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if not is_authed():
        return redirect(url_for("login"))
    return render_template(
        "index.html",
        mock_mode=MOCK_MODE,
        gate_enabled=gate_enabled(),
        default_email=DEFAULT_EMAIL,
        default_name=DEFAULT_NAME,
    )


@app.route("/health")
def health():
    return jsonify({"ok": True, "mock_mode": MOCK_MODE})


@app.route("/ask", methods=["POST"])
def ask():
    if not is_authed():
        return jsonify({"error": True, "step": "auth", "detail": "Not logged in — reload the page."}), 401

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or DEFAULT_EMAIL).strip()
    name = (data.get("name") or DEFAULT_NAME).strip()
    prompt = (data.get("prompt") or "").strip()
    conversation_id = data.get("conversation_id")
    if not prompt:
        return jsonify({"error": True, "step": "input", "detail": "Empty prompt."}), 400

    try:
        user_id = get_embed_user_id(email, name)
    except Exception as e:
        log("ERROR in step 1", repr(e))
        return jsonify({"error": True, "step": "resolve_embed_user", "detail": str(e)}), 502

    try:
        result = ask_omni(prompt, user_id, conversation_id)
    except Exception as e:
        log("ERROR in step 2", repr(e))
        return jsonify({"error": True, "step": "ask_omni", "detail": str(e)}), 502

    # Link to the conversation in Omni. Default: the plain chat URL, which works for
    # anyone who can log in to the instance. If OMNI_EMBED_SECRET is set, swap in a
    # signed embed URL so a seatless embed user can open it too.
    open_in_omni = f"{OMNI_BASE_URL}/chat?chat={result['conversation_id']}" if result.get("conversation_id") and not MOCK_MODE else None
    try:
        open_in_omni = sso_url_for_chat(result.get("conversation_id"), email, name) or open_in_omni
    except Exception as e:
        log("ERROR in step 2d", repr(e))

    try:
        image = render_chart_image(result["job_id"])
    except Exception as e:
        log("ERROR in step 3", repr(e))
        image = {"status": "error", "note": str(e)}

    return jsonify(
        {
            "mock_mode": MOCK_MODE,
            "embed_user_id": user_id,
            "message": result["message"],
            "conversation_id": result["conversation_id"],
            "actions": result["actions"],
            "omni_chat_url": result.get("omni_chat_url"),
            "open_in_omni": open_in_omni,
            "image": image,
        }
    )


if __name__ == "__main__":
    print(f"MOCK MODE: {MOCK_MODE} | gate: {'on' if gate_enabled() else 'OFF'} | base: {OMNI_BASE_URL or '(unset)'}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)), debug=False)
