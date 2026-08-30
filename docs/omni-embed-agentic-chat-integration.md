# Embedding Omni's Agentic API in an External Chat Interface

**Goal:** pre-auth embed users, ask Omni AI a question on their behalf, and pull back an image of the resulting chart to render inline in a custom chat UI — with each user only ever seeing data they're permitted to see.

Every endpoint and payload shape below has been run against a live instance (`omni.playground.exploreomni.dev`) through a small test app, not just read off the docs. Where the docs and the live API disagreed, this guide follows what the API actually does.

**Live demo:** https://omni-agentic-chat-demo.onrender.com/ (password: `omni-agent123!`). Free-tier hosting, so allow ~1 minute for it to wake if it has been idle. Source: the repo this file lives in.

![Chart rendering inline in the demo app](./screenshots/01_hero_chart.png)

*A short screen recording of this end-to-end (question → wait → chart appearing) is worth linking here too — the wait between `EXECUTING` and the chart popping in is worth seeing in real time, not just reading about.*

---

## 0. Prerequisites

- An **Organization API key** (not a PAT — the `userId` impersonation parameter used throughout only works with org keys)
- A model ID for the model the chat should query against
- Embed users provisioned (or provisionable) in the target Omni instance

---

## 1. Pre-auth the embed user (email → Omni user ID)

Two calls: upsert the user, then resolve their internal ID.

### 1a — Upsert the user via `generate-session`

This creates the embed user if they don't exist, or is a no-op if they do. **Note the version** — this is `/api/v1/`, not `/api/unstable/` as some docs show; `unstable` happens to still work on some instances but `v1` is the stable, current route:

```python
def upsert_embed_user(email, name):
    resp = requests.post(
        f"{OMNI_BASE_URL}/api/v1/embed/sso/generate-session",
        headers=HEADERS,
        json={"contentPath": "/my", "externalId": email, "name": name},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
```

Use the corporate email as `externalId` — same pattern used for OmniHR/Zeals signed SSO embeds, where `externalId` maps to corporate email.

### 1b — Resolve the Omni user ID

