#!/usr/bin/env python3
"""Paste a UC 网盘 share link in the browser -> the LAN box downloads it -> you get a link back.

Stdlib only. Run: python3 uc_lan.py --port 8517
"""
import argparse
import http.cookiejar
import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://pc-api.uc.cn/1/clouddrive"
# Pinned: quark's params (pr=ucpro...) return 41027 on UC.
QUERY = "pr=UCBrowser&fr=pc&sys=darwin&ve=1.8.6"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
      "Safari/537.36 Channel/pckk_other_ch")
# The CDN rejects the sharing UA; the signed URL checks a cookie the API handed us.
DL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))
ROOT = ""
JOBS = {}
PARALLEL = 64  # guest downloads are throttled per connection (~100 KB/s); 64 is what makes it usable


def cookie_header():
    return "; ".join(f"{c.name}={c.value}" for c in JAR if "uc.cn" in (c.domain or ""))


def api(method, path, params=None, body=None, base=None, query=QUERY):
    url = (base or API) + path + ("?" + query)
    if params:
        url += "&" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "content-type": "application/json", "referer": "https://drive.uc.cn", "user-agent": UA})
    try:
        with OPENER.open(req, timeout=30) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:  # UC returns 404 with the real reason in the body
        j = json.loads(e.read() or b"{}")
    if j.get("status") != 200:
        raise RuntimeError(j.get("message") or f"UC API status {j.get('status')}")
    return j


def parse_share(text, passcode=""):
    m = re.search(r"/s/([A-Za-z0-9_-]+)", text or "")
    if not m:
        raise RuntimeError("这不是 UC 分享链接 (需要 .../s/xxxx)")
    pwd = passcode.strip()
    if not pwd:
        mm = re.search(r"(?:pwd|passcode|提取码)[=:：\s]+([A-Za-z0-9]{1,8})", text)
        pwd = mm.group(1) if mm else ""
    return m.group(1), pwd


def safe_name(name):
    name = name.replace("/", "_").replace("\\", "_").strip().strip(".")
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[-180:] or "file"


def walk(pwd_id, stoken, pdir="0", prefix="", depth=0):
    """Share listing works anonymously; folders recurse."""
    if depth > 8:
        return []
    files, page = [], 1
    while True:
        j = api("GET", "/share/sharepage/detail", {
            "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir, "force": "0",
            "_page": page, "_size": 50, "_sort": "file_type:asc,file_name:asc"})
        lst = j.get("data", {}).get("list") or []
        for f in lst:
            name = safe_name(f.get("file_name", ""))
            if f.get("dir"):
                files += walk(pwd_id, stoken, f["fid"], prefix + name + "/", depth + 1)
            else:
                files.append({"path": prefix + name, "fid": f["fid"],
                              "token": f.get("share_fid_token", ""), "size": int(f.get("size") or 0)})
        total = int(j.get("metadata", {}).get("_total") or 0)
        if len(lst) < 50 or page * 50 >= total:
            return files
        page += 1


def links(pwd_id, stoken, batch):
    j = api("POST", "/file/download", body={
        "fids": [f["fid"] for f in batch],
        "fids_token": [f["token"] for f in batch],
        "pwd_id": pwd_id, "stoken": stoken,
    }, query="entry=ft&fr=pc&pr=UCBrowser")
    out = []
    for item in j.get("data") or []:
        u = item.get("download_url")
        if isinstance(u, list):
            u = u[0] if u else None
        if u:
            out.append((item.get("fid") or item.get("file_name"), urllib.parse.urljoin("https://fast.uc.cn/", u)))
    return out


def _open(url, start=None, end=None, tries=3):
    last = None
    for i in range(tries):
        h = {"user-agent": DL_UA, "referer": "https://fast.uc.cn/", "accept": "*/*",
             "cookie": cookie_header()}
        if start is not None:
            h["range"] = f"bytes={start}-{end}"
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90)
        except urllib.error.HTTPError as e:
            if e.code == 403:  # link expired or guest cookie lost; retrying will not help
                raise RuntimeError("CDN 拒绝了请求（403）: " + e.read()[:160].decode("utf-8", "ignore"))
            last = e
        except Exception as e:  # noqa: BLE001 - retry any transient network failure
            last = e
        time.sleep(1 + i)
    raise last


