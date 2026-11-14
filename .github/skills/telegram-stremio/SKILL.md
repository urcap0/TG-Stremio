---
name: telegram-stremio
description: 'Project skill for Telegram-Stremio, a self-hosted media server (FastAPI + MongoDB/Motor + PyroFork) that turns Telegram channels into a Stremio addon. USE WHEN working in this repo: editing Backend/fastapi/ routes (stremio, stream, api, template, webdav), Backend/helper/ modules, metadata providers (tmdb, tvdb, kitsu, cinemeta), pyrofork bot plugins, database.py, settings_manager.py, config.env, or deployment files (Dockerfile, docker-compose.yaml, Caddyfile, heroku.yml). Covers single-process architecture, boot order, stream id encoding, settings storage, ingestion pipeline, conventions, and run commands (uv run -m Backend).'
---

# Telegram-Stremio Project Skill

## What this project is

Self-hosted "Telegram Stremio" media server, version 5.x (local tree at 5.3.4). Users forward movies/episodes to Telegram channels; a PyroFork bot indexes them into MongoDB; a FastAPI app serves them as a Stremio addon (`/stremio/{token}/manifest.json`) plus a web admin panel, WebDAV, Nuvio collections, and a subscriptions bot flow. Upstream: `github.com/weebzone/Telegram-Stremio`. Current upstream sync base: `f6e1e97` (v5.3.4; admin-wide catalog hiding, reorderable Trending catalogs, mobile-friendly catalog controls, and a web-panel demo/press kit). The v5.3.3 redesign, v5.3.2 library export, and earlier catalog/Nuvio/auth/streaming behavior remain.

**Key dependencies:** FastAPI, uvicorn (uvloop + httptools), Motor, PyroFork >= 2.3.61, themoviedb, GuessIt, parse-torrent-title, rapidfuzz, Jinja2, itsdangerous. Python >= 3.11, managed with `uv` (`pyproject.toml`).

## When to use

- Adding/editing API routes, Stremio addon endpoints, stream handlers, or admin pages.
- Touching the ingestion pipeline (receiver plugin, metadata resolvers, scan managers).
- Changing configuration (settings manager, config.env), tokens/subscriptions logic, or DB schema.
- Debugging streaming, proxying (MediaFlow), WebDAV, global search, or fanart/RPDB poster issues.
- Deploying, updating, or bumping versions.

## Architecture

Everything runs in **one asyncio event loop** in one process. No separate web worker — the Telegram bot and the uvicorn server are coroutines in the same process, sharing module-level singletons.

- `Backend/__init__.py` — shared state: `db = Database()` singleton, `__version__`, `timezone = pytz.timezone("Asia/Kolkata")`, `StartTime`, and mutable globals `USE_DEFAULT_ID`, `MANUAL_SESSION`.
- `Backend/config.py` — reads `config.env` (via load_dotenv) into class `Telegram` (`API_ID`, `API_HASH`, `BOT_TOKEN`, `DATABASE`, `PORT`, `OWNER_ID`, optional fallbacks). Env is a **first-boot seed only**; runtime settings live in MongoDB via `SettingsManager`.
- `Backend/logger.py` — `LOGGER` used everywhere (`from Backend.logger import LOGGER`); writes `log.txt` in the working directory.

### Boot order (`Backend/__main__.start_services()`)

1. `db.connect()` (all `DATABASE` URIs; DB name hardcoded `dbFyvio`)
2. `SettingsManager.initialize(db)` → adds `SessionMiddleware`
3. `scan_manager.load(db)`; bind `dbcheck_manager` / `duplicate_manager`
4. `db.reload_extra_databases(...)`
5. `StreamBot.start()` (loads `Backend/pyrofork/plugins`)
6. Userbot start (from encrypted stored session via `session_auth.py`, if present)
7. `initialize_clients()` (multi-token bots; main bot = index 0)
8. `warm_streamers()` (create a `ByteStreamer` for each initialized client)
9. `setup_bot_commands()` → `/start`, `/set`
10. Background tasks: in-process uvicorn `server.serve()`, `ping()`, `DeadLinkChecker` (24 h)

Separately, `main.py`'s FastAPI `startup` hook spawns `decay_client_failures()` and `version_check_loop()` — those are not in `start_services()`.
11. `subscription_task_manager.sync()` → `idle()`

`stop_services()` cancels background tasks, stops `StreamBot`/`Userbot`, disconnects DB. Uvicorn config: `uvicorn.Config(app, host="0.0.0.0", port=Telegram.PORT, loop="uvloop", http="httptools")`.

## Module map

### `Backend/fastapi/`

| File | Responsibility |
|---|---|
| `main.py` | Creates `app`; mounts `/static`, Jinja templates; registers routers; ~120 `/api/*` admin endpoints guarded by `require_auth`; global 401 handler (JSON for API paths, 302 → `/login` for pages); serves theme-aware PWA manifest and icon. |
| `themes.py` | Three color themes (`paper`, `ink`, `night`); `get_theme()`, `get_all_themes()`, `theme_key(request)`; default `paper`. |
| `routes/stremio_routes.py` | The addon (`/stremio`): manifest, catalog, meta, subtitles, streams, and token-scoped Nuvio collections. Supports TMDB IDs and external metadata fallback for uncached metadata; applies global and per-token catalog hiding, per-token media visibility, poster providers, MediaFlow proxy URL building, and `addon_version` expiry-epoch tags. |
| `routes/stream_routes.py` | `/dl/{token}/{id}/{name}` (GET/HEAD), `/sub/...`, `/thumb/{id}`, `/stream/stats`. Range parsing, best-client selection, 206 headers. |
| `routes/cf_routes.py` | Cloudflare Worker endpoints (`/api/cf/config`, `/api/cf/usage`) — HMAC-signed via `verify_worker_request`; hands the Worker bot tokens + userbot session and ingests usage/live-stream reports. |
| `routes/api_routes.py` | Admin API: media CRUD, tokens, subscriptions, requests, catalogs, settings, scans, stats, logs, backup, restart, and global catalog ordering/hiding. |
| `routes/template_routes.py` | Jinja pages: login, dashboard, admin, media management, settings, tools, public status/request pages. `_base_context()` supplies the cookie-selected theme. Login throttles 5 failures per client IP in a rolling 15-minute window. |
| `routes/webdav_routes.py` | Read-only WebDAV under `/webdav/{token}` (PROPFIND, GET/HEAD, `/refresh`). |
| `security/credentials.py` | Admin panel auth (`require_auth`, session cookie); `sid`-based in-process revocation makes logout invalidate the session, not only clear the browser cookie. |
| `security/tokens.py` | `verify_token()` — token validity, expiry, subscription, GB limits. Dependency on `/stremio`, `/dl`, `/sub`, `/webdav`. |

### v5.3.3 web panel and theme behavior

- The redesign is primarily in the existing templates and their shared shell; keep route authorization and API behavior unchanged when editing UI. Desktop navigation is a left rail. On phones, the shell uses a four-item bottom dock (Home, Media, Catalogs, Admin) and a **More** sheet for the remaining pages. The account menu and More sheet expose the three-theme picker.
- `themes.py` owns `DEFAULT_THEME = "paper"` and the `THEMES` mapping. `theme_key(request)` chooses a supported `theme` cookie, then a supported session value, then Paper. `_base_context()` in `template_routes.py` sends `theme`, `themes`, and `current_theme` to each page; `base.html` turns theme values into CSS variables.
- Theme changes apply immediately in the browser and persist a one-year `theme` cookie (`path=/`, `SameSite=Lax`). `POST /set-theme` remains available: it accepts a theme form value, sets the same cookie for valid themes, and redirects to the Referer (or `/`). `/manifest.webmanifest` and `/pwa-icon.svg` use the selected theme and send `Cache-Control: no-cache`.
- The signed-in shell adds a command palette (`Ctrl+K` on Windows/Linux, `⌘K` on macOS) that searches pages, theme actions, common actions, and up to eight matching local media results via `/api/media/list`. Arrow keys move selection and Enter runs it. Other shortcuts: `/` focuses the current page search; `Ctrl/⌘+S` clicks a visible save action; `Shift+T` cycles themes; `?` opens the shortcut list; Escape closes dialogs/menus. `G` followed within 1.2 seconds by `H/M/C/A/P/T/R/O/S` navigates Home/Media/Catalogs/Admin/Plans/Tokens/Requests/Tools/Settings. Media-edit tabs can also register numeric shortcuts.
- The shared panel currently pulls Tailwind CSS 3.4.16 and Font Awesome 6.5.2 from CDNs, plus Space Grotesk and JetBrains Mono from Google Fonts; offline or restricted deployments may render without those remote assets.
- `Backend/fastapi/static/placeholder.svg` is the fallback poster used by the redesigned media/catalog tools when artwork is missing.

### v5.3.4 catalog visibility and mobile management

