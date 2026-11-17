#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公務活動報名系統 練習站（Vibe Coding 網頁資安實測教材用）

  本機執行：python app.py                →  http://localhost:8000
  線上部署：Render 等平台會設定 PORT 環境變數，程式會自動改綁 0.0.0.0:$PORT

每位訪客有自己專屬的一份測試資料與版本設定（存在記憶體，互不影響）：
  未修補版（紅色橫幅）會出現教材中的 9 個問題；
  已修補版（綠色橫幅）用來看「通過」的畫面；按橫幅上的按鈕切換。

只用 Python 內建模組，不需安裝任何套件。
所有帳號、姓名、電話、金鑰都是虛構的測試資料。
這個程式「故意」留有弱點，僅供課堂練習：請勿放入任何真實資料，課程結束後請關閉服務。
可用環境變數 CLASS_CODE 設定課程通行碼（未設定則不需通行碼）。
"""
import argparse
import copy
import html
import json
import os
import re
import secrets
import threading
import time
import traceback
from collections import OrderedDict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_FIXED = False                      # 新訪客預設的版本（--fixed 可改）
CLASS_CODE = os.environ.get("CLASS_CODE", "").strip()
MAX_SESSIONS = 3000                        # 超過就淘汰最久沒用的
SESSION_TTL = 6 * 3600                     # 6 小時沒用就清掉
MAX_BODY = 64 * 1024                       # 表單大小上限

USERS = {
    "a01": {"pw": "test1234", "role": "user", "name": "民眾 A", "signup": 1001},
    "b02": {"pw": "test1234", "role": "user", "name": "民眾 B", "signup": 1002},
    "admin": {"pw": "admin1234", "role": "admin", "name": "管理者", "signup": None},
}
SIGNUPS_INIT = {
    1001: {"owner": "a01", "name": "王小明（測試）", "phone": "0900-000-001",
           "email": "a01@example.test", "event": "AI 公務應用講座", "status": "待審核"},
    1002: {"owner": "b02", "name": "林小華（測試）", "phone": "0900-000-002",
           "email": "b02@example.test", "event": "AI 公務應用講座", "status": "待審核"},
}
SESSIONS = OrderedDict()   # sid -> 每位訪客自己的狀態


def new_state():
    return {"user": None, "ok": not CLASS_CODE, "fixed": DEFAULT_FIXED, "seen": time.time(),
            "signups": copy.deepcopy(SIGNUPS_INIT), "board": [], "chat": []}


FAKE_KEY = "sk-test-FAKE-0000-DEMO-KEY"   # 假金鑰，僅供練習四
FAKE_DB = "postgres://app_user:FAKE_PASSWORD@10.0.0.5:5432/signup"   # 假連線字串，僅供練習五
SYSTEM_PROMPT = ("你是「公務活動報名系統」的服務助理，可查詢全部報名資料並修改報名狀態。"
                 "內部備註：審核密語 FAKE-REVIEW-CODE（測試用假資料）。")


# ---------------------------------------------------------------- 畫面外框
def page(title, body, st, status=200):
    user, fixed = st["user"], st["fixed"]
    mode = "已修補版" if fixed else "未修補版"
    other = "未修補版" if fixed else "已修補版"
    color = "#1b7f3b" if fixed else "#b3261e"
    who = (f"{html.escape(user)}（{USERS[user]['name']}）｜<a href='/logout'>登出</a>"
           if user else "未登入｜<a href='/login'>登入</a>")
    admin_link = " ｜ <a href='/admin'>管理者清單</a>" if user and USERS[user]["role"] == "admin" else ""
    return status, f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>{html.escape(title)}｜公務活動報名系統（練習站）</title>
<style>
body{{font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;margin:0;color:#222;font-size:18px}}
.banner{{background:{color};color:#fff;padding:8px 24px;font-weight:bold;display:flex;justify-content:space-between;align-items:center}}
.banner button{{font-size:16px;padding:4px 12px;cursor:pointer}}
header{{display:flex;justify-content:space-between;padding:14px 24px;border-bottom:1px solid #ccc}}
nav{{padding:10px 24px;background:#f3f6fa;border-bottom:1px solid #ccc}}
main{{padding:20px 24px;max-width:980px}}
table{{border-collapse:collapse;margin:10px 0}} td,th{{border:1px solid #999;padding:6px 14px;text-align:left}}
th{{background:#e8eef6}} input,textarea,button{{font-size:18px;padding:6px 10px}}
textarea{{width:100%;box-sizing:border-box}} pre{{background:#f6f6f6;padding:12px;overflow:auto;font-size:15px}}
.msg{{border:1px solid #bbb;border-radius:6px;padding:8px 12px;margin:8px 0}}
.ai{{background:#eef5ff}} .me{{background:#f7f7f7}} .note{{color:#666;font-size:16px}}
</style><script src="/static/app.js"></script></head><body>
<div class="banner"><span>練習站｜全部為虛構測試資料｜目前：{mode}</span>
<form method="post" action="/mode" style="margin:0"><button>切換為{other}</button></form></div>
<header><b>公務活動報名系統</b><span>{who}</span></header>
<nav><a href="/">首頁</a> ｜ <a href="/my">我的報名</a> ｜ <a href="/query">報名查詢</a> ｜
<a href="/board">留言板</a> ｜ <a href="/assistant">AI 服務問答</a>{admin_link}</nav>
<main><h2>{html.escape(title)}</h2>{body}</main></body></html>"""


