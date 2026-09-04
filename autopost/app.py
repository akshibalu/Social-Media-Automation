from flask import Flask, render_template, jsonify, request, redirect, url_for, session, Response
import os
import time
import requests
import json
import threading
import urllib3
import io
import pickle
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo
from authlib.integrations.flask_client import OAuth

urllib3.disable_warnings()
load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"]  = "1"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY is not set in .env — refusing to start with an insecure default.")
# ── Google Sign-In setup ────────────────────────
oauth = OAuth(app)
google_oauth = oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

ALLOWED_EMAILS = [e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()]

@app.before_request
def require_login():
    open_paths = ('/login', '/auth/callback')
    if request.path.startswith(open_paths) or request.path.startswith('/media/'):
        return
    if not session.get('user_email'):
        return redirect('/login')

@app.route('/login')
def login():
    redirect_uri = url_for('auth_callback', _external=True)
    return google_oauth.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def auth_callback():
    token = google_oauth.authorize_access_token()
    user_info = token.get('userinfo')
    email = user_info['email'].lower()
    if email not in ALLOWED_EMAILS:
        session.clear()
        return "❌ Access denied — this Google account is not authorized to view this dashboard.", 403
    session['user_email'] = email
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# ── Credentials ────────────────────────────────
PAGE_ACCESS_TOKEN    = os.getenv("PAGE_ACCESS_TOKEN")
FB_PAGE_ID           = os.getenv("FB_PAGE_ID")
IG_ACCOUNT_ID        = os.getenv("IG_ACCOUNT_ID")
FOLDER_ID            = os.getenv("FOLDER_ID")          # Main upload folder (inbox source)
INBOX_FOLDER_ID      = os.getenv("INBOX_FOLDER_ID")    # Rejected files go here
POSTED_FOLDER_ID     = os.getenv("POSTED_FOLDER_ID")   # Approved/posted files go here
DEFAULT_CAPTION      = os.getenv("DEFAULT_CAPTION", "Check out our latest update!")
YOUTUBE_TITLE        = os.getenv("YOUTUBE_TITLE", "New Video!")
YOUTUBE_DESCRIPTION  = os.getenv("YOUTUBE_DESCRIPTION", "Check out our latest video!")
YOUTUBE_CATEGORY     = os.getenv("YOUTUBE_CATEGORY", "22")
YOUTUBE_PRIVACY      = os.getenv("YOUTUBE_PRIVACY", "public")
SCOPES               = ["https://www.googleapis.com/auth/drive"]
YOUTUBE_SCOPES       = ["https://www.googleapis.com/auth/youtube.upload"]
POSTED_FILES         = "posted_files.json"
LOG_FILE             = "post_log.json"
SCHEDULE_FILE        = "scheduled_posts.json"
IGNORED_FILE         = "ignored_files.json"
SERVICE_ACCOUNT_FILE = "service_account.json"

# ── Full paths for YouTube files ───────────────
BASE_DIR             = "/home/your domain name/autopost"
YOUTUBE_CREDS_FILE   = f"{BASE_DIR}/youtube_credentials.json"
YOUTUBE_TOKEN_FILE   = f"{BASE_DIR}/youtube_token.json"
YOUTUBE_STATE_FILE   = f"{BASE_DIR}/youtube_state.txt"

# ── HTTP session with retry (renamed from "session" to avoid clashing
#    with Flask's login session object) ─────────
def create_session():
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

http_session = create_session()

# ── App status ─────────────────────────────────
app_status = {
    "running"        : False,
    "last_check"     : "Never",
    "current_action" : "Idle",
    "total_checks"   : 0,
    "youtube_auth"   : False,
    "logs"           : []
}

def add_log(message, level="info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": message, "level": level}
    app_status["logs"].insert(0, entry)
    app_status["logs"] = app_status["logs"][:100]
    print(f"[{entry['time']}] {message}")

# ── Review auth helper ─────────────────────────
def check_review_auth(data):
    """Access is now gated by Google login at the site level (see require_login),
    so approve/reject actions no longer need a separate username/password check."""
    return True

# ── Post log ───────────────────────────────────
def load_post_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            return json.load(f)
    return []

def save_post_log_entry(filename, platforms, failed_platforms, success):
    log = load_post_log()
    log.insert(0, {
        "filename"         : filename,
        "timestamp"        : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "platforms"        : platforms,
        "failed_platforms" : failed_platforms,
        "success"          : success
    })
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

# ── Posted files ───────────────────────────────
def get_posted_files():
    if os.path.exists(POSTED_FILES):
        with open(POSTED_FILES, "r") as f:
            return json.load(f)
    return []

def mark_as_posted(file_id):
    posted = get_posted_files()
    posted.append(file_id)
    with open(POSTED_FILES, "w") as f:
        json.dump(posted, f)

# ── Ignored / rejected files ───────────────────
def get_ignored_files():
    if os.path.exists(IGNORED_FILE):
        with open(IGNORED_FILE, "r") as f:
            return json.load(f)
    return []

