#!/usr/bin/env python3
"""Paste a UC / Quark / Baidu 网盘 share link -> the LAN box downloads it -> you get a link back.

Stdlib only. Run: python3 uc_lan.py --port 8517
"""
import argparse
import http.cookiejar
import http.cookies
import json
import mimetypes
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = ""
JOBS = {}
PARALLEL = 64  # UC guest downloads are throttled per connection; 64 is what makes it usable


def pwd_from_text(text):
    m = re.search(r"(?:pwd|passcode|提取码)[=:：\s]+([A-Za-z0-9]{1,8})", text or "")
    return m.group(1) if m else ""


def safe_name(name):
    name = name.replace("/", "_").replace("\\", "_").strip().strip(".")
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[-180:] or "file"


class UC:
    """UC 网盘: shares are readable without an account, and so are the CDN links."""
    name = "uc"
    parallel = 64
    API = "https://pc-api.uc.cn/1/clouddrive"
    # Pinned: quark's params (pr=ucpro...) return 41027 on UC.
    QUERY = "pr=UCBrowser&fr=pc&sys=darwin&ve=1.8.6"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
          "Safari/537.36 Channel/pckk_other_ch")
    # The CDN rejects the sharing UA; the signed URL checks a cookie the API handed us.
    DL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

    def __init__(self, cookie=""):
        self.jar = http.cookiejar.CookieJar()
        for part in (cookie or "").split(";"):
            k, _, v = part.partition("=")
            if k.strip() and v.strip():
                self.jar.set_cookie(http.cookiejar.Cookie(
                    0, k.strip(), v.strip(), None, False, ".uc.cn", True, True,
                    "/", True, False, None, False, None, None, {}))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def parse(self, url):
        host = urllib.parse.urlsplit(url or "").hostname or ""
        if not (host == "uc.cn" or host.endswith(".uc.cn")):
            return None
        m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
        return m.group(1) if m else None

    def cookie_header(self):
        return "; ".join(f"{c.name}={c.value}" for c in self.jar if "uc.cn" in (c.domain or ""))

    def api(self, method, path, params=None, body=None, query=QUERY):
        url = self.API + path + "?" + query
        if params:
            url += "&" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "content-type": "application/json", "referer": "https://drive.uc.cn",
            "user-agent": self.UA})
        try:
            with self.opener.open(req, timeout=30) as r:
                j = json.loads(r.read())
        except urllib.error.HTTPError as e:  # UC returns 404 with the real reason in the body
            j = json.loads(e.read() or b"{}")
        if j.get("status") != 200:
            raise RuntimeError(j.get("message") or f"UC API status {j.get('status')}")
        return j

    def walk(self, pwd_id, stoken, pdir="0", prefix="", depth=0):
        """Share listing works anonymously; folders recurse."""
        if depth > 8:
            return []
        files, page = [], 1
        while True:
            j = self.api("GET", "/share/sharepage/detail", {
                "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir, "force": "0",
                "_page": page, "_size": 50, "_sort": "file_type:asc,file_name:asc"})
            lst = j.get("data", {}).get("list") or []
            for f in lst:
                name = safe_name(f.get("file_name", ""))
                if f.get("dir"):
                    files += self.walk(pwd_id, stoken, f["fid"], prefix + name + "/", depth + 1)
                else:
                    files.append({"path": prefix + name, "fid": f["fid"],
                                  "token": f.get("share_fid_token", ""), "size": int(f.get("size") or 0)})
            total = int(j.get("metadata", {}).get("_total") or 0)
            if len(lst) < 50 or page >= 200 or (total and page * 50 >= total):
                return files
            page += 1

    def links(self, pwd_id, stoken, batch):
        j = self.api("POST", "/file/download", body={
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
                out.append((item.get("fid") or item.get("file_name"),
                            urllib.parse.urljoin("https://fast.uc.cn/", u)))
        return out

    def resolve(self, url, pwd):
        pwd_id = self.parse(url)
        if not pwd_id:
            raise RuntimeError("这不是 UC 分享链接 (需要 .../s/xxxx)")
        code = (pwd or "").strip() or pwd_from_text(url)
        stoken = self.api("POST", "/share/sharepage/token",
                          body={"pwd_id": pwd_id, "passcode": code})["data"]["stoken"]
        raw = self.walk(pwd_id, stoken)
        if not raw:
            raise RuntimeError("分享里没有文件（或已失效）")
        files = [{"path": f["path"], "size": f["size"], "id": f["fid"]} for f in raw]
        order = {id(f): i for i, f in enumerate(files)}  # duplicate paths must not collide
        cache = {}

        def link_of(file):
            i = order[id(file)]
            b = i // 50
            if b not in cache:  # one batch of 50 at a time, kept for the next file
                cache[b] = dict(self.links(pwd_id, stoken, raw[b * 50:(b + 1) * 50]))
            u = cache[b].get(file["id"]) or cache[b].get(file["path"])
            if not u:
                raise RuntimeError("无直链: " + file["path"])
            return u

        return files, link_of

    def open(self, url, start=None, end=None, tries=3):
        last = None
        for i in range(tries):
            h = {"user-agent": self.DL_UA, "referer": "https://fast.uc.cn/", "accept": "*/*",
                 "cookie": self.cookie_header()}
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


class Quark:
    """夸克网盘: anonymous works, but the dlink is bound to the __sdid cookie.

    Mint and download from the same instance; a different session gets 412.
    """
    name = "quark"
    parallel = 8
    BASE = "https://drive-pc.quark.cn/1/clouddrive"
    QUERY = "pr=ucpro&fr=pc&uc_param_str="
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 "
          "Channel/pckk_other_ch")
    DL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
    REFERER = "https://pan.quark.cn/"

    def __init__(self, cookie=""):
        self.cookie = (cookie or "").strip()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._tokens = {}  # fid -> share_fid_token, needed by /file/download

    def parse(self, url):
        host = urllib.parse.urlsplit(url or "").hostname or ""
        if not (host == "quark.cn" or host.endswith(".quark.cn")):
            return None
        m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
        return m.group(1) if m else None

    def cookies(self):
        jar = "; ".join(f"{c.name}={c.value}" for c in self.jar if "quark.cn" in (c.domain or ""))
        return "; ".join(x for x in (self.cookie, jar) if x)

    def api(self, method, path, params=None, body=None, tries=3):
        url = self.BASE + path + "?" + self.QUERY
        if params:
            url += "&" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"content-type": "application/json", "referer": self.REFERER,
                   "user-agent": self.UA, "cookie": self.cookies()}
        last = None
        for i in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self.opener.open(req, timeout=20) as r:
                    raw = r.read()
                break
            except urllib.error.HTTPError as e:  # Quark puts the real reason in the body
                raw = e.read()
                break
            except Exception as e:  # noqa: BLE001 - transient network, retry
                last = e
                if i == tries - 1:
                    raise
                time.sleep(1 + i)
        else:
            raise last
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise RuntimeError("夸克接口返回了非 JSON: " + raw[:160].decode("utf-8", "ignore"))

    def check(self, j):
        if j.get("status") == 200:
            return j
        code = j.get("code")
        hint = {
            31001: "访客直链被限额拒绝（31001，随机出现，重试常可成功）",
            41006: "分享不存在（链接失效或拼错）",
            41007: "提取码错误",
            41008: "需要提取码",
            41019: "分享地址已过期",
        }.get(code)
        msg = hint or j.get("message") or f"夸克接口 status {j.get('status')} code {code}"
        raise RuntimeError(f"{msg} (code {code})" if code else msg)

    def resolve(self, url, pwd):
        pwd_id = self.parse(url)
        if not pwd_id:
            raise RuntimeError("这不是夸克分享链接 (需要 pan.quark.cn/s/xxxx)")
        passcode = (pwd or "").strip() or pwd_from_text(url)
        tok = self.check(self.api("POST", "/share/sharepage/token",
                                  body={"pwd_id": pwd_id, "passcode": passcode}))
        stoken = (tok.get("data") or {}).get("stoken")
        if not stoken:
            raise RuntimeError("没有拿到 stoken（分享可能要求登录）")
        self._pwd_id, self._stoken = pwd_id, stoken

        files = self.walk(pwd_id, stoken)
        if not files:
            raise RuntimeError("分享里没有文件（或已失效）")
        return files, lambda f: self.link(f)

    def walk(self, pwd_id, stoken, pdir="0", prefix="", depth=0):
        if depth > 8:
            return []
        out, page = [], 1
        while True:
            j = self.check(self.api("GET", "/share/sharepage/detail", {
                "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir, "force": "0",
                "_page": page, "_size": 50, "_sort": "file_type:asc,file_name:asc"}))
            lst = (j.get("data") or {}).get("list") or []
            for f in lst:
                name = safe_name(f.get("file_name", ""))
                if f.get("dir"):
                    out += self.walk(pwd_id, stoken, f["fid"], prefix + name + "/", depth + 1)
                else:
                    fid = f["fid"]
                    self._tokens[fid] = f.get("share_fid_token") or ""
                    out.append({"path": prefix + name, "size": int(f.get("size") or 0), "id": fid})
            total = int((j.get("metadata") or {}).get("_total") or 0)
            if len(lst) < 50 or page >= 200 or (total and page * 50 >= total):
                return out
            page += 1

    def link(self, f):
        fid = f["id"]
        for i in range(3):
            j = self.api("POST", "/file/download",
                         params={"entry": "ft", "fr": "pc", "pr": "ucpro"},
                         body={"fids": [fid], "fids_token": [self._tokens.get(fid, "")],
                               "pwd_id": self._pwd_id, "stoken": self._stoken})
            # 31001 pops up at random for guests and the next attempt usually succeeds.
            if j.get("status") == 200 or j.get("code") != 31001:
                break
            time.sleep(1 + i)
        j = self.check(j)
        item = (j.get("data") or [{}])[0]
        u = item.get("download_url")
        if isinstance(u, list):
            u = u[0] if u else None
        if not u:
            raise RuntimeError("没有拿到直链（需要登录账号或被风控）")
        return urllib.parse.urljoin("https://cdn.quark.cn/", u)

    def open(self, url, start=None, end=None, tries=3):
        last = None
        for i in range(tries):
            # __sdid (set by the token call) must travel with the dlink: the CDN binds
            # the signature to it. A fresh jar or another session's cookie gets 412.
            h = {"user-agent": self.DL_UA, "referer": self.REFERER, "accept": "*/*",
                 "cookie": self.cookies()}
            if start is not None:
                h["range"] = f"bytes={start}-{end}"
            try:
                return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90)
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise RuntimeError("CDN 拒绝了请求（403）: 直链过期或会话不匹配 - "
                                       + e.read()[:160].decode("utf-8", "ignore"))
                if e.code == 412:
                    raise RuntimeError("CDN 拒绝了请求（412）: 直链和当前会话的 __sdid 不匹配")
                if e.code in (400, 404, 410):
                    raise RuntimeError(f"CDN 返回 {e.code}: 直链失效: "
                                       + e.read()[:160].decode("utf-8", "ignore"))
                last = e
            except Exception as e:  # noqa: BLE001 - retry any transient network failure
                last = e
            time.sleep(1 + i)
        raise last


