# Step-by-step setup guide

Follow these steps in order to deploy the household finance tracker on Oracle Cloud Free Tier.

---

## Step 1 — Provision Oracle VM

⏸ **MANUAL STEP REQUIRED**
**Task:** Create a new Compute Instance on Oracle Cloud Free Tier
**Where:** Oracle Cloud Console → Compute → Instances → Create Instance
**Settings:**
- Image: Ubuntu 22.04 (Canonical)
- Shape: VM.Standard.A1.Flex — **4 OCPU, 24 GB RAM** (Free Tier eligible)
- Networking: Select existing VCN with a public subnet; assign a public IP
- SSH key: upload your public key (or download the generated private key)
- Boot volume: set to **100 GB** (within the 200 GB free allowance)
**Expected output:** Instance in "Running" state with a public IP address
**Then:** Note down the public IP

---

## Step 2 — Open firewall ports

⏸ **MANUAL STEP REQUIRED**
**Task:** Open ports 80 and 443 in the Security List AND the instance's iptables
**Where:** Oracle Cloud Console → Networking → Virtual Cloud Networks → Security Lists
**Add ingress rules:**
- Source CIDR: `0.0.0.0/0`, Protocol: TCP, Port: 80
- Source CIDR: `0.0.0.0/0`, Protocol: TCP, Port: 443
**Also run on the VM:**
```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```
**Expected output:** Both rules visible in Security List

---

## Step 3 — Point your domain

⏸ **MANUAL STEP REQUIRED**
**Task:** Create an A record pointing your domain to the VM public IP
**Where:** Your DNS provider (Cloudflare, Route53, etc.)
**Record:** `finance.yourdomain.com` → `<VM public IP>`
**Expected output:** `dig finance.yourdomain.com` resolves to the VM IP (allow ~5 min to propagate)

---

## Step 4 — Install Docker on the VM

SSH into the VM then run:
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
sudo apt-get install -y docker-compose-plugin
newgrp docker
```

---

## Step 5 — Clone the repository

```bash
git clone <your-repo-url> ~/expense-tracker-v2
cd ~/expense-tracker-v2
```

---

## Step 6 — Create .env file

```bash
cp .env.example .env
nano .env   # fill in all values
```

Required values:
- `DOMAIN` — your domain from Step 3
- `POSTGRES_PASSWORD` — strong random password
- `FIREFLY_APP_KEY` — run: `openssl rand -base64 32 | tr -d '\n'`
- `ANTHROPIC_API_KEY` — obtain from console.anthropic.com
- Leave `FIREFLY_ACCESS_TOKEN` empty for now (filled in Step 8)

---

## Step 7 — Start services (first time, without Firefly token)

```bash
docker compose up -d postgres firefly firefly-importer caddy
```

Wait ~60 seconds for Firefly to initialise its database.

---

## Step 8 — Create Firefly III admin account

⏸ **MANUAL STEP REQUIRED**
**Task:** Complete Firefly III initial setup and generate a Personal Access Token
**Where:** Browser → `https://finance.yourdomain.com`
1. Complete the registration wizard (create your admin account)
2. Go to: Profile (top right) → OAuth → Personal Access Tokens → **Create new token**
3. Name it `household-finance`, click Create, copy the token
**Then:** Paste the token into `.env` as `FIREFLY_ACCESS_TOKEN=<token>`

---

## Step 9 — Start remaining services

```bash
docker compose up -d parser streamlit
```

---

## Step 10 — Run Firefly setup script

```bash
export $(grep -v '^#' .env | xargs)
python3 scripts/setup_firefly.py
```

This creates all 5 accounts, 11 categories, and ~50 keyword rules.

---

## Step 11 — Set Streamlit passwords

⏸ **MANUAL STEP REQUIRED**
**Task:** Generate bcrypt hashes for both users and update `streamlit/config.yaml`
**Where:** Terminal on the VM
```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'your_harsh_password', bcrypt.gensalt()).decode())"
python3 -c "import bcrypt; print(bcrypt.hashpw(b'your_wife_password', bcrypt.gensalt()).decode())"
```
Replace the `PLACEHOLDER_HASH_FOR_HARSH` and `PLACEHOLDER_HASH_FOR_WIFE` values in `streamlit/config.yaml` with the output.

Then rebuild:
```bash
docker compose build streamlit
docker compose up -d streamlit
```
**Expected output:** Login page at `https://finance.yourdomain.com/dashboard` accepts credentials

---

## Step 12 — Set up weekly backup cron

```bash
mkdir -p ~/backups
chmod +x ~/expense-tracker-v2/scripts/backup.sh
(crontab -l 2>/dev/null; echo "0 2 * * 0 ~/expense-tracker-v2/scripts/backup.sh >> ~/backups/backup.log 2>&1") | crontab -
```

---

## Verification checklist

- [ ] `https://finance.yourdomain.com` shows Firefly III login
- [ ] `https://finance.yourdomain.com/dashboard` shows Streamlit login
- [ ] Both `harsh` and `wife` can log into Streamlit
- [ ] Upload & Import page can parse a sample PDF
- [ ] Dashboard shows charts after importing transactions
- [ ] Ask AI responds to "What is my current total spend this month?"
- [ ] `docker compose logs parser` shows no errors
- [ ] Weekly backup runs: `ls ~/backups/`