def mark_as_ignored(file_id):
    ignored = get_ignored_files()
    if file_id not in ignored:
        ignored.append(file_id)
    with open(IGNORED_FILE, "w") as f:
        json.dump(ignored, f)

# ── Scheduled posts ────────────────────────────
def load_scheduled():
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, "r") as f:
            return json.load(f)
    return []

def save_scheduled(scheduled):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(scheduled, f, indent=2)

# ── Google Drive ───────────────────────────────
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

def download_file(service, file_id, filename):
    add_log(f"⬇️ Downloading: {filename}")
    request_obj = service.files().get_media(fileId=file_id)
    file_path   = "temp_" + filename
    with io.FileIO(file_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request_obj)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    add_log(f"✅ Downloaded: {filename}")
    return file_path

def move_to_folder(service, file_id, target_folder_id, filename):
    try:
        file = service.files().get(fileId=file_id, fields="parents").execute()
        previous_parents = ",".join(file.get("parents", []))
        service.files().update(
            fileId=file_id, addParents=target_folder_id,
            removeParents=previous_parents, fields="id, parents"
        ).execute()
        add_log(f"📁 Moved: {filename}")
    except Exception as e:
        add_log(f"⚠️ Move failed: {e}", "warning")

def make_public_url(service, file_id):
    """Make Drive file public and return a direct download URL."""
    try:
        service.permissions().create(
            fileId=file_id, body={"role": "reader", "type": "anyone"}
        ).execute()
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    except Exception as e:
        add_log(f"⚠️ Public URL failed: {e}", "error")
        return None

# ── Temp media server ──────────────────────────
MEDIA_DIR = os.path.join(BASE_DIR, "temp_media")
os.makedirs(MEDIA_DIR, exist_ok=True)

@app.route("/media/<path:filename>")
def serve_media(filename):
    from flask import send_from_directory
    return send_from_directory(MEDIA_DIR, filename)

def save_media_for_serving(src_path, filename):
    """Copy a downloaded file into MEDIA_DIR and return its public URL."""
    import shutil
    dest = os.path.join(MEDIA_DIR, filename)
    shutil.copy2(src_path, dest)
    base_domain = os.getenv("PUBLIC_DOMAIN", "https://your domain name.pythonanywhere.com")
    return f"{base_domain}/media/{requests.utils.quote(filename)}"

def cleanup_media(filename):
    """Remove a served temp file after posting is done."""
    path = os.path.join(MEDIA_DIR, filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

# ══════════════════════════════════════════════
# YOUTUBE SERVICE
# ══════════════════════════════════════════════
def get_youtube_service():
    """Load YouTube credentials, always attempt refresh so token stays fresh."""
    if not os.path.exists(YOUTUBE_TOKEN_FILE):
        app_status["youtube_auth"] = False
        add_log("⚠️ No YouTube token found — visit /youtube/auth", "warning")
        return None
    try:
        with open(YOUTUBE_TOKEN_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        add_log(f"⚠️ Token file unreadable: {e}", "warning")
        app_status["youtube_auth"] = False
        return None

    from datetime import timezone
    expiry = None
    if data.get("expiry"):
        try:
            expiry = datetime.fromisoformat(data["expiry"].replace("Z", "+00:00"))
        except Exception:
            pass

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", YOUTUBE_SCOPES),
        expiry=expiry,
    )

    if creds.refresh_token:
        try:
            creds.refresh(Request())
            add_log("✅ YouTube token refreshed!")
            token_data = {
                "token"        : creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri"    : creds.token_uri,
                "client_id"    : creds.client_id,
                "client_secret": creds.client_secret,
                "scopes"       : list(creds.scopes) if creds.scopes else YOUTUBE_SCOPES,
                "expiry"       : creds.expiry.isoformat() if creds.expiry else None,
            }
            with open(YOUTUBE_TOKEN_FILE, "w") as f:
                json.dump(token_data, f, indent=2)
        except Exception as e:
            add_log(f"⚠️ Token refresh failed: {e} — trying with existing token", "warning")
    else:
        add_log("⚠️ No refresh_token — re-authenticate at /youtube/auth", "warning")

    if not creds.token:
        app_status["youtube_auth"] = False
        add_log("❌ No access token available", "error")
        return None

    app_status["youtube_auth"] = True
    return build("youtube", "v3", credentials=creds)

# ══════════════════════════════════════════════
# YOUTUBE OAUTH ROUTES
# ══════════════════════════════════════════════
@app.route("/youtube/auth")
def youtube_auth():
    if not os.path.exists(YOUTUBE_CREDS_FILE):
        return "youtube_credentials.json not found!", 404
    with open(YOUTUBE_CREDS_FILE) as f:
        cred_data = json.load(f)
    client_id = cred_data["web"]["client_id"]
    import urllib.parse
    params = {
        "client_id": client_id,
        "redirect_uri": "https://your domain name.pythonanywhere.com/youtube/callback",
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/youtube.upload",
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(auth_url)

@app.route("/youtube/callback")
def youtube_callback():
    if not os.path.exists(YOUTUBE_CREDS_FILE):
        return "youtube_credentials.json not found!", 404
    code = request.args.get("code")
    if not code:
        return "No code received!", 400
    with open(YOUTUBE_CREDS_FILE) as f:
        cred_data = json.load(f)
    client_id     = cred_data["web"]["client_id"]
    client_secret = cred_data["web"]["client_secret"]
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code, "client_id": client_id, "client_secret": client_secret,
                "redirect_uri": "https://your domain name.pythonanywhere.com/youtube/callback",
                "grant_type": "authorization_code"
            }
        ).json()
        if "error" in token_resp:
            return f"<h2>Token error</h2><p style='color:red'>{token_resp}</p><a href='/youtube/auth'>Try again</a>"
        token_data = {
            "token": token_resp.get("access_token"), "refresh_token": token_resp.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": client_id, "client_secret": client_secret,
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
        }
        with open(YOUTUBE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)
        app_status["youtube_auth"] = True
        add_log("✅ YouTube authenticated successfully!")
        return redirect("/")
    except Exception as e:
        add_log(f"❌ YouTube auth failed: {e}", "error")
        return f"<h2>YouTube auth failed</h2><p style='color:red'>{e}</p><a href='/youtube/auth'>Try again</a>"

