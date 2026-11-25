# netdisk-downloader

Paste a UC / Quark / Baidu 网盘 share link in a browser → the LAN box downloads it → you get a direct link back.

| provider | list a share | download |
|---|---|---|
| UC | no account | no account (the CDN checks the guest cookie) |
| Quark | no account | no account (the dlink is bound to the session's `__sdid`) |
| Baidu | no account | **logged-in cookie required** (`BDUSS` + `STOKEN`) |

## Run

```sh
python3 uc_lan.py --port 8517 --dir ~/uc-downloads
```

Open `http://<host>:8517`, paste e.g. `https://drive.uc.cn/s/xxxxx`,
`https://pan.quark.cn/s/xxxxx` or `https://pan.baidu.com/s/xxxxx` (提取码 can be
pasted in the same box or typed in the second field). Files land in
`<dir>/<job>/…` and are served at `/d/<job>/<path>` with Range support, so
video seeking and resume work.

For Baidu, log in at `pan.baidu.com`, open DevTools → Application → Cookies,
and paste `BDUSS=…; STOKEN=…; BAIDUID=…` into the Cookie field. It stays in
memory for that job only; nothing is written to disk. Without it the share
still lists, then the job fails with a clear message (anonymous links are
minted already expired).

Stdlib only — no pip install, no venv.

## Deploy as a service

```sh
sudo cp uclan.service /etc/systemd/system/
sudo systemctl enable --now uclan
```

Edit `User=` / paths in the unit for a non-`dave` / non-`/home/dave` host.

## What makes or breaks each one

1. **UC: the CDN needs the guest cookie.** UC signs the download URL, but the
   bucket callback still checks the session cookie issued by the API — without
   it every request is `403 RequestDeniedByCallback: require login [auth not
   found]`. Hence the cookie jar, a plain Chrome UA (the sharing UA is rejected)
   and the `fast.uc.cn` referer.
2. **Quark: mint and fetch in the same session.** `/share/sharepage/token` sets
   an `__sdid` cookie that the signed dlink is bound to; a link fetched by a
   different session gets a bare `412`. Guest `/file/download` also returns
   `31001` at random, so a direct link is retried 3×. Listing is sequential:
   a 179-file / 30-folder share takes ~2 minutes from overseas.
3. **Baidu: download is gated on login.** Listing works anonymously (share
   page → `BDCLND` cookie → embedded `file_list`). The old `sharedownload`
   endpoint still exists, but anonymous callers get a link backdated by one
   hour (`31360` expired) even for a 140-byte file. With `BDUSS`/`STOKEN` it is
   the same call the web page makes. Non-SVIP is throttled account-wide, so
   Baidu downloads use 4 connections instead of 64.

Guest UC downloads are throttled per connection (~100 KB/s). Files are fetched
over a 4 MB work queue; a fixed partition leaves a straggler holding the tail
at 200 KB/s. Typical UC result: ~2–4 MB/s, plateauing after ~200 MB. A
logged-in UC cookie would be several times faster (not implemented).

## Notes

- No auth: anyone who can reach the port can queue jobs, and a pasted Baidu
  cookie is used server-side for that job.
- Nothing cleans up `<dir>`.
- UC is pinned to `pr=UCBrowser&fr=pc&sys=darwin&ve=1.8.6`; Quark needs
  `pr=ucpro` on `drive-pc.quark.cn` (the UC host returns `41027` for those
  params).