The endpoint is the SCIM embed-users route, not `/api/v0/users/embed` (that path doesn't exist — 404 against a real instance):

```
GET /api/scim/v2/embed/Users
```

**Fast path — SCIM filter (one call).** Filter on `embedExternalId`, which is the field that carries the `externalId` you passed to generate-session. Filtering on `userName` is kept as a second attempt for instances that populate that field instead — on the playground instance `userName` did *not* match the email, so leading with it silently returns nothing and you end up paginating anyway.

```python
def find_embed_user_by_filter(email):
    for field in ("embedExternalId", "userName"):
        resp = requests.get(
            f"{OMNI_BASE_URL}/api/scim/v2/embed/Users",
            headers=HEADERS,
            params={"filter": f'{field} eq "{email}"'},
            timeout=15,
        )
        if not resp.ok:
            return None  # filter not supported here — fall back to pagination
        for u in resp.json().get("Resources", []):
            if email in (u.get("embedExternalId"), u.get("externalId"), u.get("userName")):
                return u["id"]
    return None
```

**Fallback — paginate.** Keep this behind the filter for instances that ignore `filter=`. Two things that will bite you if you skip them:

1. **It paginates.** A newly-created user won't necessarily be on the first page (on the playground instance the demo user was on page 2). Use `startIndex`/`count` and keep going until you find a match or exhaust `totalResults`.
2. **Match on the right field.** Check `embedExternalId` first, then `externalId` and `userName` defensively.

```python
def resolve_embed_user_id(email):
    start_index, page_size = 1, 200
    for _ in range(10):  # sane upper bound on pages
        resp = requests.get(
            f"{OMNI_BASE_URL}/api/scim/v2/embed/Users",
            headers=HEADERS,
            params={"startIndex": start_index, "count": page_size},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        users = payload.get("Resources", [])
        total = payload.get("totalResults")

        for u in users:
            if email in (u.get("embedExternalId"), u.get("externalId"), u.get("userName")):
                return u["id"]

        if not users or (total is not None and start_index + len(users) > total):
            break
        start_index += page_size

    raise RuntimeError(f"No embed user found for {email}")
```

Wire them together as `find_embed_user_by_filter(email) or resolve_embed_user_id(email)`. Log how many rows the filter returns the first time you run against a new instance: 1 row means filters work; a full page with `totalResults` equal to the whole user count means the instance ignores `filter=` and you should drop the attempt rather than pay for a wasted call on every cache miss.

**If you need SCIM-based bulk provisioning instead** of the lazy upsert-on-first-session pattern (e.g. syncing from an IdP ahead of time): standard SCIM user creation also works —

```bash
curl -X POST "https://<INSTANCE>.omniapp.co/api/scim/v2/Users" \
  -H "Authorization: Bearer <OMNI_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"displayName": "Jane Doe", "userName": "jane@customer.com"}'
```

Note the capitalized `Users` — SCIM routes on this API are case-sensitive to the standard SCIM 2.0 convention.

### 1c — Deciding group/RLS values before calling generate-session

Nothing about user creation *decides* permissions on its own — you tell Omni what they are, every time, via the same `generate-session` call:

```python
def upsert_embed_user(email, name, groups=None, user_attributes=None, connection_roles=None):
    body = {"contentPath": "/my", "externalId": email, "name": name}
    if groups:
        body["groups"] = groups                       # e.g. ["customer_a_viewers"]
    if user_attributes:
        body["userAttributes"] = user_attributes       # what your RLS filters key on
    if connection_roles:
        body["connectionRoles"] = connection_roles     # e.g. {"<connection_id>": "RESTRICTED_QUERIER"}
    resp = requests.post(f"{OMNI_BASE_URL}/api/v1/embed/sso/generate-session",
                          headers=HEADERS, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()
```

Your backend is the source of truth for "who is this person and what should they see" — from your own auth system, IdP claims, or app database. Omni just stores and enforces whatever you send. Pass the current values on every call rather than assuming they persist from creation; if you're provisioning via bulk SCIM instead, group/attribute assignment is a separate step there too (SCIM-created users default to plain Organization Member with no group memberships).

![Config bar and terminal trace of generate-session + list-embed-users](./screenshots/02_step1_preauth.png)

### Caching the lookup

Steps 1a/1b are wasted work if you repeat them on every message in a session — cache the resolved ID by email for the life of the session:

```python
_embed_user_cache = {}

def get_embed_user_id(email, name):
    if email in _embed_user_cache:
        return _embed_user_cache[email]
    upsert_embed_user(email, name)
    user_id = find_embed_user_by_filter(email) or resolve_embed_user_id(email)
    _embed_user_cache[email] = user_id
    return user_id
```

---

## 2. Call the agentic endpoint (AI Jobs API)

Three calls: create job → poll status → stream result. All scoped to the `userId` from step 1, so permissions, RLS, and topic access all enforce automatically.

```python
def ask_omni(prompt, model_id, embed_user_id, conversation_id=None):
    body = {"modelId": model_id, "prompt": prompt}
    if conversation_id:
        body["conversationId"] = conversation_id

    job = requests.post(
        f"{OMNI_BASE_URL}/api/v1/ai/jobs",
        headers=HEADERS,
        params={"userId": embed_user_id},
        json=body,
        timeout=15,
    ).json()

    while True:
        status = requests.get(f"{OMNI_BASE_URL}/api/v1/ai/jobs/{job['jobId']}",
                               headers=HEADERS, timeout=15).json()
        if status["state"] in ("COMPLETE", "FAILED", "CANCELLED"):
            break
        time.sleep(2)

    if status["state"] != "COMPLETE":
        raise RuntimeError(f"Job {status['state']}: {status.get('error', {}).get('message')}")

    result = requests.get(f"{OMNI_BASE_URL}/api/v1/ai/jobs/{job['jobId']}/result",
                           headers=HEADERS, timeout=15).json()

    return {
        "message": result.get("message", ""),          # markdown answer — render directly
        "job_id": job["jobId"],                          # needed for step 3
        "conversation_id": job.get("conversationId"),    # pass back in for follow-ups
        "actions": result.get("actions", []),
    }
```

Reuse `conversation_id` on the next call in the same thread for follow-ups ("break that down by region"). Typical `EXECUTING` polling takes anywhere from a few seconds to ~15-20s depending on query complexity — that latency is Omni doing real work against your warehouse, not something to optimize away in your client.

### 2d — Save `omniChatUrl` and hand the user into Omni

When the job completes, save the `omniChatUrl` that comes back with it — that's the conversation's home inside Omni. To let the user open it, treat its path as the `contentPath` for the SSO embed flow of your choice, so they arrive authenticated client-side:

```python
def sso_url_for_chat(omni_chat_url, email, name):
    path = urlparse(omni_chat_url).path if omni_chat_url.startswith("http") else omni_chat_url
    resp = requests.post(
        f"{OMNI_BASE_URL}/api/v1/embed/sso/generate-url",
        headers=HEADERS,
        json={"contentPath": path, "externalId": email, "name": name},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["url"]   # signed URL — render as "Open in Omni", or load in an iframe
```

Same `externalId` (and groups / userAttributes) as §1c so they land in Omni as the same embed user they asked the question as.

![Terminal trace of create job through poll through result through vis](./screenshots/03_step2_ai_jobs_terminal.png)

---

## 3. Get the chart image

**This turned out to be much simpler than initially assumed.** There's a dedicated endpoint for exactly this — no need to materialize the AI's query into a saved dashboard and download that. Confirmed working end-to-end against a live instance, rendering a real chart:

```
GET /api/v1/ai/jobs/{jobId}/vis
```

```python
def render_chart_image(job_id):
    resp = requests.get(f"{OMNI_BASE_URL}/api/v1/ai/jobs/{job_id}/vis",
                         headers=HEADERS, timeout=30)

    if resp.status_code == 422:
        # The answer was a single scalar value (e.g. "what was total revenue") —
        # there's nothing to chart. This is expected, not an error.
        return {"status": "no_chart"}

    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "image" in content_type:
        b64 = base64.b64encode(resp.content).decode("ascii")
        return {"status": "ok", "data_url": f"data:{content_type};base64,{b64}"}

    return {"status": "ok", "raw": resp.json()}
```

Two behaviors confirmed live:
- **Scalar answers** ("what was revenue last quarter") → `422 {"error": "Visualization type is not renderable as an image"}`. Treat this as a normal outcome, not a failure — just render the text answer with no image.
- **Chart-shaped answers** ("revenue by week last quarter") → real image bytes back, renderable directly via a `data:` URL in an `<img>` tag.

![Side by side: scalar answer with no chart vs. breakdown answer with chart](./screenshots/04_step3_scalar_vs_chart.png)

---

## 4. Putting it together (request flow)

```
User asks a question in external chat
        │
        ▼
Resolve/upsert embed userId (cached after first lookup)   (§1)
        │
        ▼
POST /api/v1/ai/jobs?userId=...                            (§2)
        │
        ▼
Poll /api/v1/ai/jobs/{id} until COMPLETE
        │
        ▼
GET .../result → markdown message + job_id (+ save omniChatUrl → generate-url for "Open in Omni")
        │
        ▼
GET .../jobs/{id}/vis                                       (§3)
        │
   ┌────┴────┐
 422          image bytes
(no chart)    (render inline)
        │
        ▼
Render markdown message [+ chart image] in chat UI
```

---

## Gotchas encountered building this

- **`python-dotenv`'s `load_dotenv()` doesn't override existing shell environment variables by default.** If `OMNI_BASE_URL` or similar got exported in a terminal session at some point (e.g. from earlier ad-hoc testing against a different instance), it silently wins over `.env`. Use `load_dotenv(override=True)` to make the config file authoritative, and `echo $VAR_NAME` to check for stale shell exports if the app is hitting the wrong instance for no apparent reason.
- **SCIM filter on `userName` returned nothing on the playground instance** — the email lives in `embedExternalId`. Filter on that first.
- **SCIM embed user listing paginates** — don't assume a freshly-created user is on page one.
- **The `/vis` endpoint's 422 is informative, not a bug** — build the "no chart for this answer" case into the UI from the start rather than treating it as an error path.
- **Cache the embed-user resolution per session.** Re-running `generate-session` + a paginated SCIM lookup on every single message is the single biggest avoidable source of latency in this flow — the AI job's own `EXECUTING` time is real backend work and isn't something to optimize away, but the user-lookup overhead is.

![Terminal showing a stray shell env var overriding .env and pointing at the wrong instance](./screenshots/05_gotcha_env_var.png)

---

*Endpoints verified against a live instance via a working test app; sources for anything not directly tested: `docs.omni.co/guides/embed/ai-chat-agent`, `docs.omni.co/api/*`.*