# ══════════════════════════════════════════════
# POST FUNCTIONS
# ══════════════════════════════════════════════
def post_video_youtube(file_path, filename):
    add_log("▶️ Posting video to YouTube...")
    import httplib2
    from googleapiclient.errors import HttpError

    MAX_RETRIES   = 10
    RETRY_ERRORS  = (HttpError, IOError, OSError, TimeoutError)

    if not os.path.exists(file_path):
        add_log(f"❌ YouTube: local file missing: {file_path}", "error")
        return False

    youtube = get_youtube_service()
    if not youtube:
        add_log("❌ YouTube not authenticated — visit /youtube/auth", "error")
        return False

    title = os.path.splitext(filename)[0] or YOUTUBE_TITLE
    body  = {
        "snippet": {
            "title"      : title,
            "description": YOUTUBE_DESCRIPTION,
            "categoryId" : YOUTUBE_CATEGORY,
        },
        "status": {"privacyStatus": YOUTUBE_PRIVACY},
    }

    fsize = os.path.getsize(file_path)
    add_log(f"   YouTube uploading: {filename} ({fsize // (1024*1024)} MB)")

    media = MediaFileUpload(
        file_path, mimetype="video/*", resumable=True, chunksize=1024 * 1024 * 50
    )
    insert_request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )

    response       = None
    retry          = 0
    last_progress  = -1

    while response is None:
        try:
            status, response = insert_request.next_chunk()
            retry = 0
            if status:
                pct = int(status.progress() * 100)
                if pct != last_progress:
                    add_log(f"   YouTube upload: {pct}%")
                    last_progress = pct
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retry < MAX_RETRIES:
                wait = 2 ** retry
                add_log(f"   YouTube HTTP {e.resp.status} — retry {retry+1}/{MAX_RETRIES} in {wait}s", "warning")
                time.sleep(wait)
                retry += 1
            else:
                import traceback
                add_log(f"❌ YouTube HttpError {e.resp.status}: {e}", "error")
                print(traceback.format_exc())
                return False
        except RETRY_ERRORS as e:
            if retry < MAX_RETRIES:
                wait = 2 ** retry
                add_log(f"   YouTube network error — retry {retry+1}/{MAX_RETRIES} in {wait}s: {e}", "warning")
                time.sleep(wait)
                retry += 1
            else:
                import traceback
                add_log(f"❌ YouTube upload failed after {MAX_RETRIES} retries: {e}", "error")
                print(traceback.format_exc())
                return False
        except Exception as e:
            import traceback
            add_log(f"❌ YouTube unexpected error: {e}", "error")
            print(traceback.format_exc())
            return False

    add_log(f"✅ YouTube Video posted! ID: {response.get('id', '')}")
    return True

def wait_until_ready(container_id, max_attempts=60):
    add_log("⏳ Waiting for Instagram to process...")
    for attempt in range(1, max_attempts + 1):
        try:
            resp = http_session.get(
                f"https://graph.facebook.com/v19.0/{container_id}",
                params={"fields": "status_code,status", "access_token": PAGE_ACCESS_TOKEN},
                timeout=30
            ).json()
            code   = resp.get("status_code", "UNKNOWN")
            status = resp.get("status", "")
            add_log(f"   [{attempt}/{max_attempts}] status_code={code}")
            if code == "FINISHED":
                add_log("✅ Container FINISHED!")
                return True
            elif code == "ERROR":
                add_log(f"❌ Instagram container error: {status}", "error")
                return False
            else:
                wait = 20 if attempt <= 5 else 30 if attempt <= 15 else 60
                time.sleep(wait)
        except Exception as e:
            add_log(f"⚠️ Status check error: {e}", "warning")
            time.sleep(30)
    add_log(f"❌ Instagram timed out", "error")
    return False