def download(url, dest, expect, tick):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with _open(url, 0, 0) as r:
        total = expect
        ranged = r.status == 206
        m = re.search(r"/(\d+)$", r.headers.get("content-range") or "")
        if m:
            total = int(m.group(1))
    if not ranged or total < 8 * 1024 * 1024:
        with _open(url) as r, open(dest, "wb") as f:
            while True:
                b = r.read(1 << 18)
                if not b:
                    break
                f.write(b)
                tick(len(b))
        return

    # 4 MB work queue, not fixed partitions: slow connections pick up chunks instead of
    # leaving one straggler holding the tail at 100 KB/s.
    CHUNK = 4 << 20
    queue = [(s, min(total, s + CHUNK) - 1) for s in range(0, total, CHUNK)]
    lock = threading.Lock()
    errors = []
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    def worker():
        while True:
            with lock:
                if not queue or errors:
                    return
                start, end = queue.pop()
            try:
                pos, fails = start, 0
                while pos <= end:
                    try:
                        with _open(url, pos, end) as r:
                            while pos <= end:
                                b = r.read(min(1 << 18, end - pos + 1))
                                if not b:
                                    raise IOError("short read")
                                os.pwrite(fd, b, pos)
                                pos += len(b)
                                tick(len(b))
                    except Exception:  # noqa: BLE001 - retry the remaining span
                        fails += 1
                        if fails > 3:
                            raise
                        time.sleep(fails)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)
                return

    try:
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(PARALLEL)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        os.close(fd)
    if errors:
        raise errors[0]
    if total and os.path.getsize(dest) != total:
        raise RuntimeError(f"size mismatch: {os.path.getsize(dest)} != {total}")