class Baidu:
    """百度网盘: listing is anonymous, but the dlink needs a logged-in cookie.

    Required keys: BDUSS + STOKEN (BAIDUID helps). Without them resolve() still
    lists the share and link_of() raises a clear Chinese error.
    """
    name = "baidu"
    parallel = 4  # account-wide throttle; more connections do not help
    PAN = "https://pan.baidu.com"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    VERIFY_ERR = {-9: "提取码错误", -12: "没有填提取码（或触发了百度风控）",
                  105: "分享不存在或已失效"}

    def __init__(self, cookie=""):
        # No http.cookiejar: BDCLND is issued by verify, a plain dict is enough.
        self.ck = {}
        for part in (cookie or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k and v:
                self.ck[k] = v
        self.info = {}  # seg -> shareid / uk / bdstoken / jsToken / sekey

    def parse(self, url):
        host = urllib.parse.urlsplit(url or "").hostname or ""
        if host not in ("pan.baidu.com", "yun.baidu.com"):
            return None
        m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
        return m.group(1) if m else None

    def req(self, url, ref=None, data=None, extra=None):
        h = {"user-agent": self.UA, "referer": ref or self.PAN + "/"}
        if self.ck:
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.ck.items())
        if data is not None:
            h["content-type"] = "application/x-www-form-urlencoded"
        h.update(extra or {})
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=60)
        for sc in r.headers.get_all("set-cookie") or []:  # verify issues BDCLND here
            for k, m in http.cookies.SimpleCookie(sc).items():
                self.ck[k] = m.value
        return r

    def json(self, url, ref=None, data=None):
        return json.loads(self.req(url, ref, data).read() or b"{}")

    def resolve(self, url, pwd=""):
        seg = self.parse(url)
        if not seg:
            raise RuntimeError("这不是百度网盘分享链接 (需要 pan.baidu.com/s/xxxx)")
        pwd = (pwd or "").strip() or (urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query).get("pwd") or [""])[0]
        surl = seg[1:] if seg.startswith("1") else seg
        share = f"{self.PAN}/s/{seg}"
        if pwd:
            j = self.json(
                f"{self.PAN}/share/verify?surl={surl}&t={int(time.time() * 1000)}"
                f"&channel=chunlei&web=1&app_id=250528&bdstoken=null",
                share, urllib.parse.urlencode({"pwd": pwd}).encode())
            err = j.get("errno")
            if err:
                raise RuntimeError(self.VERIFY_ERR.get(err, f"提取失败 errno={err}"))
        for i in range(2):  # occasionally falls back to the old verify page; ask again
            html = self.req(share).read().decode("utf-8", "ignore")
            m = re.search(r'<script id="locals-data" type="application/json">(.*?)</script>',
                          html, re.S)
            if m:
                break
            time.sleep(1 + i)
        if not m:
            raise RuntimeError("该分享需要提取码，或已失效（百度没返回文件列表）")
        d = json.loads(m.group(1))
        if d.get("errno"):
            raise RuntimeError(f"分享打不开: {d.get('show_msg') or d.get('errno')}")
        self.info[seg] = {
            "shareid": str(d.get("shareid") or ""),
            "uk": str(d.get("share_uk") or ""),
            "bdstoken": d.get("bdstoken") or "",
            "jsToken": d.get("jsToken") or "",
            "sekey": d.get("currentSekey") or d.get("sekey") or urllib.parse.unquote(
                self.ck.get("BDCLND", "")),
        }
        files = []
        for item in d.get("file_list") or []:
            self.walk(surl, share, item, "", files, 0)
        if not files:
            raise RuntimeError("分享里没有文件（或已失效）")
        return files, lambda f: self.dlink(seg, f["id"])

    def walk(self, surl, share, item, prefix, out, depth):
        name = safe_name(str(item.get("server_filename") or ""))
        if int(item.get("isdir") or 0):
            if depth > 8:
                return
            for sub in self.list(surl, share, item.get("path") or ""):
                self.walk(surl, share, sub, prefix + name + "/", out, depth + 1)
        else:
            out.append({"path": prefix + name, "size": int(item.get("size") or 0),
                        "id": str(item.get("fs_id"))})

    def list(self, surl, share, path):
        out, page = [], 1
        while True:
            q = urllib.parse.urlencode({
                "shorturl": surl, "dir": path, "web": 5, "app_id": 250528, "desc": 1,
                "showempty": 0, "page": page, "num": 100, "order": "time", "view_mode": 1,
                "channel": "chunlei", "bdstoken": "", "clienttype": 0})
            lst = self.json(f"{self.PAN}/share/list?{q}", share).get("list") or []
            out += lst
            if len(lst) < 100 or page >= 200:
                return out
            page += 1

    def dlink(self, seg, fid):
        info = self.info[seg]
        share = f"{self.PAN}/s/{seg}"
        # sign/timestamp are no longer in the page; tplconfig hands them out.
        auth = info.get("auth")
        if not auth or time.time() - auth[0] > 60:
            tj = self.json(f"{self.PAN}/share/tplconfig?" + urllib.parse.urlencode({
                "surl": seg, "fields": "sign,timestamp", "view_mode": 1, "channel": "chunlei",
                "web": 1, "app_id": 250528, "bdstoken": info["bdstoken"], "clienttype": 0}),
                share)
            data = tj.get("data") or {}
            if tj.get("errno") or not data.get("sign"):
                raise RuntimeError(f"取不到下载签名 (errno={tj.get('errno')})，可能触发了风控")
            auth = info["auth"] = (time.time(), data["sign"], data["timestamp"])
        q = {"sign": auth[1], "timestamp": auth[2], "channel": "chunlei",
             "web": 1, "app_id": 250528, "bdstoken": info["bdstoken"], "clienttype": 0}
        if info["jsToken"]:
            q["jsToken"] = info["jsToken"]
        body = urllib.parse.urlencode({
            "encrypt": 0, "type": "dlink", "product": "share", "uk": info["uk"],
            "primaryid": info["shareid"],
            "fid_list": json.dumps([fid], separators=(",", ":")),
            "extra": json.dumps({"sekey": info["sekey"]}, separators=(",", ":"))})
        j = self.json(f"{self.PAN}/api/sharedownload?{urllib.parse.urlencode(q)}",
                      share, body.encode())
        lst = j.get("list") or j.get("dlink_list")  # web vs plugin shape
        dl = lst[0].get("dlink") if isinstance(lst, list) and lst else j.get("dlink")
        if not isinstance(dl, str) or not dl:
            raise RuntimeError("百度没有给出直链：下载需要登录，请提供 BDUSS/STOKEN Cookie")
        # Anonymous callers get a dstime backdated by 1h (expires=1h), dead on arrival.
        m = re.search(r"dstime=(\d+)", dl)
        if m and time.time() - int(m.group(1)) > 3600:
            raise RuntimeError("百度只给了过期直链（31360）：未登录不能下载，"
                               "请填 BDUSS/STOKEN Cookie")
        return dl

    def open(self, url, start=None, end=None, tries=3):
        last = None
        for i in range(tries):
            h = {}
            if start is not None:
                h["range"] = f"bytes={start}-{'' if end is None else end}"
            try:
                return self.req(url, self.PAN + "/", extra=h)
            except urllib.error.HTTPError as e:
                body = e.read()[:200].decode("utf-8", "ignore")
                if e.code == 403:  # expired/unauthorized, retrying does not help
                    if "31360" in body or "31064" in body:
                        raise RuntimeError("百度 CDN 拒绝（403）: 未登录只发过期/未授权直链，"
                                           "请提供 BDUSS/STOKEN Cookie")
                    raise RuntimeError("百度 CDN 拒绝了请求（403）: " + body[:160])
                if e.code not in (408, 429, 500, 502, 503, 504):
                    raise
                last = e
            except Exception as e:  # noqa: BLE001 - retry network hiccups
                last = e
            time.sleep(1 + i)
        raise last