def post_photo_facebook(file_path):
    add_log("📘 Posting photo to Facebook...")
    try:
        with open(file_path, "rb") as f:
            r = http_session.post(
                f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos",
                data={"message": DEFAULT_CAPTION, "access_token": PAGE_ACCESS_TOKEN},
                files={"source": f}, timeout=120
            ).json()
        if "id" in r:
            add_log("✅ Facebook Photo posted!")
            return True
        add_log(f"❌ Facebook failed: {r}", "error")
        return False
    except Exception as e:
        add_log(f"❌ Facebook error: {e}", "error")
        return False

def post_video_facebook(file_path):
    add_log("📘 Posting video to Facebook...")
    try:
        with open(file_path, "rb") as f:
            r = http_session.post(
                f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/videos",
                data={"description": DEFAULT_CAPTION, "access_token": PAGE_ACCESS_TOKEN},
                files={"source": f}, timeout=300
            ).json()
        if "id" in r:
            add_log("✅ Facebook Video posted!")
            return True
        add_log(f"❌ Facebook video failed: {r}", "error")
        return False
    except Exception as e:
        add_log(f"❌ Facebook video error: {e}", "error")
        return False

# ── Facebook Page Stories ──────────────────────
def post_photo_facebook_story(file_path):
    """Post a photo as a Facebook Page Story (2-step: unpublished upload → photo_stories)."""
    add_log("📘 Posting photo Story to Facebook...")
    try:
        with open(file_path, "rb") as f:
            upload = http_session.post(
                f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos",
                data={"published": "false", "access_token": PAGE_ACCESS_TOKEN},
                files={"source": f}, timeout=120
            ).json()
        photo_id = upload.get("id")
        if not photo_id:
            add_log(f"❌ Facebook Story photo upload failed: {upload}", "error")
            return False
        r = http_session.post(
            f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photo_stories",
            data={"photo_id": photo_id, "access_token": PAGE_ACCESS_TOKEN},
            timeout=120
        ).json()
        if "post_id" in r or "id" in r or r.get("success"):
            add_log("✅ Facebook Photo Story posted!")
            return True
        add_log(f"❌ Facebook Story publish failed: {r}", "error")
        return False
    except Exception as e:
        add_log(f"❌ Facebook Photo Story error: {e}", "error")
        return False

def post_video_facebook_story(file_path):
    """Post a video as a Facebook Page Story (resumable 3-phase upload: start → transfer → finish)."""
    add_log("📘 Posting video Story to Facebook...")
    try:
        fsize = os.path.getsize(file_path)
        start = http_session.post(
            f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/video_stories",
            data={
                "upload_phase": "start",
                "file_size"   : fsize,
                "access_token": PAGE_ACCESS_TOKEN
            },
            timeout=60
        ).json()
        video_id   = start.get("video_id")
        upload_url = start.get("upload_url")
        if not video_id or not upload_url:
            add_log(f"❌ Facebook Video Story start failed: {start}", "error")
            return False

        with open(file_path, "rb") as f:
            file_bytes = f.read()
        upload_headers = {
            "Authorization": f"OAuth {PAGE_ACCESS_TOKEN}",
            "offset"       : "0",
            "file_size"    : str(fsize),
        }
        up = http_session.post(upload_url, headers=upload_headers, data=file_bytes, timeout=600)
        if up.status_code != 200:
            add_log(f"❌ Facebook Video Story upload failed: {up.text}", "error")
            return False

        finish = http_session.post(
            f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/video_stories",
            data={
                "upload_phase": "finish",
                "video_id"    : video_id,
                "access_token": PAGE_ACCESS_TOKEN
            },
            timeout=120
        ).json()
        if finish.get("success"):
            add_log("✅ Facebook Video Story posted!")
            return True
        add_log(f"❌ Facebook Video Story finish failed: {finish}", "error")
        return False
    except Exception as e:
        add_log(f"❌ Facebook Video Story error: {e}", "error")
        return False

def post_image_instagram(image_url):
    add_log("📸 Posting image to Instagram...")
    try:
        container = http_session.post(
            f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media",
            data={"image_url": image_url, "caption": DEFAULT_CAPTION, "access_token": PAGE_ACCESS_TOKEN},
            timeout=120
        ).json()
        container_id = container.get("id")
        if not container_id:
            add_log(f"❌ IG container failed: {container}", "error")
            return False
        if not wait_until_ready(container_id):
            return False
        for attempt in range(1, 6):
            publish = http_session.post(
                f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media_publish",
                data={"creation_id": container_id, "access_token": PAGE_ACCESS_TOKEN},
                timeout=120
            ).json()
            if "id" in publish:
                add_log("✅ Instagram Image posted!")
                return True
            error_msg  = publish.get("error", {}).get("message", "")
            error_code = publish.get("error", {}).get("code", 0)
            if "not available" in error_msg.lower() or error_code in (9007, 2207026):
                time.sleep(30 * attempt)
            else:
                return False
        return False
    except Exception as e:
        add_log(f"❌ Instagram image error: {e}", "error")
        return False

