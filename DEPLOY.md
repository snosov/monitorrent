# Deploying monitorrent (fixes branch)

Runs monitorrent with a FlareSolverr sidecar. kinozal and rutracker put a
Cloudflare challenge in front of plain HTTP clients; FlareSolverr passes it
and monitorrent reuses the clearance. Without the sidecar those two trackers
cannot parse, log in or download.

## 1. Code

```bash
git clone https://github.com/snosov/monitorrent.git
cd monitorrent
git checkout fixes
```

## 2. Database

Point monitorrent at the folder that holds your `monitorrent.db`:

```bash
echo "MONITORRENT_DATA_DIR=/path/to/folder" > .env
```

Without it, `./data` is used and a fresh database is created there. The
folder is mounted as `/data` in the container, and the file must be named
`monitorrent.db`.

**Back the database up first.** On first start the new code migrates it:
the kinozal credentials table gains a `domain` column. The change only adds a
column, but it is one-way.

## 3. Image - pick one

Check the machine with `uname -m`.

- **`x86_64`** - build it (pulls base images, takes several minutes):
  ```bash
  docker compose build
  ```
- **`aarch64`**, with a saved image `monitorrent-image-arm64.tar.gz`:
  ```bash
  gunzip -c monitorrent-image-arm64.tar.gz | docker load
  ```
  An arm64 image will not run on x86_64, and vice versa.

## 4. Start

```bash
docker compose up -d
docker compose ps        # both Up, monitorrent (healthy)
```

`flaresolverr` is pulled automatically and is deliberately not published on
the host: it is an open proxy.

## 5. After first start - in the UI

- **Settings -> Trackers -> kinozal -> Domain: `kinozal.guru`.** The migration
  sets existing credentials to `kinozal.tv`, which no longer resolves, so
  kinozal fails until this is changed.
- **Settings -> Clients:** make sure the default client is one that is
  actually configured. If it is the downloader, its folder has to be inside
  the mounted data folder, e.g. `/data/torrents` - anything else is lost when
  the container is recreated.
- **rutracker:** the pasted `cf_clearance` / User-Agent fields are no longer
  needed; FlareSolverr supplies both.
- **nnmclub:** paste `phpbb2mysql_4_sid` and `phpbb2mysql_4_data` from a
  logged-in browser into its settings. Its login form has a CAPTCHA, so
  username/password alone cannot work.

## Notes

- Files the downloader writes are owned by root (the container runs as
  root), so moving or deleting them on the host needs `sudo`.
- Run one instance per database. Two boxes on the same topics download the
  same torrents twice.
- If kinozal or rutracker start failing again, the execute log says
  "FlareSolverr could not solve ..." - Cloudflare has changed its checks.
