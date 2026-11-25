# Webhook relay (optional): react in seconds instead of at the next poll

By default the worker polls GitHub every 30–60 seconds and needs no public URL or
inbound port. A webhook relay lets GitHub tell the worker "something changed in
`owner/repo`" the moment it happens, so the worker polls that repo right away.

Without a relay nothing changes. With one, polling still runs on its normal schedule;
the relay only makes the next poll of one repo come sooner.

## What a relay event can and cannot do

- Every delivery is checked against GitHub's `X-Hub-Signature-256` header
  (HMAC-SHA256 with your App's webhook secret, compared in constant time). A delivery
  that does not verify is dropped.
- A verified delivery does one thing: it asks the worker to poll its repo now. The
  worker reads nothing else from the payload. The poller re-reads GitHub itself and
  decides everything, including the rule that only users on `allowed_users` can start
  work. A forged or unsigned event is dropped. A replayed delivery whose
  `X-GitHub-Delivery` id was among the last 1,000 verified ones is dropped too. Nudges
  never poll more often than once per `relay_min_poll_gap_seconds` (default 15): nudges
  inside that floor are coalesced into one poll when it passes, so a flood of deliveries
  costs at most one extra poll per floor.
- The repo to poll is read from the exact bytes the signature covers (`raw_body` when
  that is what verified, else the compact `body`), never from an unsigned field.
- If the relay is down, slow or misconfigured, the worker logs it, backs off (1 s,
  doubling, at most 5 minutes) and reconnects. It never stops the worker.

## Why to run your own relay

A relay sees every webhook GitHub sends for your App. Those payloads hold the full text
of the issues, comments, pull requests and reviews on every repo the App is installed
on, private repos included.

foreman-inator keeps code, traces and issue text on your Mac (SPEC invariant 7c, local
by default). A third-party relay such as the public `smee.io` breaks that spirit: the
payload text leaves your machine, passes through someone else's server, and anyone who
learns your channel URL can read the stream. The signature check stops a stranger from
faking events, but not from reading real ones.

So use a public relay only to try the feature on public repos. For anything private, run
a relay you control.

## Running a self-hosted relay

The client speaks the smee.io protocol, so the simplest self-hosted relay is the
smee.io server itself (https://github.com/probot/smee.io, MIT licensed):

1. Deploy it somewhere GitHub can reach over HTTPS (a small VPS, Fly.io, your own
   server behind a reverse proxy). For example:

   ```sh
   git clone https://github.com/probot/smee.io && cd smee.io
   docker build -t my-relay . && docker run -d -p 127.0.0.1:3000:3000 my-relay
   ```

   Put TLS in front of it (Caddy, nginx, or your platform's HTTPS); the container only listens on 127.0.0.1.
2. Pick a long random channel name; it works as a password for reading the stream:

   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

   Your relay URL is `https://relay.example.com/<channel>`. GitHub POSTs deliveries to
   it, and the worker reads them from the same URL with `Accept: text/event-stream`.
3. Keep the channel private, and do not enable the server's web UI for others.

Any relay works if, on the same URL, it accepts GitHub's `POST` and serves an SSE stream
whose messages carry JSON with the lowercased request headers as keys and the parsed body
under `body`. A relay you write yourself should also forward the exact request body as a
string under `raw_body`: the signature covers GitHub's raw bytes, and the worker checks
`raw_body` first. Without it the worker checks the re-serialized `body`, which matches
GitHub's compact JSON for ordinary payloads; a payload that does not match is dropped
and picked up by the next regular poll.

Only `https://` relay URLs are accepted, plus `http://` to `localhost` / `127.0.0.1`
for a relay on your own machine (for example behind a tunnel you run).

## Setting it up

1. Choose the relay URL before `fman setup` registers the App. With a relay
   configured, the App manifest gains a webhook (`hook_attributes.url` = your relay URL)
   and subscribes to `issues`, `issue_comment`, `pull_request`, `pull_request_review`
   and `pull_request_review_comment`. Without one, the App has no webhook.
2. GitHub creates the webhook secret when the App is registered. Setup stores it in
   the macOS Keychain; `config.toml` holds only a reference:

   ```toml
   relay_url = "https://relay.example.com/your-long-random-channel"
   relay_secret_ref = "keychain:foreman-inator/relay-webhook-secret"
   ```

   For an App registered earlier, open the App's settings on GitHub, turn on
   "Webhook", set the URL to your relay, generate a secret, subscribe to the events
   above, and store the same secret with
   `fman config set-key relay-webhook-secret`.
3. Restart the worker. Its log shows `relay: connected to <host>` (the host only; the
   channel path and the secret are never logged).

To turn it off, remove `relay_url` from `config.toml` (and the webhook from the App's
settings if you want GitHub to stop sending deliveries).