def post_video_instagram(video_url):
    add_log(f"🎬 Posting Reel to Instagram... URL: {video_url[:60]}...")
    try:
        container = http_session.post(
            f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media",
            data={"media_type": "REELS", "video_url": video_url, "caption": DEFAULT_CAPTION, "access_token": PAGE_ACCESS_TOKEN},
            timeout=300
        ).json()
        add_log(f"   IG container response: {container}")
        container_id = container.get("id")
        if not container_id:
            add_log(f"❌ IG Reel container failed: {container}", "error")
            return False
        if not wait_until_ready(container_id, max_attempts=60):
            return False
        for attempt in range(1, 6):
            publish = http_session.post(
                f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media_publish",
                data={"creation_id": container_id, "access_token": PAGE_ACCESS_TOKEN},
                timeout=120
            ).json()
            if "id" in publish:
                add_log("✅ Instagram Reel posted!")
                return True
            error_msg  = publish.get("error", {}).get("message", "")
            error_code = publish.get("error", {}).get("code", 0)
            add_log(f"   IG publish attempt {attempt} failed: code={error_code} msg={error_msg}", "warning")
            if "not available" in error_msg.lower() or error_code in (9007, 2207026):
                time.sleep(30 * attempt)
            else:
                add_log(f"❌ IG Reel publish failed permanently: {publish}", "error")
                return False
        return False
    except Exception as e:
        import traceback
        add_log(f"❌ Instagram Reel error: {e}", "error")
        print(traceback.format_exc())
        return False

# ── Instagram Stories ──────────────────────────
def post_image_instagram_story(image_url):
    add_log("📸 Posting image Story to Instagram...")
    try:
        container = http_session.post(
            f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media",
            data={"image_url": image_url, "media_type": "STORIES", "access_token": PAGE_ACCESS_TOKEN},
            timeout=120
        ).json()
        container_id = container.get("id")
        if not container_id:
            add_log(f"❌ IG Image Story container failed: {container}", "error")
            return False
        if not wait_until_ready(container_id, max_attempts=30):
            return False
        for attempt in range(1, 6):
            publish = http_session.post(
                f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media_publish",
                data={"creation_id": container_id, "access_token": PAGE_ACCESS_TOKEN},
                timeout=120
            ).json()
            if "id" in publish:
                add_log("✅ Instagram Image Story posted!")
                return True
            error_msg  = publish.get("error", {}).get("message", "")
            error_code = publish.get("error", {}).get("code", 0)
            if "not available" in error_msg.lower() or error_code in (9007, 2207026):
                time.sleep(30 * attempt)
            else:
                add_log(f"❌ IG Image Story publish failed: {publish}", "error")
                return False
        return False
    except Exception as e:
        add_log(f"❌ Instagram Image Story error: {e}", "error")
        return False

def post_video_instagram_story(video_url):
    add_log("🎬 Posting video Story to Instagram...")
    try:
        container = http_session.post(
            f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media",
            data={"media_type": "STORIES", "video_url": video_url, "access_token": PAGE_ACCESS_TOKEN},
            timeout=300
        ).json()
        container_id = container.get("id")
        if not container_id:
            add_log(f"❌ IG Video Story container failed: {container}", "error")
            return False
        if not wait_until_ready(container_id, max_attempts=60):
            return False
        for attempt in range(1, 6):
            publish = http_session.post(
                f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/media_publish",
                data={"creation_id": container_id, "access_token": PAGE_ACCESS_TOKEN},
                timeout=120
            ).json()
            if "id" in publish:
                add_log("✅ Instagram Video Story posted!")
                return True
            error_msg  = publish.get("error", {}).get("message", "")
            error_code = publish.get("error", {}).get("code", 0)
            if "not available" in error_msg.lower() or error_code in (9007, 2207026):
                time.sleep(30 * attempt)
            else:
                add_log(f"❌ IG Video Story publish failed: {publish}", "error")
                return False
        return False
    except Exception as e:
        add_log(f"❌ Instagram Video Story error: {e}", "error")
        return False

