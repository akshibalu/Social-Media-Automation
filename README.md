# AutoPost — Social Media Automation

## Overview

AutoPost is a Flask web app that watches a Google Drive folder, and lets you review, approve, reject, or schedule posts before they go out automatically to:

- **Facebook** (feed post + Story)
- **Instagram** (feed post + Story)
- **YouTube** (for video files)

Workflow: upload a photo/video to Drive → it appears in the dashboard → you approve it (immediately or scheduled) → the app posts it everywhere and moves the file to a "Posted" folder. Rejected files move to a separate folder instead.

Login to the dashboard itself is gated behind Google Sign-In, restricted to a specific list of allowed email addresses.

---

## Project structure

```
autopost/
├── app.py                            # Flask backend — all posting logic, routes, scheduler
├── templates/
│   └── index.html                    # Dashboard UI (HTML/CSS/JS, no secrets)
├── .gitignore                        # Blocks real credential files from being committed
├── .env.example                      # Blank template — copy to .env and fill in
├── service_account.example.json      # Shows the shape of the real file — do not fill by hand
├── youtube_credentials.example.json  # Shows the shape of the real file — do not fill by hand
│
│  ─── created by YOU during setup, never committed to Git ───
├── .env                              # Real config + secrets
├── service_account.json              # Real Google Drive credential (downloaded)
├── youtube_credentials.json          # Real YouTube OAuth credential (downloaded)
│
│  ─── created automatically by the app while running, never committed ───
├── youtube_token.json                # Auto-created after visiting /youtube/auth once
├── posted_files.json                 # Auto-created — tracks what's already been posted
├── post_log.json                     # Auto-created — history shown in the dashboard
├── scheduled_posts.json              # Auto-created — pending scheduled posts
├── ignored_files.json                # Auto-created — tracks rejected files
└── temp_media/                       # Auto-created — temporary folder for serving videos to Instagram
```

---

## Changes you need to make when setting this up

Before running the app, edit these:

### 1. In `app.py` — update hardcoded paths/domains
```python
BASE_DIR = "/home/your-username/autopost"
```
Change `your-username` to your actual server username/path.

Inside `youtube_auth()` and `youtube_callback()`, update both occurrences of:
```python
"redirect_uri": "https://your-domain-name.pythonanywhere.com/youtube/callback"
```
to match your actual domain — it must be **identical** to the redirect URI you register in Google Cloud Console.

### 2. Create `.env` from the template
```bash
cp .env.example .env
```
Fill in every value — real Facebook token, real Drive folder IDs, etc. (see "How to get credentials" below).

### 3. Place your real credential files
- `service_account.json` — downloaded from Google Cloud Console, placed in the project root
- `youtube_credentials.json` — downloaded from Google Cloud Console, placed in the project root

### 4. Share your 3 Drive folders with the service account
Right-click each folder in Google Drive → Share → paste the service account's email (found on its Cloud Console page) → give it **Editor** access. Skipping this causes "file not found" errors even with a correct folder ID.


---

## Install dependencies (pip)

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask python-dotenv requests google-auth google-auth-oauthlib google-api-python-client authlib httplib2 urllib3
```

| Package | Used for |
|---|---|
| `flask` | Web framework / dashboard / API routes |
| `python-dotenv` | Loads `.env` into the app |
| `requests` | Facebook/Instagram Graph API calls |
| `google-auth` | Google Drive + YouTube credential handling |
| `google-auth-oauthlib` | OAuth flow for YouTube login |
| `google-api-python-client` | Google Drive API + YouTube Data API calls |
| `authlib` | Google Sign-In for the dashboard login |
| `httplib2` | Dependency used internally by the YouTube upload logic |
| `urllib3` | HTTP retry handling |

*(`os`, `time`, `json`, `threading`, `io`, `datetime`, `zoneinfo` are Python standard library — no install needed.)*

---

## How to download the credential JSON files

Both JSON files are **downloaded from Google Cloud Console** — they are not typed by hand. The `.example.json` files in this repo only show you the *shape* of each file so you can confirm your download looks right.

### `service_account.json` (Google Drive access)
1. [console.cloud.google.com](https://console.cloud.google.com/) → select/create your project
2. **APIs & Services → Library** → enable **Google Drive API**
3. **APIs & Services → Credentials → Create Credentials → Service Account**
4. Name it, click through, then **Done**
5. Click into the service account → **Keys** tab → **Add Key → Create new key → JSON**
6. A file downloads automatically — rename it to exactly `service_account.json`, place it in the project root
7. Copy the service account's email (shown on its details page) and share your 3 Drive folders with it (Editor access)

### `youtube_credentials.json` (YouTube upload access)
1. Same Cloud project → **APIs & Services → Library** → enable **YouTube Data API v3**
2. **APIs & Services → Credentials → Create Credentials → OAuth Client ID**
3. Application type: **Web application**
4. Authorized redirect URIs → add:
   ```
   https://YOUR-DOMAIN/youtube/callback
   ```
5. Click **Create**
6. In the Credentials list, click the **download icon** next to this new client
7. Rename the downloaded file to exactly `youtube_credentials.json`, place it in the project root

---

## Auto-generated files — what they are and why you don't create them

These files are created **by the app itself**, the first time they're needed. You never create, download, or hand-write them — and none of them should ever be committed to Git 

| File | Created when | Purpose |
|---|---|---|
| `youtube_token.json` | The first time you visit `/youtube/auth` and complete the Google login prompt | Stores your YouTube channel's login session so the app can upload without asking again each time. Auto-refreshes itself before it expires. |
| `posted_files.json` | The first time any file is successfully posted | A simple list of Drive file IDs already posted, so the same file is never posted twice |
| `post_log.json` | The first time any file is processed | Full history of what was posted, to which platforms, and whether it succeeded — this is what populates the "Posted Files" table in the dashboard |
| `scheduled_posts.json` | The first time you approve a file with a future date/time | Tracks pending scheduled posts and their status (pending/posted/failed) |
| `ignored_files.json` | The first time you reject a file | Tracks rejected Drive file IDs so they don't reappear in the review queue |
| `temp_media/` (folder) | The first time a video is processed | Temporary storage so Instagram can fetch video files directly from your server; files are deleted automatically after posting |

If any of these files are deleted, the app simply recreates them empty on the next relevant action — no data recovery needed, though you will lose the post history / posted-tracking that was in them.

---

## Security notes

- Never commit `.env`, `service_account.json`, `youtube_credentials.json`, or `youtube_token.json` — `.gitignore` already blocks these
- `SECRET_KEY` in `.env` must be a real random value, or the login system is insecure
- `PAGE_ACCESS_TOKEN` must be a genuine **Page** token — confirm its type at the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/) before use
