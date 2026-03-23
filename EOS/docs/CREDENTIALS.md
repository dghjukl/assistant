# EOS — Credentials Setup Guide

Both integrations are **optional**. EOS runs fully without them. If a credential file is missing, Google OAuth now fails fast with a clear error log and authorization endpoints return an explicit configuration error. If the integration is disabled in config, it remains disabled.

---

## Discord Bot

### What it enables

EOS joins your Discord server as a bot. By default it only responds when mentioned (`@BotName`). It can also be configured to respond to all messages in a channel.

### Where the credential goes

Create a plain text file at:

```
AI personal files\Discord.txt
```

The file should contain only your bot token on the first line — nothing else:

```
MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.GxxxXx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

That folder already exists in the EOS directory. If you do not see it, create it.

---

### How to get a Discord bot token

**Step 1 — Create a bot application**

1. Go to https://discord.com/developers/applications
2. Click **New Application** — give it any name (this becomes the bot's name)
3. In the left sidebar, click **Bot**
4. Click **Reset Token** and copy the token that appears
   *(You only see this once — copy it now and paste it into `Discord.txt` immediately)*

**Step 2 — Enable the Message Content intent**

Still on the Bot tab:
- Scroll to **Privileged Gateway Intents**
- Enable **Message Content Intent**

This is required for the bot to read message text. Without it, EOS cannot see what users write.

**Step 3 — Invite the bot to your server**

1. In the left sidebar, click **OAuth2 → URL Generator**
2. Under Scopes, check: `bot`
3. Under Bot Permissions, check: `Read Messages/View Channels`, `Send Messages`, `Read Message History`
4. Copy the generated URL at the bottom of the page and open it in your browser
5. Select your server from the dropdown and click **Authorize**

**Step 4 — Verify your config**

In `config.json`, confirm:

```json
"discord": {
    "enabled": true,
    "credential_file": "AI personal files/Discord.txt",
    "respond_only_to_mentions": true,
    "ignore_bots": true
}
```

Set `"enabled": false` if you want Discord loaded but silent.

---

> **Need a walkthrough?** Paste this into any AI assistant:
> *"Walk me through creating a Discord bot application in the Discord Developer Portal on Windows — I need to get the bot token, enable the Message Content Intent, and generate an invite URL with read and send message permissions. Explain each step clearly."*

---

## Google Workspace (Calendar, Gmail, Drive)

### What it enables

EOS can read your Google Calendar events, read Gmail messages, and search Google Drive files.

### Where the credential goes

Place your downloaded OAuth JSON file in:

```
config\google\
```

Use either:
- the default location `config/google/*.json`, or
- an explicit `google.client_secret_path` in `config.json` pointing to a specific JSON file under your managed config storage.

The filename can remain the Google-downloaded name, for example:

```
client_secret_123456789-abc.apps.googleusercontent.com.json
```

Do not commit this file to git.

---

### How to get a Google OAuth credential

**Step 1 — Create a Google Cloud project**

1. Go to https://console.cloud.google.com/
2. Click the project selector at the top of the page → **New Project**
3. Give it any name (e.g. `EOS Integration`) and click **Create**

**Step 2 — Enable the APIs you want**

In your project, go to **APIs & Services → Library** and enable:
- **Google Calendar API** — for calendar read access
- **Gmail API** — for email read/send access
- **Google Drive API** — for Drive file access

Search for each by name, click it, then click **Enable**.

**Step 3 — Configure the OAuth consent screen**

Go to **APIs & Services → OAuth consent screen**:

1. Choose **External** user type → **Create**
2. Fill in:
   - App name: anything (e.g. `EOS`)
   - User support email: your email address
   - Developer contact email: your email address
3. Click **Save and Continue** through Scopes (no changes needed) and Test Users
4. On the **Test Users** step, add your own Google account email address
5. Click **Save and Continue → Back to Dashboard**

**Step 4 — Create OAuth credentials**

Go to **APIs & Services → Credentials**:

1. Click **Create Credentials → OAuth 2.0 Client IDs**
2. Application type: **Desktop app**
3. Name: anything (e.g. `EOS Desktop`)
4. Click **Create**
5. On the confirmation dialog, click **Download JSON**

**Step 5 — Place the file**

Move the downloaded JSON to:

```
config\google\
```

Leave the filename as-is, or set `google.client_secret_path` to the explicit file location you want EOS to use.

**Step 6 — Authorize on first use**

After EOS starts with Google enabled, open the Admin Panel → **Integrations** and click **Connect Google Account**. EOS opens the browser authorization flow from there, saves the resulting token to `data\google_token.json`, and does not prompt again unless you revoke or replace the token.

If authorization expires or fails:
- Delete `data\google_token.json`
- Restart EOS, then open the Admin Panel → **Integrations** and click **Re-authorize**

**Step 7 — Verify your config**

In `config.json`, confirm:

```json
"google": {
    "enabled": true,
    "client_secret_path": "config/google/*.json",
    "token_path": "data/google_token.json",
    "calendar_enabled": true,
    "gmail_enabled": true,
    "drive_enabled": true
}
```

To disable specific services, set their flag to `false`. To disable Google integration entirely without removing the credential file, set `"enabled": false`.

**Controlling Gmail Send at runtime**

Gmail sending can also be toggled without restarting. In the admin panel at **http://127.0.0.1:7860/admin**, go to **Control & Permissions → Capabilities → Google → Gmail Send**. This takes effect immediately and resets to the config.json value on next boot.

---

> **Need a walkthrough?** Paste this into any AI assistant:
> *"Walk me through setting up a Google Cloud project for OAuth2 access to Gmail, Calendar, and Drive on a desktop app — I need to enable the APIs, configure the consent screen for personal use, create a Desktop OAuth client ID, and download the credential JSON file. Step by step please."*

---

## Security Notes

- Credential files are excluded from version control by `.gitignore`
- The Discord token grants full bot-level access to every server it has joined — treat it like a password. If it leaks, reset it immediately in the Discord Developer Portal.
- The Google OAuth JSON file grants the ability to request access to your Google account — keep it private. If it leaks, delete and recreate the OAuth client ID in Cloud Console.
- `data\google_token.json` is generated at runtime and contains a live refresh token. It is also excluded from version control. If it leaks, revoke it from your Google Account's security settings at https://myaccount.google.com/permissions
- Store the Google OAuth client JSON only under `config/google/` or another explicitly configured secure path — never paste tokens directly into `config.json`