- The catalog order workspace now includes **Trending Movies** and **Trending Series**, so both daily TMDB trending shelves can be placed alongside the default and custom catalogs.
- The **Hide** checkbox in each order row is administrator-wide, not a per-token preference. The ordered entry key is `<catalog-id>::<type>` (for example `trending_movies::movie`); the bare catalog id also hides all of that catalog's types. Per-token `hidden_catalogs` settings remain separate and continue to be combined with the global list.
- Global hidden keys are persisted in the tracking `state` collection as `_id: "catalog_hidden"` with a `keys` list, through `Database.get_hidden_catalogs()` / `save_hidden_catalogs()`. `GET /api/custom-catalogs-order` includes each entry's `key` and `hidden` flag. `PUT /api/custom-catalogs-order` requires an `order` list and optionally accepts a `hidden` list; saving updates the global catalog order and, when provided, the global hidden list.
- A globally hidden catalog is omitted from addon manifests and the per-token configure catalog list; direct catalog requests return `{"metas": []}`. Hiding a whole catalog id also hides all media types; a typed key hides only that type. Do not treat this as media visibility or catalog membership: it suppresses the catalog for every addon user.
- The Auto catalogs, Order and hide, and Create catalog sections use native collapsible `<details>` on smaller screens and are opened by default at viewport widths of 1024 px and above. On widths under 1280 px, selecting a catalog scrolls the title/workspace into view. The catalog visibility pill now inserts its fixed icon markup as HTML and escapes the label text.
- The command palette displays “Searching titles…” while its debounced local-media requests are pending. It clears that indicator and title results after completion or failure, prevents the pending row from being activated, and ignores stale responses when the query changes.

### `Backend/helper/` (core modules)

| File | Responsibility |
|---|---|
| `database.py` | The monolith: `Database` class, `connect()`, `ensure_indexes()`, media insert/update/remove, catalogs, tokens, users/plans, stats. `get_custom_catalogs(slim=True)` projects/deduplicates only item media type and visibility fields for callers that do not need full item data. `get_media_details()` can resolve by IMDb, Kitsu, or TMDB ID and uses the media type to choose the movie or TV collection. DB name `dbFyvio`. |
| `settings_manager.py` | Runtime config source of truth. `Settings` snapshot + `SettingsManager.current()` (called at request time), `update()` hot-applies most changes. |
| `bounded.py` | `BoundedDict` — FIFO-bounded `dict` used by all the in-memory caches (metadata, fanart, auto-catalog, global search, analytics, stream title/thumb, ByteStreamer file-id) to stop unbounded RAM growth. |
| `encrypt.py` | Stream id codec: `encode_string()` = base62(zlib(json)), `decode_string()` reverse. **Obfuscation, not encryption** — ids are reversible. |
| `custom_dl.py` | `ByteStreamer` — Telegram download engine: chunked prefetch, multi-client parallelism, `_prewarm_sessions()`, retry rotation across available client sessions, and guarded workload release. |
| `virtual_dl.py` | Streams split files (`.001/.002`) as one continuous stream. |
| `zip_stream.py` | Pure-Python zip reader for stored (uncompressed) archives with byte-range seeking. |
| `scan_manager.py` | `ScanManager` (channel history scan, batch 200, resumable via `tracking.scan_state`), `DbCheckManager`, `DuplicateManager`. |
| `link_checker.py` | `DeadLinkChecker` — 24 h probe of all stored files, flags dead links. |
| `manual_add.py` | Manual add plumbing + caption re-tagging (`tagged_for`, `stamp_caption_by_ref`, `retag_media`, `stamp_caption_with_id`). |
| `skip_channel.py` | Metadata-fail routing. |
| `split_files.py` | Split/combined part parsing. |
| `stream_formatter.py` | AIOStreams-compatible stream name/description formatter: `PRESETS`, `parse_filename()`, `build_parse_value()`, `render()`, `format_stream()`. |
| `announcer.py`, `auto_catalog.py` | New-content announcements; TMDB-driven auto catalogs, including `Latest Releases` ordered by release date. |
| `subtitles.py`, `nfo_generator.py`, `fanart.py` | Subtitle ingestion/matching; Kodi NFO for WebDAV; fanart.tv artwork with TTL cache. |
| `global_search.py` | Userbot-powered on-demand search of `global_search_channels`. |
| `version_check.py` | Compares `__version__` against the upstream GitHub repo every 12 h. Module-level `_state` cache + `asyncio.Lock`; `check_upstream_version(force)`, `get_version_status()`, `version_check_loop()` (task started in `main.py`). |
| `session_auth.py` | In-app userbot login flow; stores session encoded in `tracking.state["user_session"]`. |
| `cf_stream.py` | Cloudflare streaming: `cf_enabled()`, `cf_stream_url()` (HMAC-signed `/dl` links, 48 h TTL), `verify_worker_request()`, `notify_worker()`/`sync_worker_soon()` (fire-and-forget Worker reload). |
| `cf_live.py` | `apply_report()` — folds Cloudflare Worker live-stream reports into `ACTIVE_STREAMS`/`RECENT_STREAMS` and bot workloads. |
| `subscription_task_manager.py` | Hourly subscription expiry/kick/reminder loop. |
| `requests_manager.py`, `analytics.py`, `health.py`, `pinger.py`, `task_manager.py`, `utils.py`, `passwords.py`, `backup.py`, `modal.py`, `exceptions.py`, `custom_filter.py`, `pyro.py` | Public requests; telemetry; health checks; self-ping; bot edit/delete helpers with Userbot fallback; token usage tracking; PBKDF2 password hashing; config backup and authenticated TeleStremio library export; Pydantic storage schemas; exceptions; owner filter; Telegram helpers. |

### `Backend/helper/metadata/`

Matching pipeline. **Priority chains: Anime → Kitsu > TVDB > TMDB > Cinemeta; Movies → TMDB > Cinemeta; Series → TVDB > Cinemeta > TMDB.**

- `parse.py` — `parse_media_name()` (PTN + GuessIt), absolute-episode extraction, combined pack detection.
- `entry.py` — `metadata(...)` orchestrator; builds `group_key`/`part_number`; `caption_with_id()`.
- `resolvers.py` — `resolve_movie()`, `resolve_series()`, anime variants with `default_id` override short-circuit.
- `common.py` — `cached_call()` + `API_SEMAPHORE(12)` over `BoundedDict` caches, fuzzy scoring thresholds (`STRONG_MATCH=0.92`, provider ~0.55–0.60), `COMBINED_SEASON = 0`, `COMBINED_EPISODE_BASE = 1000`. Also `ID_REFS`/`has_raw_ref()` (detect raw IMDb/TMDb links) and `caption_with_links()` (caption + hidden text-link URLs).
- `episode_maps.py` — anime absolute→S/E via Anime-Lists XML / anibridge JSON.
- `providers/` — `cinemeta.py` (keyless IMDb via Stremio Cinemeta), `tmdb.py`, `tvdb.py` (v4 token login), `kitsu.py`.
- `tvdb.py` `_english_translation()`: skips `isAlias` entries, short-circuits on the `isPrimary` English (`eng`/`en`) name, else first English name (v5.1.0 fix).
- `cinemeta.py` `cached_detail()` cache key is `{cinemeta_type}:{imdb_id}` (v5.2.0 fix — movie/TV entries no longer collide). `entry.py` `_resolve_id_candidate()` returns `None` early when the IMDb detail lookup yields nothing (v5.2.0 fix).

### `Backend/pyrofork/`

| File | Responsibility |
|---|---|
| `bot.py` | Client registry: `StreamBot`, `Userbot`, `multi_clients`, `work_loads`, `client_dc_map`, `client_failures`, `client_avg_mbps`. |
| `clients.py` | Multi-bot-token lifecycle, hot reload. |
| `plugins/receiver.py` | **Ingestion core**: channel file events → metadata resolution → `db.insert_media` → catalog sync + announce. Also edit/delete handlers. |
| `plugins/start.py` | `/start`: token issuance, subscription gate. |
| `plugins/subscription.py` | Purchase flow (screenshots → approver review). |
| `plugins/group_security.py` | Kicks non-subscribers joining `subscription_group_id`. |

## Key concepts

- **Streams** = `QualityDetail` entries (`quality`, `id`, `name`, `size`, optional `group_key`, `parts[]`) inside `movie.telegram[]` or `tv.seasons[].episodes[].telegram[]`. Single-file id = `encode_string({"chat_id","msg_id"})`; split id = `{"parts": [...], "zip": bool}`.
- **Catalogs** = built-in (`latest_movies`, `top_movies`, `latest_series`, `top_series`, TMDB daily `trending_movies`/`trending_series`) + per-token `custom_{id}`; per-token order/hidden in token `config`; global order in `tracking.state["catalog_order"]`; administrator-wide hidden keys in `tracking.state` document `_id="catalog_hidden"`. Global hidden catalogs are omitted from manifests/configure lists and return no metas even on direct catalog requests; a typed key hides one type, a bare id hides all types. All catalogs accept `genre`/`search`/`skip` `extra` params (search bar works for custom catalogs too). The default `latest_movies`/`latest_series` labels are “Recently Added”; the separate smart auto catalog is “Latest Releases” and is sorted by the stored release date.
- **TMDB IDs / uncached metadata:** when a TMDB API key is configured, the manifest advertises the `tmdb` id prefix. The meta route accepts `tmdb:movie:<id>`, `tmdb:tv:<id>` (also `tmdb:series:<id>`), `tmdb:<id>`, and optional season/episode suffixes. It first looks in the local database by TMDB ID; on a miss, it requests TMDB details, images, credits, and external IDs. IMDb IDs can instead fall back to Cinemeta. This fallback supplies metadata only; it does not add a title to MongoDB or create Telegram streams.
- **Tokens** = `tracking.api_tokens` docs (32-char token, expiry, GB limits, per-token `config`); embedded in every addon URL.
- **Subscriptions** = `tracking.users` + `tracking.sub_plans`; enforced in `verify_token()`, group joins, and hourly loop.
- **Ingestion**: forwarded files in `auth_channels`; bot must be admin. Movies `Title Year Quality`, TV `Title SxxExx Quality`; quality (resolution) required. Split `name.ext.NN` joined; combined packs (`E01-E04`) → season 0, episode 1000+season. Anime channels: Kitsu-first + absolute episode recovery. Manual channels attach to `Backend.MANUAL_SESSION`. After indexing, captions get stamped with IMDb/TMDb id; captions already carrying a raw IMDb/TMDb link are stamped/re-tagged via the link text (`caption_with_links`).
- **Config**: DB-backed settings seeded once from `config.env`. Hard env-only requirements: `API_ID/API_HASH/BOT_TOKEN/DATABASE/PORT/OWNER_ID`. `DATABASE` needs **≥2 URIs** (index 0 = tracking DB, rest = storage DBs).
- **Collections** (`dbFyvio`): tracking DB — `settings`, `state`, `users`, `sub_plans`, `api_tokens`, `custom_catalogs`, `subtitles`, `requests`, `announced`, `scan_state`, `user_activity`; storage DBs — `movie`, `tv`.