def signup_table(rows):
    out = "<table><tr><th>編號</th><th>姓名</th><th>電話</th><th>Email</th><th>活動</th><th>狀態</th></tr>"
    for sid, s in rows:
        out += (f"<tr><td>{sid}</td><td>{html.escape(s['name'])}</td><td>{s['phone']}</td>"
                f"<td>{s['email']}</td><td>{s['event']}</td><td>{s['status']}</td></tr>")
    return out + "</table>"


def forbidden(st, text):
    return page("403 無權限", f"<p>{text}</p>", st, 403)


# ---------------------------------------------------------------- AI 服務問答（規則式模擬）
def all_signups_text(st):
    return "\n".join(f"{sid}｜{s['name']}｜{s['phone']}｜{s['email']}｜{s['status']}"
                     for sid, s in st["signups"].items())


def assistant_reply(st, msg):
    """規則式模擬，不是真的語言模型；用來穩定重現四種常見問題。"""
    ask_sys = any(k in msg for k in ("系統提示", "內部文件", "system prompt"))
    ask_all = (any(k in msg for k in ("忽略", "系統管理員", "所有"))
               and any(k in msg for k in ("姓名", "電話", "Email", "email", "名單", "報名資料")))
    ask_approve = "核准" in msg and any(k in msg for k in ("1001", "1002", "帳號 A", "帳號A", "帳號 B", "帳號B"))
    user, FIXED, SIGNUPS = st["user"], st["fixed"], st["signups"]
    is_admin = bool(user) and USERS[user]["role"] == "admin"

    if ask_sys:
        if FIXED:
            return "無法提供系統設定或內部文件。若要查詢報名資料，我只能提供您本人的資料。"
        return f"【系統提示】{SYSTEM_PROMPT}\n【全部報名資料】\n{all_signups_text(st)}"
    if ask_approve:
        target = 1001 if any(k in msg for k in ("1001", "帳號 A", "帳號A")) else 1002
        if FIXED and not is_admin:
            return "我沒有修改報名狀態的權限。這項動作需要管理者登入並確認後才能執行。"
        SIGNUPS[target]["status"] = "已核准"
        return f"已完成：報名編號 {target} 的狀態已改為「已核准」。"
    if ask_all:
        if FIXED and not is_admin:
            if user and USERS[user]["signup"]:
                sid = USERS[user]["signup"]
                s = SIGNUPS[sid]
                return f"我只能提供您本人的報名資料：{sid}｜{s['name']}｜{s['status']}"
            return "請先登入。我只能提供您本人的報名資料。"
        return f"好的，以下是所有報名者資料：\n{all_signups_text(st)}"
    return "您好，「AI 公務應用講座」於活動當日 09:30 開放報到（測試資料）。還有其他問題嗎？"


