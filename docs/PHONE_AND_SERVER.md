# Phone access and running with the laptop off

Saved 2026-09-07 for later. The full walk-through is `deploy/README.md`;
this is the short version with the commands in order.

One constraint first: nothing running on the laptop survives the laptop
being off. Running with the computer off means a small server. Phone access
on the same Wi-Fi works today.

## Phone access today, same Wi-Fi as the laptop

The dashboard takes a password and refuses to open beyond the laptop
without one. Close the old dashboard window and paste, choosing your own
password:

```powershell
cd C:\Users\lewiscc2\kalshi-bot; .\.venv\Scripts\Activate.ps1; git pull; pip install -e ".[dev]" -q; kalshi-bot demo-ui --host 0.0.0.0 --port 8790 --password ChangeMe
```

Find the laptop's address with `ipconfig` (the IPv4 address, usually
192.168.x.x), then open `http://192.168.x.x:8790` on the phone and enter any
username with that password. Windows may ask once to allow Python through
the firewall on private networks. Use "Add to Home Screen" so it opens like
an app. If the paper loop is also running, a selector under the masthead
switches between Live and Paper.

## Running with the computer off: the shape of it

1. Create a $5 Ubuntu 24.04 server at Hetzner (CX22) or DigitalOcean.
   GitHub cannot be the server: Actions jobs die after six hours, accept no
   connections, and its terms forbid it. Oracle's Always Free tier or a
   Raspberry Pi at home also work with the same commands.
2. Run the bootstrap script, one line. It installs Docker and Tailscale,
   sets a firewall that allows only SSH and Tailscale, and clones the repo.
3. `tailscale up` puts the server on a private network only your devices
   can see. Install the Tailscale app on the phone, same login.
4. Copy the key file up and fill three lines in `.env`: the key id, a
   dashboard password, and the server's Tailscale address.
5. `docker compose up -d`. The live loop, the paper loop, the recorder, the
   learner and the dashboard all run, restart after crashes and reboots,
   and keep their state on disk so a restart never resets the cap.
6. On the phone, open `http://100.x.y.z:8765`. That address works anywhere,
   never from the public internet.

Two cautions. Stop the laptop's loop before the server's starts, so two
loops never trade the same account. The Docker build and the Tailscale
steps were not tested from the development sandbox; paste any error back.

## The commands, in order

Create the server on the provider's site first and note its IP. Replace
`YOUR_SERVER_IP` everywhere below.

**1. Laptop, PowerShell.** Stop the laptop's loop (Ctrl-C, or Stop on the
dashboard). Copy the key and the history up; enter the server's root
password when asked.

```powershell
scp "C:\Users\lewiscc2\Downloads\Claude 2.txt" root@YOUR_SERVER_IP:/root/kalshi-key.txt
```

```powershell
scp C:\Users\lewiscc2\kalshi-bot\state\live_loop.json C:\Users\lewiscc2\kalshi-bot\state\decisions.jsonl C:\Users\lewiscc2\kalshi-bot\state\alerts.jsonl root@YOUR_SERVER_IP:/root/
```

**2. Connect:**

```powershell
ssh root@YOUR_SERVER_IP
```

**3. Server: bootstrap.** If the clone asks for a login, use the GitHub
username and a personal access token as the password.

```bash
curl -fsSL https://raw.githubusercontent.com/clewvu/my-project/claude/kalshi-crypto-bot-handoff-w39dj8/deploy/setup.sh | bash
```

**4. Join the private network.** It prints a link; open it on the phone or
laptop and sign in. Then note the `100.` address.

```bash
tailscale up
```

```bash
tailscale ip -4
```

**5. Put the files in place:**

```bash
cd ~/kalshi-bot && mv /root/kalshi-key.txt secrets/kalshi-key.txt && mv /root/live_loop.json /root/decisions.jsonl /root/alerts.jsonl state/ 2>/dev/null; chmod 600 secrets/kalshi-key.txt
```

**6. Fill in the settings.** Replace the three values first: the key id
(the `KALSHI_API_KEY_ID=` line in the laptop's `.env`), a password, and
the `100.` address from step 4.

```bash
cd ~/kalshi-bot && sed -i 's/^KALSHI_API_KEY_ID=.*/KALSHI_API_KEY_ID=PASTE_YOUR_KEY_ID/; s/^DASHBOARD_PASSWORD=.*/DASHBOARD_PASSWORD=ChooseAPassword/; s/^DASHBOARD_BIND=.*/DASHBOARD_BIND=100.x.y.z/' .env && grep -E "KEY_ID|DASHBOARD" .env
```

**7. Build and check the connection.** Prints the balance by shard.

```bash
cd ~/kalshi-bot && docker compose -f deploy/docker-compose.yml build && docker compose -f deploy/docker-compose.yml run --rm live --env prod status
```

If the Crypto shard shows less than $55:

```bash
docker compose -f deploy/docker-compose.yml run --rm live --env prod transfer --amount 55 --to 2 --yes
```

**8. Start everything and watch the live log** (Ctrl-C leaves the log; the
services keep running):

```bash
cd ~/kalshi-bot && docker compose -f deploy/docker-compose.yml up -d && docker compose -f deploy/docker-compose.yml logs -f live
```

**9. Phone:** install Tailscale, sign in with the same account as step 4,
switch it on, open `http://100.x.y.z:8765`, any username, the password
from step 6. Add to home screen.

## Later, on the server

```bash
cd ~/kalshi-bot && docker compose -f deploy/docker-compose.yml ps
```

```bash
cd ~/kalshi-bot && git pull && docker compose -f deploy/docker-compose.yml up -d --build
```

```bash
cd ~/kalshi-bot && docker compose -f deploy/docker-compose.yml run --rm live review
```
