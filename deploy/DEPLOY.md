# Deploying Tri Coach to Fly.io (phone access, no Mac needed)

This puts the dashboard on a public HTTPS URL you can "Add to Home Screen" on your
iPhone. It runs even when your Mac is asleep. Cost: ~$0–2/mo (it scales to zero when
idle, so you mostly pay nothing).

Everything's pre-built. You only run the steps below (they need *your* Fly account).

## 1. One-time setup
```bash
brew install flyctl          # install the Fly CLI
fly auth signup              # or: fly auth login  (needs a card for abuse-prevention)
```

## 2. Launch the app (from the coach/ directory)
```bash
cd path/to/tri-coach   # the repo root
fly launch --no-deploy --copy-config --name YOUR-UNIQUE-NAME
```
- Accept the existing `fly.toml`.
- When asked about a database/Redis, say **No**.
- It creates the app; the `[[mounts]]` volume is created on first deploy.

## 3. Create the persistent volume (SQLite + Garmin token live here)
```bash
fly volumes create tricoach_data --region sea --size 1
```

## 4. Push your secrets
```bash
bash deploy/bundle_secrets.sh
```
Copy the **access token** it prints (you'll type it on your phone once), then run the
`fly secrets set …` command it gives you. That ships:
- `ACCESS_TOKEN` — the phone gate
- `ANTHROPIC_API_KEY` — Coach Steve
- `GARMIN_TOKEN_B64` — your already-authenticated Garmin token (avoids a datacenter
  login, which Garmin blocks)

## 5. Deploy
```bash
fly deploy
```
Then open it:
```bash
fly open
```

## 6. Put it on your phone
On the iPhone, open the Fly URL in **Safari** → Share → **Add to Home Screen**.
Launch it, enter your access token once — it's saved on the device. Works anywhere,
Mac off.

---

## Notes & upkeep
- **Updating after code changes:** just `fly deploy` again from `coach/`.
- **Garmin token expiry:** the token refreshes itself on the volume while it stays
  valid. If Garmin ever forces a re-login, re-run `deploy/bundle_secrets.sh` +
  `fly secrets set GARMIN_TOKEN_B64=…` and `fly deploy`.
- **Your Mac copy still works** unchanged — it has no `ACCESS_TOKEN`, so no gate, and
  uses the local `~/.garminconnect` token and local SQLite.
- **Truly $0 forever alternative:** an Oracle Cloud "Always Free" VM will host this for
  literally nothing, but it's a full Linux box you manage yourself (systemd + a reverse
  proxy + TLS). Fly is the low-effort choice; say the word if you want the Oracle path.
