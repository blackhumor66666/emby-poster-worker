# Security and privacy

## Reporting

Open an issue for anything that is not itself a credential. For a leaked secret,
do not paste it into an issue — revoke it at the provider first, then describe
the impact without the value. Security reports should not include credentials or
private server addresses.

## What must never be committed

`.env`, Emby API keys, OMDb / MDBList keys, `config/` (admin credential hash),
`cache/` (rendered posters, item state, rating caches), NAS paths, proxy
addresses, private domains, and personal library metadata.

`.gitignore` already covers these. If a secret does reach a commit, revoke it at
the provider and rewrite history — deleting the file in a later commit is not
enough.

## Deploying safely

- The admin console is protected by a PBKDF2 password hash stored under
  `ADMIN_DATA_DIR`. With `ADMIN_PASSWORD` empty, a random password is generated
  on first start and printed **once** to the container log
  (`docker logs emby-poster-worker | grep -i bootstrap`). Change it after the
  first login.
- Prefer binding the console to your LAN. If you expose it to the internet, put
  it behind a reverse proxy with TLS and its own authentication — the built-in
  session cookie is not marked `Secure`.
- The worker holds an Emby API key with image-write permission. Treat the
  container as having the same trust level as Emby itself.
- `PUBLIC_BASE_URL` serves rendered posters without authentication. Anyone who
  can reach that URL can enumerate the image names; do not expose it publicly.

## Usage risk and data sources

- **This tool overwrites Emby Primary images.** Keep `cache/original/` (and
  `BACKUP_ENABLED=true` for an off-volume archive) so you can restore. Try
  `DRY_RUN=true` first.
- Rating and metadata values come from third parties: Douban via the Shenyi
  Assistant Pro local cache, plus OMDb, MDBList, IMDb and Rotten Tomatoes via
  Emby fields. Their availability, accuracy, quotas and terms of service are
  outside this project's control. You are responsible for holding valid API keys
  and respecting each provider's terms.
- Poster artwork belongs to the respective rights holders. This project is for
  personal, self-hosted library use; do not redistribute fetched artwork or
  scraped rating data as a dataset.
- Douban scraping and cache generation is performed by Shenyi Assistant Pro, not
  by this project. No Douban credentials or tokens are handled here.

Provided under the MIT License, without warranty of any kind.
