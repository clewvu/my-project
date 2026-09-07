# Running the bot on a server, and watching it from your phone

The loop only runs while the machine running it is awake. To trade with the
laptop off, run everything on a small Linux server that stays on, and reach
the dashboard from your phone over a private network. Any provider works; a
$5-a-month machine with 1 CPU and 1 GB of memory is plenty. Hetzner (CX22)
and DigitalOcean (Basic droplet) are the usual choices. Pick Ubuntu 24.04.

Time needed the first time: about 30 minutes. Afterwards the server runs
the live loop, the paper loop, the recorder, the learner and the dashboard
on its own, restarting each after a crash or a reboot, and the loop's state
lives on disk so a restart resumes from the same P&L and never resets the
loss cap.

## 1. Create the server

On the provider's site: create a server, choose Ubuntu 24.04, the smallest
size, and add your SSH key if it offers to (or note the root password it
emails you). Note the server's IP address.

## 2. Connect and run the bootstrap

From PowerShell on the laptop, with the IP from step 1:

```powershell
ssh root@YOUR_SERVER_IP
```

Then on the server, one line:

```bash
curl -fsSL https://raw.githubusercontent.com/clewvu/my-project/claude/kalshi-crypto-bot-handoff-w39dj8/deploy/setup.sh | bash
```

That installs Docker and Tailscale, turns on a firewall that allows only SSH
and Tailscale, clones the repository into `~/kalshi-bot`, and creates the
`state` and `secrets` folders and a `.env` from the template. If the
repository is private the clone asks for a GitHub username and a token
(github.com, Settings, Developer settings, Personal access tokens, with
repository read access; paste the token as the password). If `curl` cannot
reach the file because of that, copy `deploy/setup.sh` up with `scp` and run
`bash setup.sh` instead.

## 3. Join the server to your private network

Still on the server:

```bash
tailscale up
```

It prints a login link. Open it on any device, sign in (Google or Apple
account works), and the server joins your network. Then:

```bash
tailscale ip -4
```

Note the address it prints. It starts with `100.` and only your own devices
can reach it. That is what makes the dashboard safe to expose.

## 4. Copy the key and fill in the settings

Back on the laptop, in a second PowerShell window, copy the key file up:

```powershell
scp "C:\Users\lewiscc2\Downloads\Claude 2.txt" root@YOUR_SERVER_IP:/root/kalshi-bot/secrets/kalshi-key.txt
```

On the server, edit `.env`:

```bash
cd ~/kalshi-bot && nano .env
```

Fill in three lines (Ctrl-O, Enter, Ctrl-X saves):

* `KALSHI_API_KEY_ID=` the same id `setup` found on the laptop; it is the
  `KALSHI_API_KEY_ID=` line in the laptop's `.env`.
* `DASHBOARD_PASSWORD=` a password you will type on your phone.
* `DASHBOARD_BIND=` the `100.` address from step 3.

`TRADE_DOLLARS` (10), `LOSS_CAP` (50) and `PROFIT_TARGET` (0) are there too.

Optional: carry the laptop's history across so the review and the sizing
tiers keep what they have learned. From the laptop:

```powershell
scp C:\Users\lewiscc2\kalshi-bot\state\live_loop.json C:\Users\lewiscc2\kalshi-bot\state\decisions.jsonl C:\Users\lewiscc2\kalshi-bot\state\alerts.jsonl root@YOUR_SERVER_IP:/root/kalshi-bot/state/
```

Stop the laptop's loop first, so two loops never trade the same account.

## 5. Check the connection, then start

```bash
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml run --rm live --env prod status
```

That prints the balance by shard. If the Crypto shard holds less than the
loss cap, move funds first:

```bash
docker compose -f deploy/docker-compose.yml run --rm live --env prod transfer --amount 55 --to 2 --yes
```

Then start everything:

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f live
```

The `live` service passes `--yes`, so it starts trading without the typed
confirmation. Ctrl-C leaves the log; the services keep running.

## 6. The dashboard on your phone

Install the Tailscale app on the phone (App Store or Play Store), sign in
with the same account as step 3, and switch it on. Then open, in the phone's
browser:

```
http://100.x.y.z:8765
```

with the address from step 3. Enter any username and the dashboard password.
Use the browser's "Add to Home Screen" so it opens like an app. The page
works on a phone-sized screen: the tabs, the Pause and Stop buttons, the
tables, the trade detail. Pause, Resume and Stop from the phone do exactly
what they do on the laptop.

The same address works from the laptop when the Tailscale app is running
there. Without Tailscale, `DASHBOARD_BIND=127.0.0.1` keeps the dashboard on
the server only, reachable through an SSH tunnel:

```powershell
ssh -L 8765:127.0.0.1:8765 root@YOUR_SERVER_IP
```

then http://127.0.0.1:8765 while that window is open.

Do not put the dashboard on the public internet. The password protects
against a casual visitor, not against the internet, and the page can stop a
live loop.

## Stopping and restarting

```bash
docker compose -f deploy/docker-compose.yml stop live      # stop trading (positions settle on their own)
docker compose -f deploy/docker-compose.yml start live     # resume
docker compose -f deploy/docker-compose.yml down           # stop everything
```

Or click Stop on the dashboard, which creates `state/STOP`; both loops exit
and refuse to start until that file is removed (`rm state/STOP`, or Clear
stop file on the dashboard, then `start`). Pause holds new entries on both
loops without exiting.

Reviews on the server:

```bash
docker compose -f deploy/docker-compose.yml run --rm live review
docker compose -f deploy/docker-compose.yml run --rm live review --live-state state/paper_loop.json --decisions state/paper_decisions.jsonl
docker compose -f deploy/docker-compose.yml run --rm live quote-test
```

To update the code after a `git push` from a later session:

```bash
cd ~/kalshi-bot && git pull && docker compose -f deploy/docker-compose.yml up -d --build
```

## What the server holds

Your private key in `secrets/`, your `.env` with the dashboard password,
and the bot's state. Keep the root password or SSH key safe, keep the server
updated (`apt-get upgrade`), and delete the server when you are done with
it. Tailscale is free for personal use; the dashboard is never reachable
from outside your own devices.