# ══════════════════════════════════════════════
# CORE POST FUNCTION
# ══════════════════════════════════════════════
def post_file(service, fid, fname, fmime):
    fpath = download_file(service, fid, fname)
    fb_ok = ig_ok = False
    failed = []

    if fmime.startswith("image/"):
        add_log("\U0001f5bc\ufe0f Image detected")
        drive_url = make_public_url(service, fid)

        fb_ok       = post_photo_facebook(fpath)
        ig_ok       = post_image_instagram(drive_url)
        fb_story_ok = post_photo_facebook_story(fpath)
        ig_story_ok = post_image_instagram_story(drive_url)

        if not fb_ok: failed.append("Facebook")
        if not ig_ok: failed.append("Instagram")
        if not fb_story_ok: failed.append("Facebook Story")
        if not ig_story_ok: failed.append("Instagram Story")

        if os.path.exists(fpath):
            os.remove(fpath)
        move_to_folder(service, fid, POSTED_FOLDER_ID, fname)
        mark_as_posted(fid)

        platforms = []
        if fb_ok: platforms.append("Facebook")
        if ig_ok: platforms.append("Instagram")
        if fb_story_ok: platforms.append("Facebook Story")
        if ig_story_ok: platforms.append("Instagram Story")
        save_post_log_entry(fname, platforms, failed, fb_ok or ig_ok or fb_story_ok or ig_story_ok)
        add_log(f"✅ Done: {fname} → {', '.join(platforms) if platforms else 'No platforms succeeded'}")

    elif fmime.startswith("video/"):
        add_log("\U0001f3a5 Video detected")
        local_video_url = save_media_for_serving(fpath, fname)
        add_log(f"   Serving video at: {local_video_url}")

        fb_ok = post_video_facebook(fpath)
        if not fb_ok: failed.append("Facebook")

        ig_ok = post_video_instagram(local_video_url)
        if not ig_ok: failed.append("Instagram")

        fb_story_ok = post_video_facebook_story(fpath)
        if not fb_story_ok: failed.append("Facebook Story")

        ig_story_ok = post_video_instagram_story(local_video_url)
        if not ig_story_ok: failed.append("Instagram Story")

        platforms_so_far = []
        if fb_ok: platforms_so_far.append("Facebook")
        if ig_ok: platforms_so_far.append("Instagram")
        if fb_story_ok: platforms_so_far.append("Facebook Story")
        if ig_story_ok: platforms_so_far.append("Instagram Story")
        save_post_log_entry(fname, platforms_so_far, failed, fb_ok or ig_ok or fb_story_ok or ig_story_ok)
        add_log(f"📝 FB+IG done: {fname} → {', '.join(platforms_so_far) if platforms_so_far else 'none'}")

        move_to_folder(service, fid, POSTED_FOLDER_ID, fname)
        mark_as_posted(fid)

        import shutil
        yt_fpath = fpath + ".yt_copy"
        try:
            shutil.copy2(fpath, yt_fpath)
        except Exception as e:
            add_log(f"\u26a0\ufe0f Could not copy file for YouTube: {e}", "warning")
            yt_fpath = None

        if os.path.exists(fpath):
            os.remove(fpath)
        cleanup_media(fname)

        if yt_fpath:
            def youtube_upload_task(yt_path, filename):
                add_log(f"\u25b6\ufe0f Starting YouTube background upload: {filename}")
                yt_ok = post_video_youtube(yt_path, filename)
                log = load_post_log()
                for entry in log:
                    if entry["filename"] == filename:
                        if yt_ok:
                            if "YouTube" not in entry.get("platforms", []):
                                entry.setdefault("platforms", []).append("YouTube")
                            entry["success"] = True
                            add_log(f"\u2705 YouTube uploaded: {filename}")
                        else:
                            if "YouTube" not in entry.get("failed_platforms", []):
                                entry.setdefault("failed_platforms", []).append("YouTube")
                            add_log(f"\u274c YouTube failed: {filename}", "error")
                        break
                with open(LOG_FILE, "w") as f:
                    import json as _json
                    _json.dump(log, f, indent=2)
                if os.path.exists(yt_path):
                    os.remove(yt_path)

            yt_thread = threading.Thread(target=youtube_upload_task, args=(yt_fpath, fname))
            yt_thread.daemon = True
            yt_thread.start()
            add_log(f"\U0001f504 YouTube upload running in background for: {fname}")
        else:
            add_log("\u26a0\ufe0f Skipping YouTube — file copy failed", "warning")

    else:
        add_log(f"\u26a0\ufe0f Unsupported MIME type: {fmime}", "warning")
        if os.path.exists(fpath):
            os.remove(fpath)


# ══════════════════════════════════════════════
# MAIN CHECK AND POST (auto-run, no inbox review)
# ══════════════════════════════════════════════
def check_and_post():
    if app_status["running"]:
        add_log("⚠️ Already running — skipping", "warning")
        return
    app_status["running"]        = True
    app_status["last_check"]     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    app_status["current_action"] = "Checking Drive folder..."
    app_status["total_checks"]  += 1
    try:
        service     = get_drive_service()
        posted_list = get_posted_files()
        ignored     = get_ignored_files()
        results     = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name, mimeType)"
        ).execute()
        drive_files = results.get("files", [])
        if not drive_files:
            add_log("📭 No new files found.")
        else:
            for drive_file in drive_files:
                fid   = drive_file["id"]
                fname = drive_file["name"]
                fmime = drive_file["mimeType"]
                if fid in posted_list or fid in ignored:
                    continue
                add_log(f"📂 New file: {fname}")
                app_status["current_action"] = f"Processing: {fname}"
                post_file(service, fid, fname, fmime)
    except Exception as e:
        add_log(f"❌ Error in check_and_post: {e}", "error")
    finally:
        app_status["running"]        = False
        app_status["current_action"] = "Idle — waiting for next check"

# ══════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════
IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

