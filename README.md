# uclan

Paste a UC 网盘 share link in a browser → the LAN box downloads it → you get a direct link back.

UC share links are readable **without an account** (`pc-api.uc.cn`), so this needs
no login, no cookie pasting, no browser extension.

## Run

```sh
python3 uc_lan.py --port 8517 --dir ~/uc-downloads
```

Open `http://<host>:8517`, paste e.g. `https://drive.uc.cn/s/xxxxx` (提取码 can be
pasted in the same box or typed in the second field). Files land in
`<dir>/<job>/…` and are served at `/d/<job>/<path>` with Range support, so
video seeking and resume work.

Stdlib only — no pip install, no venv.

## Deploy as a service

```sh
sudo cp uclan.service /etc/systemd/system/
sudo systemctl enable --now uclan
```

Edit `User=` / paths in the unit for a non-`dave` / non-`/home/dave` host.

## Two things that make or break it

1. **The CDN needs the guest cookie.** UC signs the download URL, but the bucket
   callback still checks for the session cookie issued by the API — without it
   every request is `403 RequestDeniedByCallback: require login [auth not found]`.
   Hence the cookie jar, a plain Chrome UA (the sharing UA is rejected) and the
   `fast.uc.cn` referer.
2. **Guest downloads are throttled per connection** (~100 KB/s). Files are
   fetched over a 64-thread, 4 MB work queue; a fixed partition leaves a
   straggler holding the tail at 200 KB/s.

Typical result: ~2–4 MB/s, plateauing after ~200 MB. A logged-in UC cookie
would be several times faster (not implemented).

## Notes

- No auth: anyone who can reach the port can queue jobs.
- Nothing cleans up `<dir>`.
- API is pinned to `pr=UCBrowser&fr=pc&sys=darwin&ve=1.8.6`; Quark's params
  (`pr=ucpro`) return `41027 分享不存在` on UC.
