# Running the bot on a server

The loop only runs while the machine running it is awake. To trade without the
laptop, run it on a small Linux server that stays on. Any provider works; a
$5-a-month machine with 1 CPU and 1 GB of memory is plenty. Hetzner (CX22) and
DigitalOcean (Basic droplet) are the usual choices. Pick Ubuntu 24.04.

Time needed the first time: about 20 minutes.

## 1. Create the server

On the provider's site: create a server, choose Ubuntu 24.04, the smallest
size, and add your SSH key if it offers to (or note the root password it
emails you). Note the server's IP address.

## 2. Connect and install Docker

From PowerShell on the laptop, with the IP from step 1:

```powershell
ssh root@YOUR_SERVER_IP
```

Then on the server:

```bash
apt-get update && apt-get install -y docker.io docker-compose-v2 git
git clone -b claude/kalshi-crypto-bot-handoff-w39dj8 https://github.com/clewvu/my-project.git kalshi-bot
cd kalshi-bot
mkdir -p state secrets
cp deploy/.env.server .env
```

The repository is private, so `git clone` will ask for a GitHub username and
a token. Create a token at github.com, Settings, Developer settings, Personal
access tokens, with repository read access, and paste it as the password.

## 3. Copy the key and fill in the settings

Back on the laptop, in a second PowerShell window, copy the key file up:

```powershell
scp "C:\Users\lewiscc2\Downloads\Claude 2.txt" root@YOUR_SERVER_IP:/root/kalshi-bot/secrets/kalshi-key.txt
```

On the server, put the key id into `.env` (the same id `setup` found on
the laptop; it is the line `KALSHI_API_KEY_ID=` in the laptop's `.env`):

```bash
nano .env      # fill KALSHI_API_KEY_ID=..., Ctrl-O Enter Ctrl-X to save
```

Adjust `TRADE_DOLLARS`, `LOSS_CAP`, `PROFIT_TARGET` there too if wanted.

## 4. Check the connection, then start

```bash
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml run --rm live --env prod status
```

That prints the balance by shard. If the Crypto shard holds less than the
loss cap, move funds first:

```bash
docker compose -f deploy/docker-compose.yml run --rm live --env prod transfer --amount 45 --to 2 --yes
```

Then start everything:

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f live
```

The `live` service passes `--yes`, so it starts trading without the typed
confirmation. It restarts itself after a crash or a reboot, and because the
loop's state lives in `state/live_loop.json` on disk, a restart resumes from
the same P&L and never resets the loss cap.

## 5. Watch the dashboard from anywhere

The dashboard listens only on the server's own localhost. From the laptop:

```powershell
ssh -L 8765:127.0.0.1:8765 root@YOUR_SERVER_IP
```

Leave that window open and browse to http://127.0.0.1:8765. From a phone,
an SSH app such as Termius can do the same tunnel.

## Stopping and restarting

```bash
docker compose -f deploy/docker-compose.yml stop live      # stop trading (positions settle on their own)
docker compose -f deploy/docker-compose.yml start live     # resume
docker compose -f deploy/docker-compose.yml down           # stop everything
```

Or click Stop on the dashboard, which creates `state/STOP`; the loop exits
and will refuse to start until that file is removed
(`rm state/STOP`).

To update the code after a `git push` from a later session:

```bash
cd ~/kalshi-bot && git pull && docker compose -f deploy/docker-compose.yml up -d --build
```

## What the server holds

Your private key in `secrets/`, your `.env`, and the bot's state. Keep the
root password or SSH key safe, keep the server updated
(`apt-get upgrade`), and delete the server when you are done with it.