def run_scheduler():
    while True:
        try:
            scheduled = load_scheduled()
            now_utc   = datetime.now(UTC)
            due = []
            for s in scheduled:
                if s["status"] != "pending":
                    continue
                sched_dt_ist = datetime.strptime(s["scheduled_time"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                sched_dt_utc = sched_dt_ist.astimezone(UTC)
                if sched_dt_utc <= now_utc:
                    due.append(s)
            for item in due:
                add_log(f"⏰ Scheduled post due: {item['filename']}")
                try:
                    service   = get_drive_service()
                    file_info = service.files().get(fileId=item["file_id"], fields="mimeType,name").execute()
                    fmime     = file_info.get("mimeType", "")
                    fname     = file_info.get("name", item["filename"])
                    post_file(service, item["file_id"], fname, fmime)
                    scheduled = load_scheduled()
                    for s in scheduled:
                        if s["file_id"] == item["file_id"]:
                            s["status"] = "posted"
                    save_scheduled(scheduled)
                except Exception as e:
                    add_log(f"❌ Scheduled post failed: {e} — will retry", "error")
                    scheduled = load_scheduled()
                    for s in scheduled:
                        if s["file_id"] == item["file_id"]:
                            s["retries"] = s.get("retries", 0) + 1
                            if s["retries"] >= 5:
                                s["status"] = "failed"
                                add_log(f"❌ Max retries reached for {item['filename']}", "error")
                    save_scheduled(scheduled)
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(60)

# ══════════════════════════════════════════════
# INBOX API ROUTES
# ══════════════════════════════════════════════

@app.route("/api/inbox")
def get_inbox():
    """Return files from the main FOLDER_ID that haven't been scheduled or rejected yet."""
    try:
        service   = get_drive_service()
        results   = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name, mimeType, createdTime)"
        ).execute()
        files     = results.get("files", [])
        scheduled = load_scheduled()
        scheduled_ids = {s["file_id"] for s in scheduled}
        ignored   = get_ignored_files()
        files     = [f for f in files if f["id"] not in scheduled_ids and f["id"] not in ignored]
        return jsonify(files)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/preview/<file_id>")
def preview_file(file_id):
    """Stream a Drive file's actual bytes (image or video) so it can be
    previewed inline in the browser before approving/rejecting it."""
    try:
        service = get_drive_service()
        meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime = meta.get("mimeType", "application/octet-stream")

        request_obj = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request_obj)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        return Response(fh.read(), mimetype=mime)
    except Exception as e:
        add_log(f"⚠️ Preview failed for {file_id}: {e}", "warning")
        return jsonify({"error": str(e)}), 500


