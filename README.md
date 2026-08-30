# Omni Agentic API Demo

A small chat UI that pre-auths an Omni embed user, asks Omni AI a question on their
behalf via the AI Jobs API, and renders the answer plus the chart image inline.
The full writeup of the API flow is in [`docs/omni-embed-agentic-chat-integration.md`](docs/omni-embed-agentic-chat-integration.md).

Hosted for free on [Render](https://render.com) as a web service, deployed straight from GitHub.

## Try it

**https://omni-agentic-chat-demo.onrender.com/** - password `omni-agent123!` (demo only).

It runs against the Omni playground instance. Ask a breakdown question ("revenue by
week last quarter") to get a chart; scalar questions ("total revenue last quarter")
return text only by design. If it has been idle the first load takes ~1 minute to wake.

## Deploy (one-time setup, ~5 minutes)

1. Push this repo to GitHub.
2. Sign up at https://render.com (free; sign in with GitHub so it can see the repo).
3. **New → Blueprint**, select the repo. Render reads `render.yaml` and asks you for
   the values it can't guess:

   | Name | Value |
   |---|---|
   | `OMNI_BASE_URL` | `https://<your-instance>.omniapp.co` |
   | `OMNI_API_KEY` | an **Organization** API key (not a PAT) |
   | `OMNI_MODEL_ID` | the model to query against |
   | `DEMO_PASSWORD` | what visitors type to get in |
   | `OMNI_EMBED_SECRET` | Embed secret from Admin → Embed - makes "Link to this chat in Omni" a signed SSO embed URL |
   | `OMNI_EMBED_LOGIN_URL` | embed login URL, e.g. `https://<org>.embed-omniapp.co/embed/login` (leave blank if your host follows the `.omniapp.co` → `.embed-omniapp.co` pattern) |

   `SECRET_KEY` is generated for you. Leave the three `OMNI_*` values blank to run in
   **mock mode** with fake data.
4. Click **Apply**. First build takes ~2 minutes; the app comes up at
   `https://omni-agentic-demo.onrender.com` (Render appends a suffix if the name is taken).
   Every later push to `main` redeploys automatically.

Free-tier behaviour worth knowing before a customer call: the service **sleeps after
15 minutes idle and takes ~1 minute to wake**, so open the URL a few minutes before
the demo. Free instances share 750 hours/month per workspace - plenty for one demo app.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values, or leave OMNI_* blank for mock mode
python app.py          # http://localhost:7860
```

Or with Docker (not needed for Render, kept for other hosts):

```bash
docker build -t omni-agent-demo .
docker run --rm -p 7860:7860 --env-file .env omni-agent-demo
```

## How it works

| Step | Endpoint | Notes |
|---|---|---|
| 1a. Upsert embed user | `POST /api/v1/embed/sso/generate-session` | `externalId` = email; groups / userAttributes / connectionRoles set here every call |
| 1b. Resolve user id | `GET /api/scim/v2/embed/Users` | `filter=embedExternalId eq "<email>"` (then `userName`); paginated scan as fallback. Cached per email. |
| 2. Ask | `POST /api/v1/ai/jobs?userId=…` → poll `GET /ai/jobs/{id}` → `GET …/result` | `conversationId` reused for follow-ups |
| 2d. Link to chat | signed `…/embed/login?contentPath=/chat?chat=<conversationId>&…&signature=` | Standard-SSO "magic URL", HMAC-signed locally with the Embed secret, so the embed user lands in the conversation already logged in. Falls back to a plain `/chat?chat=<id>` link if no secret is set. |
| 3. Chart | `GET /api/v1/ai/jobs/{id}/vis` | Image bytes, or `422` when the answer is a scalar (no chart - expected) |

## Security notes

- The Organization API key never leaves the server; the browser only talks to `/ask`.
- The password gate is a single shared password - fine for demos, not for production.
  For anything real, replace it with your own auth and derive the embed user's email
  (and groups / user attributes) from that identity instead of the text box in the UI.
- Anyone with the password can type any email into the embed-user box and query as
  that embed user, so treat the password like the API key's little sibling.
- Render's free tier sleeps after 15 min idle (~1 min to wake) and its proxy times out
  requests at ~100s, so the app caps AI-job polling at 90s.
