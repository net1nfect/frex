# Authentication

The API can be protected with a bearer token. Authentication is **off by
default** so existing deployments keep working; enabling it is a single
environment variable on both sides.

## Server

```bash
export FREX_API_TOKEN="$(openssl rand -hex 32)"
uv run --project frex frex serve
```

When the variable is set:

- every request must send `Authorization: Bearer <token>`;
- a missing or wrong token returns `401 {"detail": "Unauthorized"}`;
- `/`, `/docs`, `/redoc`, `/openapi.json`, and `/static/*` stay public.

When the variable is empty or unset, the server logs a warning and accepts
unauthenticated requests.

## Dispatcher

`FrexClient` reads the same variable:

```bash
export FREX_API_TOKEN="<same token>"
uv run --project frex frex dispatch --config dispatch.yaml
```

No config file change is needed. The token is sent on every request made by
the client's per-thread session.

## Browser UI

The single-page UI asks for the token once and stores it in `localStorage`.
Requests made by the UI include the bearer header.

## Rotating the token

1. Stop the dispatcher.
2. Change `FREX_API_TOKEN` on the server and restart it.
3. Change the variable in the dispatcher environment and restart it.
4. Reload the UI and enter the new token.

There is no token database and no session state; rotation is immediate.
