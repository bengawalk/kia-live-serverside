# kia-live-rt-broadcaster

Cloudflare Worker + Durable Object that broadcasts GTFS-RT (`rt.pb`) updates
to connected WebSocket clients, so consumers don't need to poll R2 for
changes.

- `GET /rt/ws` — clients upgrade to a WebSocket here and receive the latest
  `rt.pb` bytes immediately, then a new binary frame each time the feed
  updates.
- `POST /rt/publish` — internal endpoint used by the Python pipeline
  (`src/services/durable_object_updater.py`) to push new `rt.pb` bytes.
  Requires `Authorization: Bearer <DO_UPDATE_SECRET>`.

## Setup

```sh
cd worker
npm install
wrangler secret put DO_UPDATE_SECRET   # must match DO_UPDATE_SECRET in the Python service's .env
```

## Develop

```sh
npm run dev
```

## Deploy

```sh
npm run deploy
```

The Worker is bound to `kia-rt.blrtransit.com` via the `routes` entry in
`wrangler.toml` (requires `blrtransit.com` to already be an active zone on
this Cloudflare account — wrangler provisions the DNS record and TLS cert
automatically on first deploy, no dashboard steps needed).

After deploying, set `DO_WORKER_URL=https://kia-rt.blrtransit.com` in the
Python service's `.env`.