@app.route("/api/approve", methods=["POST"])
def approve_file():
    """
    Approve a file (site-level Google login already verified who's here):
    - If a scheduled_time is given: move FOLDER_ID → POSTED_FOLDER_ID immediately
      and add to the scheduled list (status=pending) for the scheduler to post later.
    - If NO scheduled_time is given: post it right now (Facebook, Instagram,
      Stories, YouTube) in the background instead of scheduling it.
    """
    data = request.json

    if not check_review_auth(data):
        add_log("🔒 Approve blocked", "warning")
        return jsonify({"error": "Not authorized"}), 401

    file_id        = data.get("file_id")
    filename       = data.get("filename")
    scheduled_time = (data.get("scheduled_time") or "").strip()
    if not file_id:
        return jsonify({"error": "file_id required"}), 400

    scheduled = load_scheduled()
    for s in scheduled:
        if s["file_id"] == file_id:
            return jsonify({"error": "Already scheduled"}), 400

    if not scheduled_time:
        try:
            service   = get_drive_service()
            file_info = service.files().get(fileId=file_id, fields="mimeType,name").execute()
            fmime     = file_info.get("mimeType", "")
            fname     = file_info.get("name", filename or "file")
        except Exception as e:
            return jsonify({"error": f"Failed to read file info: {str(e)}"}), 500

        def immediate_post_task(fid, fname, fmime):
            try:
                add_log(f"🚀 No schedule given — posting now: {fname}")
                svc = get_drive_service()
                post_file(svc, fid, fname, fmime)
            except Exception as e:
                add_log(f"❌ Immediate post failed: {e}", "error")

        t = threading.Thread(target=immediate_post_task, args=(file_id, fname, fmime))
        t.daemon = True
        t.start()
        return jsonify({"message": "No time selected — posting now!", "immediate": True})

    try:
        service = get_drive_service()
        move_to_folder(service, file_id, POSTED_FOLDER_ID, filename)
    except Exception as e:
        return jsonify({"error": f"Failed to move file: {str(e)}"}), 500

    scheduled.append({
        "file_id"        : file_id,
        "filename"       : filename,
        "scheduled_time" : scheduled_time,
        "status"         : "pending",
        "retries"        : 0,
        "created_at"     : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    save_scheduled(scheduled)
    add_log(f"📅 Approved & moved to posted folder: {filename} — scheduled at {scheduled_time} IST")
    return jsonify({"message": "Scheduled and moved to posted folder!"})


@app.route("/api/reject", methods=["POST"])
def reject_file():
    """
    Reject a file (site-level Google login already verified who's here):
    - Move it from FOLDER_ID → INBOX_FOLDER_ID
    - Mark as ignored so it doesn't reappear
    """
    data = request.json

    if not check_review_auth(data):
        add_log("🔒 Reject blocked", "warning")
        return jsonify({"error": "Not authorized"}), 401

    file_id  = data.get("file_id")
    filename = data.get("filename", "file")
    if not file_id:
        return jsonify({"error": "file_id required"}), 400
    try:
        service = get_drive_service()
        move_to_folder(service, file_id, INBOX_FOLDER_ID, filename)
        mark_as_ignored(file_id)
        scheduled = load_scheduled()
        scheduled = [s for s in scheduled if s["file_id"] != file_id]
        save_scheduled(scheduled)
        add_log(f"❌ Rejected & moved to inbox folder: {filename}")
        return jsonify({"message": "Moved to inbox folder!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cancel", methods=["POST"])
def cancel_scheduled():
    data      = request.json
    file_id   = data.get("file_id")
    scheduled = load_scheduled()
    scheduled = [s for s in scheduled if s["file_id"] != file_id]
    save_scheduled(scheduled)
    add_log(f"❌ Cancelled scheduled post")
    return jsonify({"message": "Cancelled!"})


@app.route("/api/scheduled")
def get_scheduled():
    return jsonify(load_scheduled())

# ══════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════
@app.route("/api/retry_youtube", methods=["POST"])
def retry_youtube():
    """Retry YouTube upload for a file that failed or was missed."""
    data     = request.json
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "filename required"}), 400

    try:
        service = get_drive_service()
        results = service.files().list(
            q=f"'{POSTED_FOLDER_ID}' in parents and name='{filename}' and trashed=false",
            fields="files(id, name, mimeType)"
        ).execute()
        files = results.get("files", [])
        if not files:
            return jsonify({"error": f"File not found in posted folder: {filename}"}), 404
        f = files[0]
        fid   = f["id"]
        fmime = f["mimeType"]
    except Exception as e:
        return jsonify({"error": f"Drive error: {str(e)}"}), 500

    def do_retry(fid, fname, fmime):
        try:
            service   = get_drive_service()
            fpath     = download_file(service, fid, fname)
            yt_fpath  = fpath + ".yt_retry"
            import shutil
            shutil.copy2(fpath, yt_fpath)
            if os.path.exists(fpath):
                os.remove(fpath)

            yt_ok = post_video_youtube(yt_fpath, fname)

            log = load_post_log()
            for entry in log:
                if entry["filename"] == fname:
                    if yt_ok:
                        if "YouTube" not in entry.get("platforms", []):
                            entry.setdefault("platforms", []).append("YouTube")
                        entry["failed_platforms"] = [p for p in entry.get("failed_platforms", []) if p != "YouTube"]
                        entry["success"] = True
                        add_log(f"✅ YouTube retry succeeded: {fname}")
                    else:
                        if "YouTube" not in entry.get("failed_platforms", []):
                            entry.setdefault("failed_platforms", []).append("YouTube")
                        add_log(f"❌ YouTube retry failed: {fname}", "error")
                    break
            with open(LOG_FILE, "w") as lf:
                json.dump(log, lf, indent=2)

            if os.path.exists(yt_fpath):
                os.remove(yt_fpath)
        except Exception as e:
            import traceback
            add_log(f"❌ YouTube retry error: {e}", "error")
            print(traceback.format_exc())

    t = threading.Thread(target=do_retry, args=(fid, filename, fmime))
    t.daemon = True
    t.start()
    add_log(f"🔄 YouTube retry started for: {filename}")
    return jsonify({"message": f"YouTube retry started for {filename}"})


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def get_status():
    return jsonify({
        "running"        : app_status["running"],
        "last_check"     : app_status["last_check"],
        "current_action" : app_status["current_action"],
        "total_checks"   : app_status["total_checks"],
        "youtube_auth"   : app_status["youtube_auth"],
        "logs"           : app_status["logs"][:30]
    })

@app.route("/api/post_log")
def get_post_log():
    return jsonify(load_post_log())

@app.route("/api/run_now")
def run_now():
    thread = threading.Thread(target=check_and_post)
    thread.daemon = True
    thread.start()
    return jsonify({"message": "Check started!"})

@app.route("/run")
def run_check():
    thread = threading.Thread(target=check_and_post)
    thread.daemon = True
    thread.start()
    return "OK", 200

# ── Startup ────────────────────────────────────
def check_youtube_auth():
    yt = get_youtube_service()
    if yt:
        add_log("✅ YouTube already authenticated")
    else:
        add_log("⚠️ YouTube not authenticated — visit /youtube/auth to connect")

check_youtube_auth()

scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.daemon = True
scheduler_thread.start()

if __name__ == "__main__":
    add_log("🚀 AutoPost Web App Started")
    app.run(debug=False, host="0.0.0.0", port=5000)