DRIVES = (UC, Quark, Baidu)


def drive_for(url, cookie=""):
    for cls in DRIVES:
        if cls().parse(url):
            # Only Baidu needs a pasted cookie; never forward another site's
            # cookies to UC/Quark.
            return cls(cookie if cls is Baidu else "")
    raise RuntimeError("不支持的分享链接（支持 UC / 夸克 / 百度网盘）")


def download(open_, url, dest, expect, tick, parallel=PARALLEL):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open_(url, 0, 0) as r:
        total = expect
        ranged = r.status == 206
        m = re.search(r"/(\d+)$", r.headers.get("content-range") or "")
        if m:
            total = int(m.group(1))
    if not ranged or total < 8 * 1024 * 1024:
        with open_(url) as r, open(dest, "wb") as f:
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
                        with open_(url, pos, end) as r:
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
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, parallel))]
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


def grab(job, url, pwd, cookie):
    try:
        d = drive_for(url, cookie)
        files, link_of = d.resolve(url, pwd)
        if not files:
            raise RuntimeError("分享里没有文件（或已失效）")
        seen, rows = set(), []
        for f in files:  # two folders can yield the same relative path; keep both
            path, n = f["path"], 2
            stem, ext = os.path.splitext(path)
            while path in seen:
                path = f"{stem} ({n}){ext}"
                n += 1
            seen.add(path)
            rows.append({"path": path, "size": f["size"], "done": 0, "state": "waiting"})
        job["files"] = rows
        job["total"] = sum(f["size"] for f in files)
        job["state"] = "下载中"
        for item, f in zip(rows, files):
            item["state"] = "下载中"
            dest = os.path.join(job["dir"], item["path"])
            download(d.open, link_of(f), dest, f["size"],
                     lambda n, it=item: it.__setitem__("done", it["done"] + n),
                     getattr(d, "parallel", PARALLEL))
            item["state"] = "完成"
            item["url"] = "/d/" + job["id"] + "/" + urllib.parse.quote(item["path"])
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
        threading.Thread(target=grab, args=(
            job, body.get("url", ""), body.get("pwd", ""), body.get("cookie", "")),
            daemon=True).start()
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
<title>网盘 → 局域网</title>
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
input#cookie{flex:1 1 100%;font-size:13px}
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
<h1>网盘 → 局域网</h1>
<p class="sub">粘贴 UC / 夸克 / 百度 分享链接，服务器下载后给你一个直链。提取码可以一起粘进来。百度下载需要登录 Cookie。</p>
<form id="f">
  <input id="url" placeholder="https://drive.uc.cn/s/xxxxx  或  pan.quark.cn/s/xxxxx  或  pan.baidu.com/s/xxxxx" autocomplete="off">
  <input id="pwd" placeholder="提取码" autocomplete="off">
  <input id="cookie" type="password" placeholder="百度 Cookie（BDUSS=…; STOKEN=…），UC / 夸克留空" autocomplete="off">
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
    body:JSON.stringify({url:document.getElementById('url').value,pwd:document.getElementById('pwd').value,
      cookie:document.getElementById('cookie').value})});
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