## Ingestion pipeline (deep dive)

**Trigger:** `plugins/receiver.py` handler on `filters.channel & (filters.document | filters.video)`.

**Flow:** skip-channel check → manual channel/session handling → subtitle routing (`is_subtitle_file`) → `_is_supported_media()` (rejects files without a resolution/quality) → `_extract_fields()` → `metadata(...)` provider resolution → `file_queue` → `process_file()` worker (serialized by `db_lock`) → `db.insert_media` → auto-catalog sync + `announce_new_media` + `auto_fulfill`.

**Filename rules:**
- Movies: `Title.Year.Quality...` — e.g. `Oppenheimer.2023.1080p.BluRay.x264.mkv`.
- TV: `Title.SxxExx.Quality...` (also `NxNN` / `"Season N"` anchors). Quality (resolution) is **required** — no resolution → skipped or routed to skip channel.
- Split files: `name.ext.001/.002…` joined into one stream via `group_key = f"{channel}:{quality}:{normalized_base}"`; `Part 01`/`CD01`/`Disc02` styles are deliberately **not** joined. Split zip archives (`.zip.001…`) join + seek-stream only if entries are **stored (uncompressed)**.
- Combined packs: `E01-E04`, `combined`, or season packs (`S03`) → stored as `season_number = 0` (`COMBINED_SEASON`), `episode_number = 1000 + season` (`COMBINED_EPISODE_BASE`), titled `"Season N Combined"`.
- Anime channels (`anime_channels`): Kitsu-first resolution, absolute-episode recovery (`One Piece - 1172.mkv`), `is_anime` flag, `episode_maps.py` absolute→S/E conversion.
- Manual channels (`manual_channels`): never auto-indexed; files attach to the active **Manual Upload Session** (`Backend.MANUAL_SESSION`, set via Tools page; `kind: "personal"|"real"`, fallback season). Personal titles use negative `tmdb_id`.
- Caption stamping: after indexing the bot edits the caption to append the IMDb/TMDb id (`caption_with_id`) so re-forwards and scans match instantly via `extract_default_id`. If the caption already carries a raw IMDb/TMDb link (`has_raw_ref`), stamping reads the id from `caption_with_links` (caption + hidden text-link URLs) and replaces the raw link (`stamp_caption_by_ref`).

**Edits/deletions:** `file_edited_handler` — a same-title (or id-less) edit renames the stored part (`rename_media_part`), an override-id edit reindexes it; `file_deleted_handler` purges DB entries for deleted messages. `replace_mode` deletes the old same-quality Telegram message on replacement; `duplicate_protection` skips exact duplicates (bot deletes the duplicate message).

**Scans:** `ScanManager` iterates channel history in batches of 200 (`SCAN_BATCH_SIZE`), persists progress in `tracking.scan_state` (`_id="scan"`), modes `scan`/`rescan`, resumable after restart. Controlled from the Tools page (`/api/admin/tools/...`).

## Configuration reference

### Env vars (`config.env`, first-boot seed only)

**Required:** `API_ID`, `API_HASH`, `BOT_TOKEN` (Telegram bot credentials), `OWNER_ID` (numeric), `DATABASE` (comma-separated Mongo URIs, **≥2** — index 0 = tracking DB, rest = storage DBs), `PORT` (default 8000).

**Optional fallbacks:** `REPLACE_MODE`, `HIDE_CATALOG`, `AUTH_CHANNEL` (CSV), `TMDB_API` (also enables TMDB IDs in the addon manifest and TMDB metadata fallback), `TVDB_API`, `BASE_URL`, `UPSTREAM_REPO` / `UPSTREAM_BRANCH`, `ADMIN_USERNAME` / `ADMIN_PASSWORD` (default `admin`/`admin`), `SUBSCRIPTION`, `SUBSCRIPTION_GROUP_ID`, `APPROVER_IDS`, `HTTP_Proxy_URL` (legacy capitalization), `SHOW_ProxyAndNonProxyBoth`, `WEBDAV_USER` / `WEBDAV_PASSWORD`.

### DB-backed settings (`SettingsManager`, editable in web Settings page)

- Media: `replace_mode`, `duplicate_protection`, `hide_catalog`
- Streaming: `default_streaming` (`bots`|`userbot` — userbot streams first, falls back to the bot pool)
- Channels: `auth_channels`, `anime_channels`, `manual_channels`, `channel_titles`, `skip_channel`, `delete_on_metadata_fail`, `announce_new_content`, `announcement_channel`, `global_search`, `global_search_channels`
- APIs: `tmdb_api`, `tvdb_api`, `base_url`
- Admin: `admin_username`, `admin_password`, `session_secret`
- Subscriptions: `subscription`, `subscription_group_id`, `approver_ids`, `payment_instructions`, `payment_qr_url`
- Proxy: `http_proxy_url`, `show_proxy_and_non_proxy_both`, `mediaflow_proxy`, `mediaflow_password`
- Cloudflare streaming: `cf_stream_url`, `cf_stream_secret`, `cf_stream_mode` (`off`|`cloudflare`|`both`; DB-only — no env seed)
- Stream formatter: `stream_formatter` (`default`|`custom`|a preset), `formatter_name`, `formatter_description` (custom templates; ≤20000 chars)
- WebDAV: `webdav_user`, `webdav_password`
- Clients/DBs: `multi_tokens`, `extra_databases`
- Posters: `better_poster_enabled`/`better_poster`, `rpdb_enabled`/`rpdb_api_key`, `spatial_poster_enabled`/`spatial_poster`, `fanart_enabled`/`fanart_api_key`/`fanart_shuffle`/`fanart_shuffle_interval`/`fanart_low_res_poster`
- Upstream: `upstream_repo`, `upstream_branch`

**Validation rules:** channel fields must be `-100…` ids; a channel may belong to only one role (AUTH∩ANIME overlap allowed); only one poster provider at a time (BetterPoster/RPDB/SpatialPosters/fanart); `better_poster`/`spatial_poster` must contain `{imdb_id}`; `spatial_poster_enabled` requires `spatial_poster`; fanart requires a key; `stream_formatter` must be `default`|`custom`|a known preset; `default_streaming` must be `bots`|`userbot`.

## Data model & collections

- **Tracking DB:** `settings` (`_id="app_settings"`), `state` (`db_index`, `catalog_order`, `catalog_hidden` with global hidden keys, `auto_catalog_settings`, `user_session`), `users` (subscription_status, subscription_expiry, plan fields), `sub_plans`, `api_tokens`, `custom_catalogs`, `subtitles`, `requests`, `announced`, `scan_state`, `user_activity`.
- **Storage DBs:** `movie` (indexed on `tmdb_id`, `imdb_id`, `kitsu_id`; `telegram[]` qualities), `tv` (embeds `seasons[].episodes[].telegram[]`). Auto-catalog membership items may include `release_date` for premiere ordering. Pydantic schemas in `modal.py`: `QualityPart`, `QualityDetail`, `Episode`, `Season`, `TVShowSchema`, `MovieSchema`.
- **Custom catalog slimming (upstream e6024b8):** `get_custom_catalogs(slim=True)` aggregates catalogs and replaces each `items` array with a `$setUnion` of distinct `{media_type, visibility, allowed_tokens}` projections. Manifest, Nuvio collections, addon catalog listing and manual-add/catalog-order helpers use this when they need membership/visibility but not full item metadata. Use the full default result when a caller needs catalog item IDs or metadata.
- **Custom catalog field projection (upstream 3326dec):** `get_custom_catalogs(item_fields=...)` returns `item_count` and can project only requested item keys; `item_fields=()` omits the item array. The admin catalog listing uses this to avoid transferring every item just to display counts or determine whether a selected title is present. The UI reads `item_count`, not `len(items)`.
- **TMDB metadata lookup (upstream 9f0d68a):** `get_media_details()` accepts optional `tmdb_id` and `media_type`; TMDB movie IDs query `movie`, while series/TV IDs query `tv`. The Stremio meta handler supplies this lookup and, if no local document exists, calls `_external_meta_for_id()`. TMDB fallback builds title, overview, year, poster, backdrop, logo, genres, rating, cast, runtime, and placeholder episode metadata for TV; an IMDb/Cinemeta fallback is available for IMDb IDs. Fallback metadata is not a library record and does not imply a playable local stream.
- **TeleStremio library export (upstream 3326dec):** authenticated `GET /api/admin/backup/library` streams a gzip-compressed JSON catalog (`format: "telestremio-catalog"`, version 2, source `telegram-stremio`) containing `movie`, `tv`, and configured `channels`. Library documents omit Mongo `_id`; stream records have their encoded Telegram `id` replaced with `id: ""` and normalized `parts` references. Single-part IDs are decoded to chat/message references; split-file parts are preserved. Channel entries include the configured channel reference/title and anime flag. The export is intended for TeleStremio import and does not contain app settings, admin passwords, session secrets, API tokens, or subscription plans. Treat the download as sensitive because it includes library metadata and Telegram channel/message references; the endpoint requires admin auth.
- **Token doc shape:** `token` (32 chars), `user_id`, `is_admin`, `subscription_exempt`, `expires_at`, `limits {daily_limit_gb, monthly_limit_gb}`, `usage {total_bytes, daily, monthly}` (rolled by date-string compare), free-form `config` (quality sort, hidden catalogs, catalog order).
- **Id encoding** (`encrypt.py`): stream/subtitle/thumb ids = base62(zlib(json)) — compaction/obfuscation, **reversible**, not security. Telegram channel ids stored normalized positive; prefixed `-100` for API calls.

