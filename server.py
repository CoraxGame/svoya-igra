#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
СВОЯ ИГРА - локальный сервер.

Раздаёт игру (index.html) и передаёт нажатия кнопок с телефонов на компьютер
ведущего. Работает только на стандартной библиотеке Python, ничего ставить не нужно.
Интернет не нужен: достаточно одной Wi-Fi сети (или мобильного хот-спота компьютера).
"""
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

PORT = int(os.environ.get("PORT", "8000"))
CLOUD = bool(os.environ.get("RENDER") or os.environ.get("CLOUD"))  # запуск на облачном хостинге
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "server_data")
DB_FILE = os.path.join(DATA_DIR, "db.json")
MAX_BODY = 8 * 1024 * 1024  # 8 МБ хватает для списка аватаров
MEDIA_DIR = os.path.join(DATA_DIR, "media")
MAX_MEDIA = 300 * 1024 * 1024  # один файл не больше 300 МБ
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip() or None  # публичная ссылка (туннель)

cond = threading.Condition()
peers = {}      # id -> {"presence": {...}, "updatedAt": ms, "n": число открытых соединений}
peers_ver = 0
db = {}         # путь документа -> данные (список аватаров и т.п.)
db_ver = 1


def now_ms():
    return int(time.time() * 1000)


def load_db():
    global db
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        db = {}


def save_db():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
        os.replace(tmp, DB_FILE)
    except Exception as e:
        print("Не удалось сохранить данные:", e)


def lan_addresses():
    """Все адреса этого компьютера в локальной сети (без 127.0.0.1)."""
    found = []

    def add(ip):
        if ip and not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in found:
            found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # пакеты не отправляются, нужно только узнать адрес
        add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except Exception:
        pass
    return found


def peers_snapshot():
    return [{"id": k, "presence": v["presence"], "updatedAt": v["updatedAt"]} for k, v in peers.items()]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SvoyaIgra/1.0"

    def log_message(self, fmt, *args):  # тихий режим
        pass

    # ---------- вспомогательное ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    # ---------- медиа ----------
    def _media_file(self, mid):
        if not ID_RE.match(mid or ""):
            return None
        return os.path.join(MEDIA_DIR, mid)

    def _upload_media(self, query):
        qs = urllib.parse.parse_qs(query)
        mid = (qs.get("id") or [""])[0]
        mtype = (qs.get("type") or [""])[0][:100]
        fp = self._media_file(mid)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if not fp or n <= 0 or n > MAX_MEDIA:
            return self._json({"error": "bad request"}, 400)
        os.makedirs(MEDIA_DIR, exist_ok=True)
        tmp = fp + ".part"
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left > 0:
                os.remove(tmp)
                return self._json({"error": "incomplete"}, 400)
            os.replace(tmp, fp)
            with open(fp + ".type", "w", encoding="utf-8") as f:
                f.write(mtype)
        except OSError as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "size": n})

    def _serve_media(self, mid):
        fp = self._media_file(mid)
        if not fp or not os.path.isfile(fp):
            self.send_response(404)
            self.end_headers()
            return
        try:
            with open(fp + ".type", "r", encoding="utf-8") as f:
                ctype = f.read().strip() or "application/octet-stream"
        except OSError:
            ctype = "application/octet-stream"
        size = os.path.getsize(fp)
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:  # последние N байт
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # ---------- GET ----------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/info":
            urls = [] if CLOUD else ["http://%s:%d" % (ip, PORT) for ip in lan_addresses()]
            return self._json({"urls": urls, "public": PUBLIC_URL})
        if path == "/api/state":
            qs = urllib.parse.parse_qs(url.query)
            try:
                cpv = int((qs.get("pv") or ["-1"])[0])
                cdv = int((qs.get("dv") or ["-1"])[0])
            except ValueError:
                cpv = cdv = -1
            with cond:
                out = {"pv": peers_ver, "dv": db_ver}
                if cpv != peers_ver:
                    out["peers"] = peers_snapshot()
                if cdv != db_ver:
                    out["db"] = db
                return self._json(out)
        if path == "/api/events":
            qs = urllib.parse.parse_qs(url.query)
            cid = (qs.get("id") or [""])[0][:64]
            if not cid:
                return self._json({"error": "no id"}, 400)
            return self._events(cid)
        if path == "/api/media/has":
            qs = urllib.parse.parse_qs(url.query)
            mid = (qs.get("id") or [""])[0]
            fp = self._media_file(mid)
            return self._json({"has": bool(fp and os.path.exists(fp)), "size": os.path.getsize(fp) if fp and os.path.exists(fp) else 0})
        if path.startswith("/media/"):
            return self._serve_media(path[len("/media/"):])
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def _serve_index(self):
        try:
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(500)
            self.end_headers()
            self.wfile.write("index.html not found".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events(self, cid):
        global peers_ver
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with cond:
            p = peers.get(cid)
            if p is None:
                p = {"presence": {}, "updatedAt": now_ms(), "n": 0}
                peers[cid] = p
            p["n"] += 1
            peers_ver += 1
            cond.notify_all()
        seen_p, seen_d = -1, -1
        try:
            while True:
                with cond:
                    if seen_p == peers_ver and seen_d == db_ver:
                        cond.wait(timeout=5)
                    pv, dv = peers_ver, db_ver
                    snap = peers_snapshot() if pv != seen_p else None
                    dbs = json.dumps(db, ensure_ascii=False) if dv != seen_d else None
                out = b""
                if snap is not None:
                    out += ("event: peers\nid: %d\ndata: %s\n\n" % (pv, json.dumps(snap, ensure_ascii=False))).encode("utf-8")
                if dbs is not None:
                    out += ("event: db\nid: %d\ndata: %s\n\n" % (dv, dbs)).encode("utf-8")
                if not out:
                    out = b": ping\n\n"
                self.wfile.write(out)
                self.wfile.flush()
                seen_p, seen_d = pv, dv
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with cond:
                p = peers.get(cid)
                if p is not None:
                    p["n"] -= 1
                    if p["n"] <= 0:
                        del peers[cid]
                peers_ver += 1
                cond.notify_all()

    # ---------- POST ----------
    def do_POST(self):
        global peers_ver, db_ver
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/media":
            return self._upload_media(parsed.query)
        body = self._read_json()
        if not isinstance(body, dict):
            return self._json({"error": "bad request"}, 400)
        if path == "/api/presence":
            cid = str(body.get("id", ""))[:64]
            patch = body.get("patch")
            if not cid or not isinstance(patch, dict):
                return self._json({"error": "bad request"}, 400)
            with cond:
                p = peers.get(cid)
                if p is None:
                    p = {"presence": {}, "updatedAt": now_ms(), "n": 0}
                    peers[cid] = p
                before = json.dumps(p["presence"], sort_keys=True)
                for k, v in patch.items():
                    if v is None:
                        p["presence"].pop(k, None)
                    else:
                        p["presence"][k] = v
                if json.dumps(p["presence"], sort_keys=True) != before:
                    p["updatedAt"] = now_ms()
                    peers_ver += 1
                    cond.notify_all()
            return self._json({"ok": True})
        if path == "/api/leave":
            cid = str(body.get("id", ""))[:64]
            with cond:
                if cid in peers:
                    peers[cid]["presence"] = {}
                    peers_ver += 1
                    cond.notify_all()
            return self._json({"ok": True})
        if path == "/api/db":
            doc_path = str(body.get("path", ""))[:200]
            if not doc_path:
                return self._json({"error": "bad request"}, 400)
            with cond:
                db[doc_path] = body.get("data")
                db_ver += 1
                save_db()
                cond.notify_all()
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)


def start_tunnel():
    """Если рядом лежит cloudflared, запускает бесплатный туннель и ловит публичную ссылку."""
    global PUBLIC_URL
    if os.environ.get("TUNNEL") == "0" or PUBLIC_URL:
        return None
    exe = None
    for name in ("cloudflared.exe", "cloudflared"):
        cand = os.path.join(ROOT, name)
        if os.path.isfile(cand):
            exe = cand
            break
    if exe is None:
        exe = shutil.which("cloudflared")
    if exe is None:
        return None
    try:
        proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", "http://localhost:%d" % PORT],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        print("Не удалось запустить cloudflared:", e)
        return None

    def reader():
        global PUBLIC_URL
        url = None
        for line in proc.stderr:
            m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line)
            if m and not url:
                url = m.group(0)
                print("  Туннель создан, жду соединения (до 30 секунд)...")
            if url and not PUBLIC_URL and "Registered tunnel connection" in line:
                PUBLIC_URL = url
                print()
                print("  >>> ПУБЛИЧНАЯ ССЫЛКА ДЛЯ ОНЛАЙН-ИГРОКОВ (туннель подключён):")
                print("      " + PUBLIC_URL)
                print()
            elif (" ERR " in line or "error" in line.lower()) and not PUBLIC_URL:
                print("  [cloudflared] " + line.strip()[:200])
        if not PUBLIC_URL:
            print("  Туннель остановился и не подключился. Проверьте интернет, VPN и антивирус.")
    threading.Thread(target=reader, daemon=True).start()
    return proc


SSH_PROVIDERS = [
    ("localhost.run", ["-R", "80:localhost:%d", "nokey@localhost.run"], r"https://[-a-z0-9]+\.lhr\.life"),
    ("serveo", ["-R", "80:localhost:%d", "serveo.net"], r"https://[-a-z0-9]+\.serveo(?:usercontent)?\.(?:net|com)"),
]


def try_ssh(name, args, rx, wait=25):
    """Пробует один ssh-туннель. Возвращает процесс, если ссылка получена, иначе None."""
    global PUBLIC_URL
    exe = shutil.which("ssh")
    if exe is None:
        return None
    cmd = [exe, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + os.devnull,
           "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]
    cmd += [a % PORT if "%d" in a else a for a in args]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = []

    def reader():
        for line in proc.stdout:
            m = re.search(rx, line)
            if m and not found:
                found.append(m.group(0))
    threading.Thread(target=reader, daemon=True).start()
    end = time.time() + wait
    while time.time() < end and not found and proc.poll() is None:
        time.sleep(0.3)
    if found and proc.poll() is None:
        PUBLIC_URL = found[0]
        return proc
    try:
        proc.terminate()
    except Exception:
        pass
    return None


def tunnel_manager():
    """Без Cloudflare: по очереди пробует ssh-туннели (localhost.run, serveo)."""
    state = {"procs": []}
    mode = os.environ.get("TUNNEL", "")
    if mode == "0":
        return state
    if mode == "cf":
        cf = start_tunnel()
        if cf is not None:
            state["procs"].append(cf)
        return state
    if shutil.which("ssh") is None:
        print("  В системе нет программы ssh, туннель создать нельзя.")
        print("  Windows 10/11: Параметры - Приложения - Дополнительные компоненты - Клиент OpenSSH.")
        return state

    def run():
        for name, args, rx in SSH_PROVIDERS:
            print("  Пробую туннель: %s ..." % name)
            pr = try_ssh(name, args, rx)
            if pr:
                state["procs"].append(pr)
                print()
                print("  >>> ПУБЛИЧНАЯ ССЫЛКА ДЛЯ ОНЛАЙН-ИГРОКОВ (%s):" % name)
                print("      " + PUBLIC_URL)
                print()
                # следим: если туннель оборвался, поднимаем снова
                pr.wait()
                print("  Туннель оборвался, подключаю заново...")
                return run()
            print("  %s недоступен." % name)
        print("  Ни один туннель не подключился. Попробуйте VPN или мобильный интернет (раздайте с телефона).")
    threading.Thread(target=run, daemon=True).start()
    return state


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if CLOUD:
        os.environ["NO_BROWSER"] = "1"
        os.environ["TUNNEL"] = "0"
    load_db()
    if not os.path.exists(os.path.join(ROOT, "index.html")):
        print("Не найден файл index.html рядом с server.py. Распакуйте архив целиком.")
        (None if CLOUD else input("Нажмите Enter, чтобы закрыть..."))
        return 1
    try:
        httpd = Server(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print("Не удалось запустить сервер на порту %d: %s" % (PORT, e))
        print("Возможно, игра уже запущена в другом окне. Закройте его и попробуйте снова.")
        (None if CLOUD else input("Нажмите Enter, чтобы закрыть..."))
        return 1
    ips = [] if CLOUD else lan_addresses()
    print("=" * 60)
    print("  СВОЯ ИГРА - локальный сервер запущен")
    print("=" * 60)
    print()
    print("  На ЭТОМ компьютере (ведущий) откройте:")
    print("      http://localhost:%d" % PORT)
    print()
    if ips:
        print("  Телефонам игроков (должны быть в той же Wi-Fi сети):")
        for ip in ips:
            print("      http://%s:%d" % (ip, PORT))
    elif CLOUD:
        print("  Режим облачного сервера, порт %d." % PORT)
    else:
        print("  Адрес в локальной сети определить не удалось.")
        print("  Подключите компьютер к Wi-Fi или включите мобильный хот-спот.")
    print()
    tunnels = tunnel_manager()
    if tunnels["procs"] or (os.environ.get("TUNNEL") != "0" and not PUBLIC_URL):
        print("  Запускаю туннель для онлайн-игроков, подождите до минуты...")
    print()
    print("  Чтобы остановить игру, закройте это окно.")
    print("=" * 60)
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open("http://localhost:%d" % PORT)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for pr in tunnels["procs"]:
            try:
                pr.terminate()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