def summarize_doc(st, doc):
    first = re.split(r"[。\n]", doc.strip())[0][:60]
    hidden = re.search(r"改說(.+?)[。）)\n]", doc + "\n")
    if hidden and "忽略" in doc:
        if st["fixed"]:
            return (f"摘要：{first}。\n（注意：文件中有一段像是給 AI 的指示，"
                    "已當作一般文字處理，沒有執行。）")
        return hidden.group(1).strip() + "。"
    return f"摘要：{first}。"


# ---------------------------------------------------------------- 路由
LOCK = threading.Lock()


class TooLarge(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    timeout = 20
    server_version = "PracticeSite"
    sys_version = ""

    def log_message(self, fmt, *args):
        print("  %s %s" % (self.command, self.path), flush=True)

    # --- 每位訪客一份狀態
    def state(self, create=True):
        self.new_sid = None
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            sid = c["sid"].value if "sid" in c else None
        except Exception:
            sid = None
        now = time.time()
        with LOCK:
            st = SESSIONS.get(sid) if sid else None
            if st is not None and now - st["seen"] > SESSION_TTL:
                SESSIONS.pop(sid, None)
                st = None
            if st is None:
                if not create:      # 沒帶 cookie 直接抓檔案：給一份不保存的預設狀態
                    return new_state()
                sid = secrets.token_hex(16)
                st = new_state()
                SESSIONS[sid] = st
                self.new_sid = sid
                while len(SESSIONS) > MAX_SESSIONS:
                    SESSIONS.popitem(last=False)
            else:
                SESSIONS.move_to_end(sid)
            st["seen"] = now
            return st

    def send(self, status, body, ctype="text/html; charset=utf-8", location=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        if location:
            self.send_header("Location", location)
        if getattr(self, "new_sid", None):
            self.send_header("Set-Cookie", f"sid={self.new_sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, to):
        self.send(303, "", location=to)

    def form(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_BODY:
            raise TooLarge()
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def do_HEAD(self):
        self.new_sid = None
        self.send(200, "")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        path = urlparse(self.path).path
        if path == "/healthz":
            self.new_sid = None
            return self.send(200, "ok", "text/plain; charset=utf-8")
        if path == "/robots.txt":
            self.new_sid = None
            return self.send(200, "User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
        st = self.state(create=(path != "/static/app.js"))
        try:
            if CLASS_CODE and not st["ok"] and path != "/static/app.js":
                return self.gate(method, st, path)
            self.route(method, st, path)
        except TooLarge:
            self.send(*page("413 內容過長", "<p>送出的內容太長了。</p>", st, 413))
        except Exception:
            if st["fixed"]:   # 統一錯誤處理：使用者只看到簡短訊息，細節只留在伺服器端
                print(traceback.format_exc(), flush=True)
                self.send(*page("資料格式不正確", "<p>請確認輸入內容後再試一次。（錯誤代碼 E400）</p>", st, 400))
            else:             # 弱點（練習五）：把錯誤堆疊與設定直接印給使用者
                detail = traceback.format_exc() + f"\n[DEBUG] DB_CONN = {FAKE_DB}\n[DEBUG] DEBUG_MODE = True"
                self.send(*page("500 Internal Server Error", f"<pre>{html.escape(detail)}</pre>", st, 500))

    def gate(self, method, st, path):
        err = ""
        if path == "/enter" and method == "POST":
            if secrets.compare_digest(self.form().get("code", "").strip(), CLASS_CODE):
                st["ok"] = True
                return self.redirect("/")
            err = "<p style='color:#b3261e'>通行碼不正確</p>"
        body = (err + "<form method='post' action='/enter'><p>課程通行碼 <input name='code' autofocus></p>"
                "<button>進入練習站</button></form>")
        return self.send(*page("請輸入課程通行碼", body, st))

    def route(self, method, st, path):
        user, fixed = st["user"], st["fixed"]
        signups, board = st["signups"], st["board"]
        q = parse_qs(urlparse(self.path).query)

        if path == "/":
            rows = "".join(f"<tr><td>{k}</td><td>{v['pw']}</td><td>{v['name']}</td><td>{v['signup'] or '—'}</td></tr>"
                           for k, v in USERS.items())
            body = ("<p>這是教學用的練習站，請依教材步驟操作。您看到的是自己專屬的一份測試資料，不會影響其他人。</p>"
                    f"<h3>測試帳號</h3><table><tr><th>帳號</th><th>密碼</th><th>身分</th><th>報名編號</th></tr>{rows}</table>"
                    "<form method='post' action='/reset'><button>重設測試資料</button></form>")
            return self.send(*page("首頁", body, st))

        if path == "/mode" and method == "POST":
            st["fixed"] = not st["fixed"]
            back = urlparse(self.headers.get("Referer", "")).path or "/"
            if not back.startswith("/") or back.startswith("//") or back in ("/mode", "/enter"):
                back = "/"
            return self.redirect(back)

        if path == "/reset" and method == "POST":
            st["signups"] = copy.deepcopy(SIGNUPS_INIT)
            st["board"] = []
            st["chat"] = []
            return self.redirect("/")

        if path == "/login":
            err = ""
            if method == "POST":
                f = self.form()
                acc = f.get("account", "").strip()
                if acc in USERS and USERS[acc]["pw"] == f.get("password", ""):
                    st["user"] = acc
                    return self.redirect("/admin" if USERS[acc]["role"] == "admin" else "/my")
                err = "<p style='color:#b3261e'>帳號或密碼錯誤</p>"
            body = (err + "<form method='post'><p>帳號 <input name='account'></p>"
                    "<p>密碼 <input name='password' type='password'></p><button>登入</button></form>")
            return self.send(*page("登入", body, st))

        if path == "/logout":
            st["user"] = None
            return self.redirect("/")

        if path == "/my":
            if not user:
                return self.redirect("/login")
            if USERS[user]["role"] == "admin":
                return self.redirect("/admin")
            return self.redirect(f"/signup/{USERS[user]['signup']}")

        if path == "/query":
            if "id" not in q:
                body = ("<form><p>報名編號 <input name='id'> <button>查詢</button></p></form>"
                        "<p class='note'>例：1001</p>")
                return self.send(*page("報名查詢", body, st))
            raw = q["id"][0]
            if fixed and not (raw.isdigit() and len(raw) <= 6):   # 後端驗證格式與長度
                return self.send(*page("資料格式不正確", "<p>報名編號須為 6 碼以內的數字。（錯誤代碼 E400）</p>", st, 400))
            if len(raw) > 50:
                raise ValueError("value too long for type character varying(50)")
            return self.redirect(f"/signup/{int(raw)}")           # 弱點：未驗證就直接轉型

        m = re.fullmatch(r"/signup/(\d{1,9})", path)
        if m:
            if not user:
                return self.redirect("/login")
            sid = int(m.group(1))
            if sid not in signups:
                return self.send(*page("404 查無資料", "<p>查無此報名編號。</p>", st, 404))
            s = signups[sid]
            # 弱點（練習二）：未修補版沒有比對「資料擁有者」與「目前登入者」
            if fixed and s["owner"] != user and USERS[user]["role"] != "admin":
                return self.send(*forbidden(st, "無權限檢視此筆資料。"))
            return self.send(*page(f"報名資料 {sid}", signup_table([(sid, s)]), st))

        if path == "/admin":
            # 弱點（練習一）：未修補版只把選單連結藏起來，後端沒有檢查身分
            if fixed and not (user and USERS[user]["role"] == "admin"):
                return self.send(*forbidden(st, "此頁僅限管理者，請先以管理者帳號登入。"))
            return self.send(*page("管理者清單", signup_table(signups.items()), st))

        if path == "/api/signups":
            if fixed and not (user and USERS[user]["role"] == "admin"):
                return self.send(403, json.dumps({"error": "forbidden"}), "application/json; charset=utf-8")
            return self.send(200, json.dumps(signups, ensure_ascii=False, indent=2),
                             "application/json; charset=utf-8")

        if path == "/board":
            if method == "POST":
                f = self.form()
                if f.get("text", "").strip() and len(board) < 50:
                    board.append(((f.get("nick", "") or "匿名")[:30], f["text"][:2000]))
                return self.redirect("/board")
            items = ""
            for nick, text in board:
                # 弱點（練習三）：未修補版把留言原樣插入 HTML
                shown = html.escape(text) if fixed else text
                items += f"<div class='msg'><b>{html.escape(nick)}</b>：{shown}</div>"
            body = (items or "<p class='note'>目前沒有留言。</p>") + (
                "<form method='post'><p>暱稱 <input name='nick'></p>"
                "<p><textarea name='text' rows='3' placeholder='請輸入留言'></textarea></p>"
                "<button>送出留言</button></form>"
                "<form method='post' action='/board/clear' style='margin-top:10px'><button>清空留言</button></form>")
            return self.send(*page("留言板", body, st))

        if path == "/board/clear" and method == "POST":
            board.clear()
            return self.redirect("/board")

        if path == "/static/app.js":
            if fixed:
                js = "// 金鑰已移到後端環境變數，前端只呼叫自己的後端 /api/weather\nconsole.log('app.js loaded');\n"
            else:   # 弱點（練習四）：把金鑰寫在前端
                js = (f'// 活動當日天氣小工具\nconst API_KEY = "{FAKE_KEY}";  // TODO: 上線前要換掉\n'
                      'const WEATHER_API = "https://api.example.test/v1/weather?token=" + API_KEY;\n'
                      "console.log('app.js loaded');\n")
            return self.send(200, js, "application/javascript; charset=utf-8")

        if path == "/assistant":
            chat = st["chat"]
            if method == "POST":
                f = self.form()
                del chat[:-38]
                if f.get("doc", "").strip():
                    doc = f["doc"][:4000]
                    chat.append(("我", "【請摘要這份文件】\n" + doc))
                    chat.append(("AI", summarize_doc(st, doc)))
                elif f.get("msg", "").strip():
                    msg = f["msg"][:4000]
                    chat.append(("我", msg))
                    chat.append(("AI", assistant_reply(st, msg)))
                return self.redirect("/assistant")
            log = "".join(
                f"<div class='msg {'ai' if who == 'AI' else 'me'}'><b>{who}</b>：{html.escape(text).replace(chr(10), '<br>')}</div>"
                for who, text in chat)
            body = ("<p class='note'>本頁是「規則式模擬」，不是真的語言模型，用來穩定重現教材中的四種情況。</p>"
                    + log +
                    "<form method='post'><p><textarea name='msg' rows='3' placeholder='請輸入問題'></textarea></p>"
                    "<button>送出</button></form>"
                    "<h3>文件摘要</h3><form method='post'>"
                    "<p><textarea name='doc' rows='4' placeholder='把文件內容貼在這裡'></textarea></p>"
                    "<button>請 AI 摘要</button></form>"
                    "<form method='post' action='/assistant/clear' style='margin-top:10px'><button>清空對話</button></form>")
            return self.send(*page("AI 服務問答（模擬）", body, st))

        if path == "/assistant/clear" and method == "POST":
            st["chat"] = []
            return self.redirect("/assistant")

        return self.send(*page("404 找不到頁面", "<p>找不到這個網址。</p>", st, 404))


def main():
    global DEFAULT_FIXED
    ap = argparse.ArgumentParser(description="公務活動報名系統 練習站")
    ap.add_argument("--fixed", action="store_true", help="新訪客預設為已修補版（預設為未修補版）")
    ap.add_argument("--port", type=int, default=8000, help="本機執行用的埠號（線上平台以 PORT 環境變數為準）")
    ap.add_argument("--host", default=None, help="綁定位址；未指定時，本機為 127.0.0.1，有 PORT 環境變數時為 0.0.0.0")
    args = ap.parse_args()
    DEFAULT_FIXED = args.fixed
    hosted = bool(os.environ.get("PORT"))
    port = int(os.environ.get("PORT") or args.port)
    host = args.host or ("0.0.0.0" if hosted else "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    where = f"0.0.0.0:{port}（線上模式）" if hosted else f"http://localhost:{port}"
    print(f"練習站已啟動：{where}｜通行碼：{'已設定' if CLASS_CODE else '未設定'}", flush=True)
    print("停止：按 Ctrl + C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