## URL surface

- **Addon:** `/stremio/{token}/manifest.json`, `/stremio/{token}/catalog/{movie|series}/{id}[/{extra}].json`, `/stremio/{token}/meta/{movie|series}/{id}.json`, `/stremio/{token}/subtitles/{type}/{id}.json`
- **Nuvio collections:** token-guarded routes are `/stremio/{token}/nuvio-collection.json` (master), `/nuvio-discover-collection.json`, `/nuvio-ott-collection.json`, `/nuvio-franchises-collection.json` (legacy alias `/nuvio-explore-collection.json`), and `/nuvio-spotlight-collection.json`. Install the token's addon manifest in Nuvio before importing a collection, because collection sources refer to that addon's catalog IDs. Each modular URL returns only its selected collection.
- **Streams:** `GET|HEAD /dl/{token}/{id}/{name}`, `/sub/{token}/{id}/{name}`, `/thumb/{id}` (public), `/stream/stats`
- **WebDAV:** `/webdav/{token}/...` (read-only), `POST /webdav/{token}/refresh`
- **Admin API:** `/api/media/*` (incl. `POST /api/media/tag` — re-tag a title's files with IMDb deeplinks), `/api/tokens*`, `/api/admin/*` (settings, stats, health, logs, backup, restart, subscriptions, access, requests, stream-analytics, tools — scan/dbcheck/duplicates/dead-links/manual-session/bot-admin), `/api/custom-catalogs*`, `/api/system/*`. Backup includes `GET /api/admin/backup/export` (config JSON), `POST /api/admin/backup/import` (restore config), and `GET /api/admin/backup/library` (gzip TeleStremio catalog export).
- **Pages:** `/` (dashboard), `/login`, `/logout`, `/admin/dashboard`, `/admin/settings`, `/admin/tools`, `/media/manage`, `/media/edit`, `/catalogs`, `/status` (public), `/stremio` (guide), `/request` (public), `/open/{stremio|nuvio}/{type}/{id}` (deep links), PWA: `/manifest.webmanifest`, `/sw.js`, `/pwa-icon.svg`

## Data flows

1. **Ingestion:** forward file → receiver handler → metadata resolution → queue → `insert_media` → announce + catalog sync + request auto-fulfill.
2. **Streaming:** Stremio fetches manifest/catalog/meta → stream id → `GET /dl/{token}/{id}/{name}` → `verify_token()` (expiry, subscription, GB limits) → decode id → `select_best_client()` (least workload + 3× failure penalty, DC preference, round-robin) → `ByteStreamer.prefetch_stream()` (1 MB chunks, prefetch, multi-client parallel) → 206 partial responses.
3. **Token auth:** `verify_token()` checks `api_tokens`, owner-linked `is_admin`, `expires_at`, subscription expiry, then daily/monthly GB (`limit_exceeded`).
4. **Settings hot-reload:** `SettingsManager.update()` validates → `_sync_channel_titles()` → reload extra DBs → save → `_reinit_dependent()` (restarts multi-token clients, userbot, global search flag).
5. **Restart/update:** web Restart → `uv run update.py` (destructive self-update) → `os.execl(uv, "run", "-m", "Backend")`.

## Run & deploy

```bash
uv run -m Backend          # entry point (Backend/__main__.py) — must run from repo root
uv run update.py           # self-update (destructive: resets .git, pulls upstream branch)
python bump-version.py [patch|minor|major]  # bumps pyproject.toml + Backend/__init__.py
```

- Web server = uvicorn started in-process on `0.0.0.0:PORT` (default 8000). Docker CMD: `bash start.sh` → `uv run update.py && uv run -m Backend`.
- `docker-compose.yaml`: mounts `./config.env:/app/config.env`, port `8000:8000`.
- `Caddyfile`: reverse proxy example (`reverse_proxy localhost:8000`) — gitignored.
- `config.env` contains real credentials — never commit (gitignored).
- Local dev: copy `sample_config.env` → `config.env`, `uv sync`, then `uv run -m Backend`.
- `update.py`: wipes `log.txt`, deletes `.git`, re-inits with hardcoded identity (`weebzone` / `doc.adhikari@gmail.com`), `git fetch origin && git reset --hard origin/{branch}`. Upstream resolution: DB `settings.upstream_repo/branch` > env > default `https://github.com/weebzone/Telegram-Stremio` / `master`. Runs on every `start.sh` boot and on web Restart.
- `bump-version.py`: `python bump-version.py [patch|minor|major]` (default patch) — reads the project name via `tomllib`, regex-replaces semver in `pyproject.toml`, `Backend/__init__.py`, and the local `[[package]]` entry (`source = { virtual = "." }`) in `uv.lock` (PEP 503-normalized name lookup).
- Heroku: `heroku.yml` → `build.docker.web = Dockerfile`.
- Upstream's static GitHub Pages site lives in `docs/`: `index.html` is the product showcase and `setup.html` is the step-by-step installation guide; shared CSS/JS and screenshots are under `docs/assets/` and `docs/shots/`. `docs/video/` contains the ~60-second MP4 demo, poster image, and animated GIF used in the README; `docs/press/` contains general and self-hosted JPG press-kit screenshots. The site is independent of the FastAPI app and requires no backend route. The guide covers VPS/Docker, Heroku, Koyeb, Render, orkestr, credentials, first login, Stremio/Nuvio setup, backups, and troubleshooting. README links to the upstream pages at `weebzone.github.io/Telegram-Stremio`; a fork needs its own Pages configuration if it wants to publish a fork-specific site.
- The README now links to TeleStremio Android and desktop releases. TeleStremio is a separate app; the authenticated `/api/admin/backup/library` gzip export described above is the migration path for transferring the indexed library.

## Conventions & gotchas

- Use `LOGGER` from `Backend.logger` for logging. Async-first (Motor/httpx/pyrogram). Blocking CPU work offloaded to `ThreadPoolExecutor` (see `encrypt.py`).
- `#-----` section separators, imperative docstrings, singleton globals (`db`, `file_queue`, `ACTIVE_STREAMS`).
- Must run from repo root — relative paths (`Backend/fastapi/templates`, `log.txt`, `.restartmsg`) depend on cwd.
- Stream ids are reversible (base62+zlib, not secure); Telegram channel ids normalized positive in DB, prefixed `-100` for API calls.
- Personal/manual titles use negative `tmdb_id`. Combined packs in season 0 sort as "Specials".
- `update.py` is destructive (`shutil.rmtree(".git")`, hard reset, hardcoded git identity) — careful when testing.
- Restart = re-exec `uv run -m Backend` after the updater.
- Fanart / BetterPoster / RPDB / SpatialPosters are mutually exclusive poster providers (validated in settings API).
- Minor lint noise: duplicated `import asyncio` at top of `api_routes.py`.
- Local divergence from upstream: donation code and promotions are removed while retaining the rest of the upstream release. In `stremio_routes.py`, `_donation()` is absent, empty matches return `{"streams": []}`, and no donation stream is appended. Both signed-in support links are removed from `base.html` (account menu and mobile More sheet). The donation badge/section is omitted from `README.md`; the support links are omitted from the footers in `docs/index.html` and `docs/setup.html`. Check all these files after every upstream sync because upstream may reintroduce the links without a conflict. The older stream-entry implementation appeared as an obfuscated `__x7()` through 5.0.4, as a prepended `_donation()` in 5.0.5–5.0.6, and as an appended `_donation()` from 5.1.0 onward.

## Troubleshooting playbook

- **No streams / empty stream list:** check token `config.quality_filter` (falls back to all if it would hide everything), verify storage DB `movie`/`tv` docs, run dead-link check, check scan state.
- **Streams won't play:** verify token GB limits (`limits`, `usage`), `expires_at`/subscription expiry, MediaFlow/proxy URL config, client failures (`client_failures`).
- **Bot not indexing files:** bot must be admin in each `auth_channel`; file must include a resolution; check skip-channel routing (`delete_on_metadata_fail`); watch `log.txt`.
- **Global search unavailable:** requires a connected Userbot session (Settings → Session login; stored encoded in `tracking.state["user_session"]`); `global_search` is rejected when no userbot.
- **Nuvio blank rows / missing platform titles:** install the manifest URL in Nuvio before importing a collection. If OTT or regional folders are empty, enable the relevant built-in Auto Catalogs in the Catalogs page and run **Sync Catalogs**. Collection rows reference the server addon's catalog IDs; external metadata fallback does not populate local catalogs or streams.
- **Nuvio TMDB title details missing:** uncached `tmdb:*` IDs need a configured TMDB API key. For IMDb (`tt...`) IDs, the metadata handler can use Cinemeta; a TMDB fallback is not used to turn metadata-only titles into playable Telegram files.
- **Admin login throttled:** login failures are counted in a bounded in-memory cache per `client_ip_from(request)` IP. Five failures within 15 minutes return HTTP 429; a successful login clears that IP's count. The limiter and revoked session-ID cache are process-local and reset on restart.
- **TeleStremio library migration:** use Settings → Backup → **Library for TeleStremio** to download the authenticated gzip catalog, then import it in TeleStremio via Settings → Backup → Import file. It replaces the target app's library; it is not the Telegram-Stremio settings backup. Keep the file private because it contains Telegram channel/message references.
- **Wrong metadata:** use caption stamping/override id or the media edit page; check TMDB/TVDB keys; thresholds in `common.py`.
- **Server won't start:** run from repo root (relative paths), `DATABASE` needs ≥2 URIs, port free, `uv` installed.
- **Forgot admin password:** passwords are hashed and cannot be recovered, only reset. The upstream README links an external reset site (`https://telegram-stremio-reset.vercel.app/`) that takes the **tracking DB URI** (first URI in `DATABASE`, not a storage URI), resets the password to `admin`, and may need the DB name `dbFyvio` set in advanced options. A full app-process restart is required because settings are cached in memory; then log in and change the default password. This submits a database credential to a third party: use the tool only if you trust its operator, use a narrowly scoped/temporary credential if possible, and rotate/revoke that credential afterward. The README says the tool is stateless; that claim is not independently verified here.
- **Update/restart issues:** `update.py` is destructive (resets `.git`) — avoid on forks; restart re-execs `uv run -m Backend` after the updater.

## Common task procedures

**Add an admin API endpoint:** add a route in `Backend/fastapi/routes/api_routes.py` guarded with `Depends(require_auth)`; call DB methods on `Backend.db`; validate settings through `SettingsManager.update()` when needed.

**Add a Stremio addon feature:** edit `routes/stremio_routes.py` (manifest/catalog/meta); respect per-token visibility helpers (`_visibility_query`, `_token_can_view`) and `verify_token`.

**Change configuration:** add the property to `Settings` in `settings_manager.py`, seed from env in `_seed_from_env()`, validate in `update()`, then expose in the Settings page (`templates/settings.html` + settings API handler).

**Add a metadata provider:** implement search/detail/build-payload in `Backend/helper/metadata/providers/`, register in `resolvers.py` priority chains, mind `cached_call()` + semaphore in `common.py`.

**Debug streaming:** trace `stream_routes.stream_handler` → `custom_dl.ByteStreamer` (or `virtual_dl` / `zip_stream` / global variants); check client selection in `select_best_client()` and token limits in `security/tokens.py`.

---

# Exhaustive reference (appendix)

## Module internals

### `subtitles.py`
- Extensions: `SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")`; `is_subtitle_file()`, `subtitle_ext()`.
- `_LANGUAGES`: **48** entries as `(ISO 639-2/T code, label, aliases)` — e.g. `eng/English`, `hin/Hindi`, `tam/Tamil`, `bho/Bhojpuri` (no 639-1). Built at import: `_LANG_BY_TOKEN`, `_LANG_WORDS`.
- `detect_language()`: tokenizes lowercase, strips extension tokens and `_IGNORE_TOKENS = {"forced","sdh","cc","full","default","hearing","impaired","dubbed","dub","sub","subs","subtitle","subtitles"}`; scans tokens from the end (full names ≥4 chars → 3-letter codes → 2-letter codes); fallback `("und", "Unknown")`.
- `ingest_subtitle()`: identifies title (`extract_default_id` + `parse_media_name`; TV if season+episode), upserts into `tracking.subtitles` (`imdb_id, media_type, season, episode, lang_code, lang_label, name, chat_id, msg_id, encoded, source:"auto"`).
- `stremio_subtitle_entries()`: entries `{"id": "tg-{msg_id}", "url": "{base}/sub/{token}/{encoded}/subtitle{ext}", "lang": label}`; duplicate labels get numeric suffix.
- Manual: `list_languages()`, `resolve_subtitle_message()`, `manual_ingest_subtitle()`, `list_title_subtitles()`, `get_subtitles_for()`, `remove_subtitle()`.

### `requests_manager.py`
- `STATUSES = ("pending","uploaded","denied","banned")`; `_IMDB_RE = re.compile(r"(tt\d{7,10})")`; IP hashed via sha256 `[:16]`.
- Public: `search_titles`, `media_exists`, `submit_request`, `list_requests`, `popular_pending(limit=12)`, `set_status`, `delete_request`, `auto_fulfill`.
- `search_titles` chain: Cinemeta first, TMDB fallback (imdb → `_cinemeta_id_search`/`_tmdb_imdb_search`; name → `_cinemeta_name_search` limit 8 → `_tmdb_name_search`); dedupe by `(media_type, imdb_id or tmdb:{id})`, cap 15.
- `submit_request` reasons: `created|added|already_available|reopened|banned|invalid`. `auto_fulfill` marks matching pending → `uploaded`.

### `task_manager.py`
- `DELETE_BATCH_SIZE = 10`; `_FALLBACK_WORTHY = (ChatAdminRequired, ChannelPrivate, MessageDeleteForbidden, MessageAuthorRequired, UserNotParticipant, RPCError)`; `_SESSION_DEAD = (AuthKeyUnregistered, SessionRevoked)`.
- Functions: `edit_message`, `delete_message`, `delete_messages_batch`, `_userbot_edit`, `_delete_chunk`.
- Retry: `FloodWait` → sleep then retry once; `PeerIdInvalid` → `_resolve_peer` then retry once; other fallback-worthy errors → Userbot. Userbot disabled for the run after a dead-session error.

### `backup.py`
- Excludes `admin_password` and `session_secret`. Backs up settings + `custom_catalogs`, `sub_plans`, `api_tokens`.
- `_jsonify` (ObjectId→str, datetime→iso), `_revive` (24-char `_id`→ObjectId, ISO strings→datetime). `import_config` rejects payloads whose `app != "telegram-stremio"`; collections are **replaced** wholesale.
- `export_library()` (v5.3.2) streams a gzip JSON TeleStremio catalog, version 2. It walks each `storage_*` DB's movie and TV collections one document at a time; removes `_id`/`updated_on`, converts `updated_on` to millisecond `updated_on` and `added_on`, and normalizes quality entries to `id: ""`, `parts`, and `size_bytes`. Legacy single-file IDs are decoded to Telegram `chat_id`/`msg_id`; invalid IDs skip that quality. Exported channel records are derived from configured auth channels and include channel ref, chat ID, title, and anime flag. This is separate from the settings/config backup and restore flow.

### `fanart.py`
- `fanart_artwork(imdb_id, tmdb_id, media_type) → {"poster","logo","background"}`. Endpoints: `/v3/movies/{id}`, `/v3/tv/{id}`, TMDB `/3/tv/{id}/external_ids`.
- Caches: `_CACHE_TTL = 6h`, `_ERROR_TTL = 300`, `_CACHE_MAX = 4096`, `_fetch_sem = Semaphore(10)`, in-flight dedupe dict.
- Shuffle: English-preferred; non-shuffle → max `likes`; shuffle with interval → time-bucket seeded `random` (`interval` minutes, default 5). Poster/logo use `/preview/`; backgrounds proxied via `wsrv.nl`.

### `analytics.py` / `health.py` / `pinger.py` / `passwords.py` / `encrypt.py`
- Analytics: `client_ip_from` (tries `cf-connecting-ip`, `x-real-ip`, `x-forwarded-for`, client host), `parse_app`/`parse_device` (UA maps), `lookup_ip` via `http://ip-api.com/json/{ip}`, `record_client`, `record_stream_start`, `get_activity_overview(page, per_page=5)`. `_IP_TTL = 6h`, `ONLINE_WINDOW = 120`.
- Health: checks `databases`, `bots`, `tmdb`, `base_url`; `_FREE_TIER_BYTES = 512 MB`; TTLs 30/300/60 s; status `critical|warning|ok`.
- Pinger: `sleep_time = 1200` s, GETs `{base_url}/status` (public status page) with `allow_redirects=True`, aiohttp timeout 15 s; skips with a warning when `base_url` is unset, warns on non-2xx/3xx responses.
- Passwords: `pbkdf2_sha256$200000$salt$digest` (16-byte salt), `hmac.compare_digest`, legacy plaintext fallback.
- Encrypt: `BASE62_ALPHABET` (digits, lowercase, uppercase), zlib `Z_BEST_COMPRESSION`, module-level `ThreadPoolExecutor`.

### `custom_dl.py` (ByteStreamer)
- `CHUNK_SIZE = 1 MB`, `CLEAN_INTERVAL = 30 min`, `TEST_CHUNK_SIZE = 100 MB`, `STALE_STREAM_IDLE = 180`.
- `ACTIVE_STREAMS` dict + `RECENT_STREAMS` deque(maxlen=20); stale cleaner every 30 s decrements `work_loads` only when `load_held` is still true. Normal completion and stale cleanup clear this guard so a stream releases its workload slot at most once.
- Prefetch queue maxsize = prefetch; parallel chunk fetches; fetch timeout 15 s; retries `<3` / flood `<5` (backoff `min(0.5*2**(tries-1), 10)`); FILE_REFERENCE errors refresh location; consumer stalls after 90 s.
- Registry entry (local `stream_entry` alias) fields: `start_ts`, `last_ts` (falls back to `start_ts`), `total_bytes`, `recent_measurements` deque(maxlen=3) via `setdefault`, `instant_mbps`/`avg_mbps`/`peak_mbps`, `status`, `chunk_size`; finished entry logged via `db.log_stream_stats(stream_entry)` and popped into `RECENT_STREAMS` after a 3 s delay.
- `_prewarm_sessions()`: DCs `[1, 2, 4, 5]`, `no_updates`, up to 6 authorization retries; reuse the current DC authorization key and avoid replacing an existing media session. Failed chunk attempts rotate to the next available media session/client. Speed test: ping `limit=4096`, chunk `512 KB`, `max_concurrent_chunks = 8`; empty-chunk handling must not call `queue.task_done()` twice.

### `bot.py` / `clients.py`
- `StreamBot`: `sleep_threshold=20, workers=6, max_concurrent_transmissions=10`, plugins root `Backend/pyrofork/plugins`. `USERBOT_CLIENT_INDEX = -1`; userbot `device_model='Telegram Stremio', no_updates=True, in_memory=True`.
- Registries: `multi_clients`, `work_loads`, `client_dc_map`, `client_failures`, `client_avg_mbps`.
- `TokenParser.parse_from_settings()`: 1-based ids from `multi_tokens` (0 = main bot); `reload_multi_token_clients()` diffs and hot-reloads.

### `global_search.py`
- Limits: `MAX_RESULTS = 50`, `SEARCH_COOLDOWN_SECONDS = 5`, `MAX_CONCURRENT_SEARCHES = 3`, `MAX_CONCURRENT_CHANNELS = 5`, `MIN_TITLE_SCORE = 0.7`, `RESULT_CACHE_SECONDS = 60`, `SPLIT_SCAN_WINDOW = 20`.
- Query variants: `"Title S02E03"`, `"Title 101"` (absolute), `"Title {year}"`, bare title, `Title E101`, symbol-stripped. Split parts gathered within ±20 messages of the seed.
- Global search no longer treats loose `Part`/`CD`/`Disc`/`Pt` suffixes as split files. Split search results require a complete contiguous part sequence starting at 1; video messages labeled with noncanonical part suffixes are skipped rather than emitted as incomplete streams.

### `scan_manager.py`
- `SCAN_BATCH_SIZE = 200`, `SCAN_MAX_EMPTY_BATCHES = 10`, `SCAN_MAX_ID_CAP = 1_000_000`, `SCAN_BATCH_DELAY = 0.5`, `SCAN_PROCESS_CONCURRENCY = 8`. DB check: concurrency 5, page size 100.
- `tracking.scan_state` `_id="scan"` fields: `status, mode, selected_channels, pending, current_channel, current_id, cursors, counters{...}, started_at, updated_at, finished_at, error`.

### `metadata/common.py` thresholds
- `CINEMETA_THRESHOLD = 0.60`, `TMDB/TVDB/KITSU_THRESHOLD = 0.55`, `STRONG_MATCH = 0.92`, `ALT_TITLE_LOOKUPS = 5`, `API_SEMAPHORE = Semaphore(12)`, `COMBINED_SEASON = 0`, `COMBINED_EPISODE_BASE = 1000`.
- Caches (v5.3.0 → `BoundedDict`, FIFO eviction): `IMDB_CACHE`/`TMDB_SEARCH_CACHE`/`TMDB_DETAILS_CACHE`/`ALT_TITLES_CACHE`/`TVDB_CACHE`/`KITSU_CACHE` maxlen 300, `EPISODE_CACHE` 500. `ID_REFS` regex catches `imdb.com/title/tt…`, `themoviedb.org/(movie|tv)/…`, and bare `tt\d{7,10}`.

### `auto_catalog.py`
- `AUTO_CATALOG_REGION = "IN"`, sync concurrency 5 (instant 3). `AUTO_CATALOG_DEFINITIONS` (26): language (bollywood, hollywood, anime, kdrama, bengali, south_indian, tamil, telugu, malayalam, kannada, japanese, korean), smart (`top_rated`, `latest_releases`), OTT (netflix, prime_video, hotstar, apple_tv, hulu, hbo, jiocinema, zee5, sonyliv, mx_player, crunchyroll — 12).
- `Latest Releases` is the smart catalog for recent movie/TV premiere dates. `release_date()` prefers release/air dates not later than today; `catalog_sort_key()` sorts by `release_date`, then falls back to `updated_on`/`added_at`. Catalog item records carry `release_date`; a new settings marker migrates the former `recently_added` key and old custom-catalog auto key/name, and enables `latest_releases` by default. This is separate from the built-in `latest_movies`/`latest_series` shelves, displayed as “Recently Added”.

### `webdav_fs.py`
- `VNode`: `path, name, is_dir, size, mtime, content_type, kind` (`movie_video|movie_nfo|show_nfo|season_nfo|episode_video|episode_nfo|poster`), `stream_id, stream_name, parts, nfo_body, media_type, tmdb_id, db_index, season_number, episode_number, children`.
- Tree: `/Movies/Title (Year)/…` + `/TV Shows/Show (Year)/Season 01/Show S01E01 - Episode Title - 1080p.mkv`; every quality = separate file.
- `pick_best_quality`: order `2160p/4k=0, 1440p=1, 1080p=2, 720p=3, 480p=4, 360p=5`, unknown 50. Singleton `fs` with `cache_ttl=300`.

### `virtual_dl.py` / `zip_stream.py`
- `resolve_virtual_parts()` computes per-part `file_id`, sizes, and `cum_start` cumulative offsets. `virtual_stream_generator()` walks overlapping parts, computes local offsets, chains `prefetch_stream` per part with `stream_id = "{id}-p{index}"`, aborts on client disconnect.
- `zip_stream`: `STORED = 0` only streamable; `parse_local_header()` (`PK\x03\x04`, `data_offset = 30 + name_len + extra_len`, `has_descriptor = flag & 0x08`); `_parse_central_directory()` Zip64-aware (EOCD `PK\x05\x06`, locator `PK\x06\x07`, record `PK\x01\x02`); `resolve_zip_entry()` reads local head (64 KB) then falls back to central directory; serves inner-file byte ranges via concatenated archive offsets.

### `stream_formatter.py`
- AIOStreams-compatible stream name/description formatter (same template syntax, fields, and presets). `PRESETS` keys (7): `torrentio`, `torbox`, `gdrive`, `lightgdrive`, `minimalisticgdrive`, `prism`, `tamtaro`.
- `parse_filename(filename)` → per-file parse tree (`stream{resolution, quality, encode, visualTags, audioTags, languages, size, subtitles}`, `file`, `addon`, `service`, `meta`); driven by `PTN` plus AIOStreams regex tables.
- `build_parse_value(filename, size_bytes, *, addon_name, is_split, parts, channel, meta)` builds the value tree; `render(template, pv)` renders a template against it (JS-semantics helpers: `_truthy`, `format_bytes`, `format_duration`, `normalise_language`, etc.).
- `format_stream(settings, filename, size_bytes, **kw)`: returns `None` for `stream_formatter == "default"` (built-in naming), else renders `PRESETS[preset]` (or the custom `formatter_name`/`formatter_description`) into `(name, description)`; swallows errors with a warning.
- Settings page preview: `POST /api/admin/settings/formatter-preview` renders a preset/custom template against a synthetic `video.mkv` / 2.5 GB value.

## Stremio routes internals

- Manifest: `ADDON_NAME = "Telegram"`, `id: telegram.media.{token[:8]}`, `idPrefixes: ["tt","tg","kitsu"]` plus `"tmdb"` only when `tmdb_api_key()` is configured, `behaviorHints: {configurable: true, configurationRequired: false}`, `types: ["movie","series"]`, resources include `catalog` unless `hide_catalog`; one config entry (manifest_url). Built-in latest catalog labels are “Recently Added”. `PAGE_SIZE = 15`, 20 `GENRES`.
- Built-in catalogs: `latest_movies`, `top_movies`, `latest_series`, `top_series`, plus TMDB daily trending `trending_movies`/`trending_series`; custom = `custom_{_id}`.
- Catalog `extra` params: `genre=`, `search=`, `skip=` (URL-encoded, `/`-separated). `search=` filters any catalog including custom/auto catalogs (scoped to the catalog's visible item ids). Trending catalogs resolve `_get_tmdb_trending_ids()` (10-min cache) then `_get_db_matches_in_tmdb_order()` to preserve TMDB rank over local matches.
- Nuvio collections (upstream 9f0d68a–3326dec): `build_nuvio_collection_data()` builds **Discover**, **Streaming Platforms**, **Franchises & Universes**, and **Regional & Spotlight**. Addon sources use `telegram.media.{token[:8]}` and token-visible catalog IDs; platform IDs resolve from custom-catalog `auto_key` or names. Latest Releases is included as a Discover shelf. Final sanitization retains only valid addon catalog sources, removes folders without sources, and omits empty collections. Each modular endpoint filters to its named collection; the master endpoint returns all available collections. `/nuvio-franchises-collection.json` retains `/nuvio-explore-collection.json` as a compatibility alias. Artwork uses standardized `assets/nuvio/*.webp` plus curated artwork. In Nuvio, install the addon manifest first, then import the master or a modular URL from **Settings → Collections → Import → Import from URL**.
- TMDB ID parsing: `_parse_stremio_id()` recognizes `tmdb:movie:<id>`, `tmdb:tv:<id>` / `tmdb:series:<id>`, `tmdb:<id>`, and trailing season/episode components. For database hits, `get_meta()` uses the TMDB ID and media type. For misses, `_external_meta_for_id()` calls TMDB `details(append_to_response="images,credits,external_ids")`, chooses English logos when available, and supplies basic video entries for non-special seasons; IMDb misses use Cinemeta `get_detail()`. Failures are warned and returned as empty metadata. This is metadata fallback, not ingestion or stream generation.
- Positive external metadata results are held in a `BoundedDict(500)` for six hours, keyed by Stremio ID and media type. TV fallback episodes are limited to episodes aired through TMDB's `last_episode_to_air`; release timestamps use the season air date when present. Do not cache failures as successful metadata.
- `addon_version` = version or `f"{ADDON_VERSION}-{epoch_tag}"` where `epoch_tag = format(int(expiry.timestamp()) & 0xFFFF, "x")`; expiry note in description.
- Visibility: per-item `public|tokens|owner` + `allowed_tokens`; `_token_can_view` (owner always; `tokens` needs membership; subscription mode needs not-expired); `_not_exclusive_clause` hides `exclusive_catalog_id` docs unless `exclusive_searchable`.
- Membership cache: `_MEMBERSHIP_TTL = 60` s, max 5000 entries, fail-open.
- Kitsu ids: `kitsu:<id>:<season>:<episode>` or `kitsu:<id>:<abs_ep>`; `_parse_stremio_id` splits on `:`.
- Pseudo-streams: subscription expired → "🚫 Plan Expired"; group gate → "📢 Join Required"; `limit_exceeded` → "Limit Reached" with `url = tg://user?id={OWNER_ID}`.
- Sorting: combined → `episode_start`, `name_key`, then `get_resolution_priority` (reverse unless `quality_sort == "asc"`); else `(resolution_priority, size_bytes)`. Priority key reads `sort_name` (falls back to `name`), so formatter-renamed streams still sort by resolution. Duplicate-name suffixing (`(1)`, `(2)`, …) applies only when `stream_formatter == "default"`.
- `get_resolution_priority`: `{2160p/4k/uhd:2160, 1080p/fhd:1080, 720p/hd:720, 480p/sd:480, 360p:360}`, default 1. `stream_res_label`: `{2160:"4K", 1080:"1080p", 720:"720p", 480:"480p", 360:"360p"}`, default `"other"`.
- Poster providers: BetterPoster `https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg`; RPDB free tier or keyed `https://api.ratingposterdb.com/{key}/imdb/poster-default/{imdb_id}.jpg`; SpatialPosters `settings.spatial_poster` with `{type}`/`{imdb_id}` placeholders.
- `build_proxy_url`: plain = `base + url`; MediaFlow = `{base}/proxy/stream?d={quote(url)}&api_password=...`.
- `_global_streams_for`: Kitsu/Cinemeta title resolution, SxxExx→absolute mapping, `global_search(...)`, absolute-first results.
- Also: `GET /{token}/configure` (HTML), `GET|POST /{token}/addon-config` (quality filter values `{"480p","720p","1080p","4K"}`).

## Stream routes internals

- Dispatch order on decoded id: `global+zip → global_zip_media_streamer`; `global+parts → global_virtual_media_streamer`; `global → global_media_streamer`; `parts+zip → db_zip_media_streamer`; `parts → virtual_media_streamer`; else `media_streamer` (`chat_id = int(f"-100{decoded['chat_id']}")`).
- DB-backed streamers resolve their client via `_db_client(fetch)`: when `default_streaming == "userbot"` and a userbot streamer exists, the userbot fetches first (prefetch `(1, 1)`); on failure it warns and falls back to `select_best_client(0)` + the bot pool. Otherwise it selects the best bot as before.
- `parse_range_header`: handles `bytes=-N` and `bytes=N-`; 416 + `Content-Range: bytes */{size}` on bad ranges.
- `select_best_client(target_dc)`: score `work_loads + 3 * client_failures`; DC-preferring then round-robin tie-break. `decay_client_failures()`: every 300 s, -1 per client.
- `get_parallel_prefetch(client_count)` = `min(max(ceil(count/5), 1), 5)`.
- Headers: 206 + Content-Range; `Accept-Ranges: bytes`, `Cache-Control: public, max-age=3600`, CORS `*`, `Content-Disposition` with UTF-8 fallback.
- Stream `meta` includes `file_name` (from `_resolve_filename_mime`, computed before building headers) and `title` falls back to `file_name` when `_lookup_title` returns nothing.
- `/thumb/{id}` TTL 3600; `/stream/stats` returns `{active, recent}` with mbps/duration fields plus `file_name` (title falls back to `file_name`); `_SUBTITLE_MIME` covers all 5 subtitle exts. `_title_cache`/`_thumb_cache` are `BoundedDict` (2000/200).

## Full `/api/*` route table

**Media** — `GET /api/media/list` · `DELETE /api/media/delete` · `PUT /api/media/update` · `POST /api/media/tag` · `DELETE /api/media/delete-quality|delete-tv-quality|delete-tv-episode|delete-tv-season` · `POST /api/media/resolve-telegram` · `POST /api/media/manual-add` · `GET /api/media/manual-add/catalogs|resolve-meta` · `GET /api/media/rescan/search` · `POST /api/media/rescan/apply`

**Subtitles** — `GET /api/media/subtitles/languages` · `GET /api/media/subtitles` · `POST /api/media/subtitles/resolve|add|remove`

**Tokens/system** — `POST /api/tokens` · `PUT|DELETE /api/tokens/{token}` · `GET /api/system/workloads|stats|speedtest` · `GET /api/system/speedtest/stream` (SSE: `start/progress/result/done`)

**Access** — `GET /api/admin/access/tokens` · `DELETE /api/admin/access/tokens/{token}` · `POST /api/admin/access/users/{user_id}/assign-plan` · `PATCH /api/admin/access/tokens/{token}/link-user|lifetime` · `POST /api/admin/access/tokens/{token}/expiry` · `POST /api/admin/access/grant-lifetime` · `GET /api/admin/subscriptions/preflight` · `POST /api/admin/subscriptions/backfill-names`

**Subscriptions** — `GET|POST /api/admin/subscriptions/plans` · `PUT|DELETE /api/admin/subscriptions/plans/{plan_id}` · `GET /api/admin/subscriptions/users` · `POST /api/admin/subscriptions/users/{user_id}/manage`

**Requests** — `GET /api/request/search|popular` · `POST /api/request/submit` · `GET /api/admin/requests` · `PATCH|DELETE /api/admin/requests/{request_id}`

**Custom catalogs** — `GET|POST /api/custom-catalogs` · `PUT|DELETE /api/custom-catalogs/{catalog_id}` · `POST|GET /api/custom-catalogs/media-visibility` · `GET /api/custom-catalogs/search-media` · `POST /api/custom-catalogs/auto-sync` · `GET /api/custom-catalogs/auto-sync/status` · `GET|PUT /api/custom-catalogs-order` · `GET|PUT /api/custom-catalogs/auto-sync/settings` · `GET|POST|DELETE /api/custom-catalogs/{catalog_id}/items`

**Settings/session** — `GET|PUT /api/admin/settings` · `POST /api/admin/settings/formatter-preview` · `GET /api/admin/settings/session` · `POST .../send-code|verify-code|verify-password|disconnect|reconnect` · `DELETE /api/admin/settings/session`

**Admin/system** — `GET /api/admin/system-stats` · `POST /api/admin/clear-cache` · `GET /api/admin/dead-links` · `GET /api/admin/stream-analytics|user-activity` · `POST /api/admin/clear-analytics` · `GET /api/admin/stats|health|health/report|setup-status` · `GET /api/admin/backup/export` (config JSON) · `POST /api/admin/backup/import` (config restore) · `GET /api/admin/backup/library` (authenticated TeleStremio gzip library export) · `GET /api/admin/logs|logs/download` · `POST /api/admin/restart`

**Tools** — `GET /api/admin/tools/channels` · `GET /api/admin/tools/bot-admin/scan` · `POST /api/admin/tools/bot-admin/apply` · `GET /api/admin/tools/bot-admin/apply/status` · `GET|POST|DELETE /api/admin/tools/manual-session` · `GET /api/admin/tools/manual-session/search` · `POST /api/admin/tools/scan/start|cancel` · `GET /api/admin/tools/scan/status` · `POST /api/admin/tools/dbcheck/start|cancel` · `GET /api/admin/tools/dbcheck/status` · `POST /api/admin/tools/dead-links/purge` · `POST /api/admin/tools/duplicates/start|cancel|purge` · `GET /api/admin/tools/duplicates/status`

Note: `/api/media/details` is defined but **not registered** in `main.py`.

`restart_app_api()`: sleep 1 s → run `uv run update.py` (waited) → `os.execl(uv, "run", "-m", "Backend")`.

## Themes

- Three themes are defined in `Backend/fastapi/themes.py`: **Paper** (light; default), **Ink** (dark), and **Night** (dark). Each theme has `name`, `is_dark`, and `colors` (`primary`, `on_primary`, `background`, `card`, `card_high`, `border`, `text`, `text_secondary`, `danger`, `ok`). The old 12-theme + 3-style registry is gone; do not use `get_all_styles()` or `current_style`.
- Theme colors become CSS custom properties in `base.html`, and the same theme powers the page palette, Tailwind semantic colors, browser `theme-color`, and PWA icon/manifest. The public showcase also previews these three palettes, but its `localStorage` palette is separate from the admin panel's `theme` cookie.
- The admin panel's theme is client-side state persisted in a one-year cookie; it is not a DB-backed setting and is independent of the admin login session. To add or rename a theme, update `THEMES` and ensure its required color keys are present; the picker and command palette enumerate the mapping dynamically.

## Templates (14)

`access_manage.html` (tokens & access), `admin_dashboard.html`, `base.html` (layout), `custom_catalogs.html`, `dashboard.html`, `login.html`, `media_edit.html`, `media_management.html`, `request_public.html`, `requests_manage.html`, `settings.html`, `stremio_configure.html` (addon install), `subscriptions_manage.html`, `tools.html`.

The shared `base.html` owns the desktop rail, mobile dock/More sheet, account menu, theme picker, command palette, keyboard shortcut dialog, toast UI, and common CSS/Tailwind configuration. Keep new navigation entries in its `nav` list so both the rail and mobile More sheet stay in sync; the mobile dock intentionally shows only the first four entries. Most application screens keep their existing template filenames but were rebuilt around this shared shell. In v5.3.4 the command palette shows a pending state while title searches run; the Catalogs page also adds small-screen collapsible sections and global hide controls.

## Bot flows

- **`/start` (free mode):** only `OWNER_ID` gets a personal token + manifest link; others ignored.
- **`/start` (subscription mode):** active user → `ensure_api_token_for_user` + `addon_links_text` (manifest + master and four modular Nuvio collection URLs); expired → marked; otherwise plan keyboard `callback_data=f"plan_{id}"` ("`<days> Days - <sym><price>`"). Install the manifest in Nuvio before importing collections.
- **Purchase:** `plan_selection` (expiry extends if active, `set_pending_payment`, DMs instructions with `ForceReply`, optional QR photo) → `handle_payment_screenshot` (forwards to approvers with `approve_{uid}`/`reject_{uid}`) → `admin_review` (approve: token + invite link for group; reject: notice; captions updated "✅ Approved by …"/"❌ Rejected by …"). `/status` reports remaining time.
- **Group security:** on `chat_member_updated` in `subscription_group_id` → ban+unban kick for inactive non-owner/non-approver joiners + DM.
- **Ingestion gate:** files from channels outside `auth_channels` are silently ignored (v5.3.0 dropped the "Channel is not in AUTH_CHANNEL" reply).
- **Caption edits:** `edit_message`/`_userbot_edit` use `parse_mode=ParseMode.HTML`.
- **`clients.py`:** `multi_clients[0] = StreamBot`; extra tokens 1-based; extras created with `sleep_threshold=100, no_updates=True, in_memory=True`.

## `Settings` class — all 52 properties

- **Booleans (15):** `replace_mode`, `duplicate_protection`, `hide_catalog`, `subscription`, `show_proxy_and_non_proxy_both`, `mediaflow_proxy`, `global_search`, `announce_new_content`, `delete_on_metadata_fail`, `better_poster_enabled`, `rpdb_enabled`, `spatial_poster_enabled`, `fanart_enabled`, `fanart_shuffle`, `fanart_low_res_poster`
- **Strings (27):** `announcement_channel`, `skip_channel`, `tmdb_api`, `tvdb_api`, `base_url`, `upstream_repo`, `upstream_branch`, `admin_username`, `admin_password`, `session_secret`, `http_proxy_url`, `cf_stream_url`, `cf_stream_secret`, `cf_stream_mode`, `default_streaming`, `stream_formatter`, `formatter_name`, `formatter_description`, `mediaflow_password`, `webdav_user`, `webdav_password`, `payment_instructions`, `payment_qr_url`, `better_poster`, `rpdb_api_key`, `spatial_poster`, `fanart_api_key`
- **Lists (8):** `global_search_channels`, `anime_channels`, `manual_channels`, `channel_titles`, `auth_channels`, `approver_ids`, `multi_tokens`, `extra_databases`
- **Integers (2):** `subscription_group_id`, `fanart_shuffle_interval`

`SettingsManager`: `initialize`, `reload`, `current`, `update`, `_reinit_dependent`. Validation: channel ids must match `-100` + digits (len ≥ 8); channel appears in only one role (only AUTH∩ANIME overlap allowed); only one poster provider; `better_poster`/`spatial_poster` must contain `{imdb_id}`; `spatial_poster_enabled` requires `spatial_poster`; `stream_formatter` ∈ `default`|`custom`|presets; `default_streaming` ∈ `bots`|`userbot`; `fanart_enabled` requires key; `extra_databases` must start with `mongodb://` or `mongodb+srv://`.

## Database method groups

- Connection/storage: `connect`, `disconnect`, `update_current_db_index`, `connect_storage_db`, `disconnect_storage_db`, `get_database_list`, `reload_extra_databases`, `ensure_indexes`
- Settings: `get_settings`, `save_settings`
- Global catalog presentation: `get_catalog_order`, `save_catalog_order`, `get_hidden_catalogs`, `save_hidden_catalogs` (the hidden list is stored separately from `catalog_order` in tracking `state`).
- Users/subscriptions: `get_user`, `is_subscription_active`, `set_pending_payment`, `approve_payment`, `reject_payment`, `get_expired_users`, `mark_user_expired`, `get_expiring_users`, `mark_reminder_sent`, `get_subscription_plans`, `add_subscription_plan`, `update_subscription_plan`, `delete_subscription_plan`, `get_all_subscribers`, `manage_subscriber`, `assign_subscription`, `set_user_never_expires`
- Catalogs: `create_custom_catalog`, `get_custom_catalogs`, `get_custom_catalog`, `update_custom_catalog`, `set_catalog_item_visibility`, `set_media_visibility`, `get_media_visibility`, `delete_custom_catalog`, `add_item_to_custom_catalog`, `remove_item_from_custom_catalog`, `find_media_doc`, `purge_media_from_catalogs`, `mark_item_exclusive`, `clear_item_exclusive`
- Media/streams: `get_media_ids_by_part`, `remove_media_part`, `rename_media_part`, `insert_media`, `update_movie`, `update_tv_show`, `sort_movies`, `sort_tv_shows` (sort_params-driven fast library sorting), `search_documents`, `get_media_details`, `get_document(s)`, `update_document`, `delete_document`, `get_title_by_stream_id`, `delete_media_by_stream_id`, `delete_movie_quality`, `delete_tv_quality`, `delete_tv_episode`, `delete_tv_season`, `get_database_stats`, `replace_media_metadata`, `_count`
- Tokens: `add_api_token`, `ensure_api_token_for_user`, `align_token_with_subscription`, `set_token_lifetime`, `update_token_expiry`, `grant_lifetime_to_unlinked`, `get_api_token`, `get_api_token_by_user`, `get_all_api_tokens`, `revoke_api_token`, `set_token_config`, `link_token_user`, `update_token_usage`, `update_api_token_limits`
- Dead links/analytics: `flag_dead_link`, `get_all_dead_links`, `log_stream_stats`, `get_stream_analytics`

Indexes: `custom_catalogs` (updated_at desc; items.tmdb_id+media_type); subtitles (chat_id+msg_id **unique**; imdb_id+season+episode; legacy `stream_id` indexes dropped); storage `movie`/`tv`: `tmdb_id`, `imdb_id`, `kitsu_id` asc, plus (v5.3.0) `updated_on` desc, `rating` desc, `telegram.id` asc, `telegram.parts.msg_id` asc. Tracking DB also holds `stream_analytics`.

Stream analytics: `log_stream_stats` stamps `logged_at` with `datetime.now(timezone.utc)` and updates the token's `last_active`/`last_title`/`user_name`; `get_stream_analytics` normalizes naive timestamps to UTC and returns `logged_at` as `%Y-%m-%dT%H:%M:%S.%f`[:-3] + `"Z"`.

## `verify_token()` order (security/tokens.py)

1. Missing token → `HTTPException(401)`.
2. `limit_exceeded = None`, `limit_video = None`, `subscription_expired = False`.
3. `is_admin` = flag OR `user_id == Telegram.OWNER_ID`.
4. Expiry (skipped for admins/exempt): token `expires_at` always enforced; then subscription mode requires active user, else `subscription_expired = True`.
5. Daily limit → `limit_exceeded="daily"`, video `https://bit.ly/3YZFKT5`.
6. Monthly limit → `limit_exceeded="monthly"`, video `https://bit.ly/4rfjtgd`.
Returns enriched `token_data` dict (early return, not exception) for expiry/limit cases.