def grab(job, url, passcode):
    try:
        pwd_id, pwd = parse_share(url, passcode)
        stoken = api("POST", "/share/sharepage/token", body={"pwd_id": pwd_id, "passcode": pwd})["data"]["stoken"]
        files = walk(pwd_id, stoken)
        if not files:
            raise RuntimeError("分享里没有文件（或已失效）")
        job["files"] = [{"path": f["path"], "size": f["size"], "done": 0, "state": "waiting"} for f in files]
        job["total"] = sum(f["size"] for f in files)
        job["state"] = "下载中"
        for i in range(0, len(files), 50):
            batch = files[i:i + 50]
            got = dict(links(pwd_id, stoken, batch))
            for f in batch:
                item = next(x for x in job["files"] if x["path"] == f["path"])
                u = got.get(f["fid"]) or got.get(f["path"])
                if not u:
                    item["state"] = "失败: 无直链"
                    continue
                item["state"] = "下载中"
                dest = os.path.join(job["dir"], f["path"])
                download(u, dest, f["size"], lambda n, it=item: it.__setitem__("done", it["done"] + n))
                item["state"] = "完成"
                item["url"] = "/d/" + job["id"] + "/" + urllib.parse.quote(f["path"])
        job["state"] = "完成"
    except Exception as e:  # noqa: BLE001
        job["state"] = "失败: " + str(e)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/":
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p.path == "/api/job":
            job = JOBS.get(urllib.parse.parse_qs(p.query).get("id", [""])[0])
            if not job:
                return self._json(404, {"error": "no such job"})
            self._json(200, {k: job[k] for k in ("state", "files", "total")})
        elif p.path.startswith("/d/"):
            self.serve(urllib.parse.unquote(p.path[3:]))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/grab":
            return self._json(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        job = {"id": uuid.uuid4().hex[:10], "state": "解析中", "files": [], "total": 0}
        job["dir"] = os.path.join(ROOT, job["id"])
        JOBS[job["id"]] = job
        threading.Thread(target=grab, args=(job, body.get("url", ""), body.get("pwd", "")), daemon=True).start()
        self._json(200, {"id": job["id"]})

    def serve(self, rel):
        job_id, _, rest = rel.partition("/")
        path = os.path.realpath(os.path.join(ROOT, job_id, rest))
        if not path.startswith(os.path.realpath(ROOT) + os.sep) or not os.path.isfile(path):
            return self._json(404, {"error": "not found"})
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size:
                self.send_response(416)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            code = 206
        import mimetypes
        self.send_response(code)
        self.send_header("content-type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("content-length", str(end - start + 1))
        self.send_header("accept-ranges", "bytes")
        if code == 206:
            self.send_header("content-range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(1 << 18, left))
                if not b:
                    break
                try:
                    self.wfile.write(b)
                except ConnectionError:
                    return
                left -= len(b)


PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UC 网盘 → LAN</title>
<style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;background:#0e1014;color:#e6e8ee;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:56px 20px}
h1{font-size:22px;letter-spacing:.2px;margin:0 0 6px}
p.sub{color:#8b93a7;margin:0 0 28px}
form{display:flex;gap:10px;flex-wrap:wrap}
input{flex:1 1 420px;min-width:0;padding:12px 14px;border-radius:10px;border:1px solid #2a3040;background:#151922;color:inherit;font:inherit;outline:none}
input:focus{border-color:#4f7cff}
input#pwd{flex:0 0 110px}
button{padding:12px 22px;border-radius:10px;border:0;background:#4f7cff;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
ul{list-style:none;padding:0;margin:28px 0 0}
li{padding:14px;border:1px solid #212734;border-radius:12px;margin-bottom:10px;background:#12161e}
.row{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m{color:#8b93a7;font-size:13px;white-space:nowrap}
.bar{height:4px;border-radius:2px;background:#232a38;margin-top:10px;overflow:hidden}
.bar>i{display:block;height:100%;background:#4f7cff;width:0;transition:width .2s}
a{color:#7aa2ff}
.err{color:#ff7b72;margin-top:24px}
</style>
<main>
<h1>UC 网盘 → 局域网</h1>
<p class="sub">粘贴分享链接，服务器下载后给你一个直链。提取码可以一起粘进来。</p>
<form id="f">
  <input id="url" placeholder="https://drive.uc.cn/s/xxxxx  或  https://fast.uc.cn/s/xxxxx" autocomplete="off">
  <input id="pwd" placeholder="提取码" autocomplete="off">
  <button id="go">下载</button>
</form>
<div id="msg"></div>
<ul id="list"></ul>
</main>
<script>
const f=document.getElementById('f'),list=document.getElementById('list'),msg=document.getElementById('msg'),go=document.getElementById('go');
const fmt=n=>n>1073741824?(n/1073741824).toFixed(2)+' GB':n>1048576?(n/1048576).toFixed(1)+' MB':(n/1024).toFixed(0)+' KB';
f.onsubmit=async e=>{
  e.preventDefault();msg.innerHTML='';list.innerHTML='';go.disabled=true;
  const r=await fetch('/api/grab',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({url:document.getElementById('url').value,pwd:document.getElementById('pwd').value})});
  const {id}=await r.json();let last=0,lastT=Date.now(),speed=0;
  const tick=async()=>{
    const j=await (await fetch('/api/job?id='+id)).json();
    const done=j.files.reduce((a,b)=>a+b.done,0);
    const now=Date.now();
    if(now-lastT>800){speed=(done-last)/(now-lastT)*1000;last=done;lastT=now;}
    list.innerHTML=j.files.map(x=>{
      const p=x.size?Math.min(100,x.done/x.size*100):0;
      return `<li><div class="row"><span class="n">${x.path.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</span>
      <span class="m">${x.size?fmt(x.done)+' / '+fmt(x.size):''} ${x.state}</span></div>
      ${x.size?`<div class="bar"><i style="width:${p}%"></i></div>`:''}
      ${x.url?`<div class="m" style="margin-top:8px"><a href="${x.url}">${location.origin}${x.url}</a></div>`:''}</li>`}).join('');
    msg.textContent=j.state+(speed>0&&j.state!=='完成'?' · '+fmt(speed)+'/s':'');
    msg.className=j.state.startsWith('失败')?'err':'';
    if(j.state==='完成'||j.state.startsWith('失败')){go.disabled=false;return;}
    setTimeout(tick,1000);
  };
  tick();
};
</script></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8517)
    ap.add_argument("--dir", default=os.path.expanduser("~/uc-downloads"))
    a = ap.parse_args()
    ROOT = a.dir
    os.makedirs(ROOT, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"http://0.0.0.0:{a.port}  -> {ROOT}", flush=True)
    srv.serve_forever()
