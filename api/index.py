import os, json, base64, time, httpx
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
GH = os.environ["GH_TOKEN"]
OWNER = os.environ["GH_OWNER"]
ALLOWED = int(os.environ["ALLOWED_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
STATE_REPO = "bot-state"
MAX_BYTES = 10 * 1024 * 1024

TG = httpx.Client(base_url=f"https://api.telegram.org/bot{BOT_TOKEN}", timeout=25)
GHC = httpx.Client(
    base_url="https://api.github.com",
    headers={"Authorization": f"Bearer {GH}", "Accept": "application/vnd.github+json"},
    timeout=25,
)

MENU = {"inline_keyboard": [
    [{"text": "📁 Repos", "callback_data": "repos"},
     {"text": "⬆️ Upload", "callback_data": "upload"}],
    [{"text": "📊 Status", "callback_data": "status"},
     {"text": "⚙️ Actions", "callback_data": "actions"}],
    [{"text": "📱 APK", "callback_data": "apk"},
     {"text": "🌐 Dashboard", "url": f"https://github.com/{OWNER}?tab=repositories"}],
    [{"text": "❓ Help", "callback_data": "help"}],
]}

HELP = (
    "Perintah:\n"
    "/repos - daftar repo (tap untuk pilih)\n"
    "/use nama - pilih repo\n"
    "/newrepo nama - buat repo private baru\n"
    "/branch nama - buat branch baru & pindah\n"
    "(kirim file sebagai Dokumen) - upload = langsung commit\n"
    "   caption = lokasi file, contoh: src/app.py\n"
    "/pr judul - buat pull request\n"
    "/merge nomor - merge pull request\n"
    "/status - repo, branch, commit terakhir"
)


# ---------- helper ----------

def tg(method, **data):
    j = TG.post("/" + method, json=data).json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram: {j.get('description')}")
    return j["result"]


def gh(method, path, ok404=False, **kw):
    r = GHC.request(method, path, **kw)
    if ok404 and r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def send(chat, text, kb=MENU):
    data = {"chat_id": chat, "text": text[:4000],
            "link_preview_options": {"is_disabled": True}}
    if kb:
        data["reply_markup"] = kb
    tg("sendMessage", **data)


# ---------- state (file kecil di repo bot-state, tanpa database) ----------

_st = {"data": None, "sha": None}


def state():
    if _st["data"] is None:
        f = gh("GET", f"/repos/{OWNER}/{STATE_REPO}/contents/state.json", ok404=True)
        if f:
            _st["data"] = json.loads(base64.b64decode(f["content"]))
            _st["sha"] = f["sha"]
        else:
            _st["data"], _st["sha"] = {}, None
    return _st["data"]


def set_state(**kw):
    s = state()
    s.update(kw)
    body = {"message": "state",
            "content": base64.b64encode(json.dumps(s).encode()).decode()}
    if _st["sha"]:
        body["sha"] = _st["sha"]
    path = f"/repos/{OWNER}/{STATE_REPO}/contents/state.json"
    r = GHC.put(path, json=body)
    if r.status_code == 404:  # repo bot-state belum ada, buat dulu
        gh("POST", "/user/repos", json={"name": STATE_REPO, "private": True, "auto_init": True})
        for _ in range(3):
            time.sleep(2)
            r = GHC.put(path, json=body)
            if r.status_code != 404:
                break
    if r.status_code >= 400:
        raise RuntimeError(f"Gagal simpan state: {r.status_code} {r.text[:200]}")
    _st["sha"] = r.json()["content"]["sha"]


def need_repo():
    s = state()
    if not s.get("repo"):
        raise RuntimeError("Belum pilih repo. Tap /repos lalu pilih, atau pakai /use nama")
    return s["repo"], s.get("branch") or "main"


# ---------- perintah ----------

def h_start(chat, arg):
    me = gh("GET", "/user")
    cap = (
        f"👤 @{me['login']}\n\n"
        f"📦 Repo publik: {me['public_repos']}\n"
        f"👥 Followers: {me['followers']}\n"
        f"➡️ Following: {me['following']}\n"
        f"📅 Bergabung: {me['created_at'][:10]}\n"
        f"🔗 {me['html_url']}"
    )
    tg("sendPhoto", chat_id=chat, photo=me["avatar_url"], caption=cap, reply_markup=MENU)


def h_repos(chat, arg):
    data = gh("GET", "/user/repos?per_page=30&sort=updated&affiliation=owner")
    rows, row = [], []
    for r in data:
        if len(r["name"]) > 58:
            continue
        row.append({"text": r["name"][:30], "callback_data": "use:" + r["name"]})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    send(chat, "📁 Tap repo untuk memilih:", {"inline_keyboard": rows} if rows else MENU)


def h_use(chat, arg):
    if not arg:
        raise RuntimeError("Pakai: /use nama-repo")
    info = gh("GET", f"/repos/{OWNER}/{arg}", ok404=True)
    if not info:
        raise RuntimeError(f"Repo {arg} tidak ditemukan")
    set_state(repo=arg, branch=info["default_branch"])
    return f"✅ Repo aktif: {arg}\n🌿 Branch: {info['default_branch']}"


def h_newrepo(chat, arg):
    if not arg:
        raise RuntimeError("Pakai: /newrepo nama-repo")
    r = gh("POST", "/user/repos", json={"name": arg, "private": True, "auto_init": True})
    set_state(repo=r["name"], branch=r["default_branch"])
    return f"✅ Repo {r['name']} dibuat (private) dan dipilih\n{r['html_url']}"


def h_branch(chat, arg):
    if not arg:
        raise RuntimeError("Pakai: /branch nama-branch")
    repo, cur = need_repo()
    sha = gh("GET", f"/repos/{OWNER}/{repo}/git/ref/heads/{quote(cur, safe='/')}")["object"]["sha"]
    gh("POST", f"/repos/{OWNER}/{repo}/git/refs", json={"ref": f"refs/heads/{arg}", "sha": sha})
    set_state(branch=arg)
    return f"🌿 Branch {arg} dibuat dari {cur} dan dipilih"


def h_upload_help(chat, arg):
    s = state()
    return (f"⬆️ Repo: {s.get('repo', '(belum dipilih)')} | Branch: {s.get('branch', '-')}\n"
            "Kirim file sebagai Dokumen (bukan foto), satu per satu.\n"
            "Caption = lokasi file di repo, contoh: src/app.py\n"
            "Caption kosong = nama file di root repo.")


def h_upload(chat, msg):
    repo, br = need_repo()
    if msg.get("document"):
        d = msg["document"]
        fid, size, name = d["file_id"], d.get("file_size") or 0, d.get("file_name") or "file.bin"
    else:
        p = msg["photo"][-1]
        fid, size, name = p["file_id"], p.get("file_size") or 0, f"foto_{p['file_unique_id']}.jpg"
    if size > MAX_BYTES:
        raise RuntimeError("File terlalu besar (maks 10 MB)")
    path = (msg.get("caption") or name).strip().lstrip("/")
    info = tg("getFile", file_id=fid)
    r = httpx.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}", timeout=25)
    r.raise_for_status()
    qp = quote(path, safe="/")
    old = gh("GET", f"/repos/{OWNER}/{repo}/contents/{qp}?ref={quote(br, safe='')}", ok404=True)
    body = {"message": f"Upload {path}",
            "content": base64.b64encode(r.content).decode(),
            "branch": br}
    if isinstance(old, dict) and old.get("sha"):
        body["sha"] = old["sha"]
    res = gh("PUT", f"/repos/{OWNER}/{repo}/contents/{qp}", json=body)
    return f"✅ {path} masuk ke {repo} ({br})\n{res['commit']['html_url']}"


def h_status(chat, arg):
    repo, br = need_repo()
    cs = gh("GET", f"/repos/{OWNER}/{repo}/commits?sha={quote(br, safe='')}&per_page=3")
    lines = [f"• {c['sha'][:7]} {c['commit']['message'].split(chr(10))[0][:50]}" for c in cs]
    return f"📦 Repo: {repo}\n🌿 Branch: {br}\n\nCommit terakhir:\n" + "\n".join(lines)


def h_pr(chat, arg):
    repo, br = need_repo()
    base = gh("GET", f"/repos/{OWNER}/{repo}")["default_branch"]
    if br == base:
        raise RuntimeError(f"Kamu masih di branch {base}. Buat branch dulu: /branch nama")
    p = gh("POST", f"/repos/{OWNER}/{repo}/pulls",
           json={"title": arg or br, "head": br, "base": base})
    cb = f"merge:{p['number']}@{repo}"
    if len(cb.encode()) > 64:
        cb = f"merge:{p['number']}"
    kb = {"inline_keyboard": [[{"text": f"✅ Merge #{p['number']}", "callback_data": cb}]]}
    send(chat, f"🔀 PR #{p['number']}: {p['html_url']}", kb)


def h_merge(chat, arg):
    num, _, repo = arg.lstrip("#").partition("@")
    num = num.strip()
    if not num.isdigit():
        raise RuntimeError("Pakai: /merge nomor-pr")
    if not repo:
        repo, _b = need_repo()
    gh("PUT", f"/repos/{OWNER}/{repo}/pulls/{num}/merge", json={})
    base = gh("GET", f"/repos/{OWNER}/{repo}")["default_branch"]
    if state().get("repo") == repo:
        set_state(branch=base)
    return f"✅ PR #{num} di-merge. Branch aktif kembali ke {base}"


def h_actions(chat, arg):
    repo, _b = need_repo()
    runs = gh("GET", f"/repos/{OWNER}/{repo}/actions/runs?per_page=5").get("workflow_runs", [])
    lines = []
    for r in runs:
        icon = "⏳" if r["status"] != "completed" else ("✅" if r["conclusion"] == "success" else "❌")
        lines.append(f"{icon} {r['name']} - {r['head_branch']} ({r['created_at'][:10]})")
    return "\n".join(lines) or "Belum ada workflow run."


def h_apk(chat, arg):
    repo, _b = need_repo()
    rel = gh("GET", f"/repos/{OWNER}/{repo}/releases/latest", ok404=True)
    if not rel:
        return "Belum ada release di repo ini."
    apks = [a for a in rel.get("assets", []) if a["name"].endswith(".apk")]
    if not apks:
        return f"Release {rel['tag_name']} tidak punya file APK."
    return "\n\n".join(f"📱 {a['name']}\n{a['browser_download_url']}" for a in apks)


def h_help(chat, arg):
    return HELP


ROUTES = {
    "start": h_start, "repos": h_repos, "use": h_use, "newrepo": h_newrepo,
    "branch": h_branch, "pr": h_pr, "merge": h_merge, "status": h_status,
    "actions": h_actions, "apk": h_apk, "help": h_help, "upload": h_upload_help,
}


def run(chat, fn):
    try:
        out = fn()
        if out:
            send(chat, out)
    except Exception as e:
        print("ERR", repr(e))
        try:
            send(chat, f"❌ {str(e)[:3500]}")
        except Exception:
            pass


def route(chat, cmd, arg):
    fn = ROUTES.get(cmd)
    if not fn:
        send(chat, "Perintah tidak dikenal. Ketik /help")
        return
    run(chat, lambda: fn(chat, arg))


def handle(upd):
    _st["data"], _st["sha"] = None, None
    cb = upd.get("callback_query")
    if cb:
        try:
            tg("answerCallbackQuery", callback_query_id=cb["id"])
        except Exception:
            pass
        if cb["from"]["id"] != ALLOWED:
            return
        chat = cb["message"]["chat"]["id"]
        cmd, _, arg = (cb.get("data") or "").partition(":")
        route(chat, cmd, arg)
        return
    msg = upd.get("message")
    if not msg:
        return
    chat, uid = msg["chat"]["id"], msg["from"]["id"]
    if uid != ALLOWED:
        send(chat, f"⛔ Akses ditolak. ID kamu: {uid}", None)
        return
    if msg.get("document") or msg.get("photo"):
        run(chat, lambda: h_upload(chat, msg))
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        route(chat, cmd[1:].split("@")[0].lower(), arg.strip())


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
                self.send_response(403)
                self.end_headers()
                return
            n = int(self.headers.get("content-length", 0))
            handle(json.loads(self.rfile.read(n) or b"{}"))
        except Exception as e:
            print("ERR", repr(e))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write("bot aktif".encode())
