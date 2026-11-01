# Videos (Phase 04)

Video categories, the video catalogue, Bunny Stream integration, a demo
provider, the administration console, and membership-gated playback.

Videos are **content only**. Watching a video does not create a verified view,
does not credit anything and does not touch the ledger. Rewards are Phase 06+
and must be backed by real advertiser funding.

## Models (`apps/videos/models.py`)

| Model | Key points |
| --- | --- |
| `VideoCategory` | Name (case-insensitively unique), slug, description, display order, active flag. Never deleted (trigger); deactivate instead. |
| `Video` | Title, slug, description, optional category (PROTECT), `required_entitlement` (`videos.standard` / `videos.premium` / `videos.hd`), provider (`BUNNY` / `DEMO`), provider ids, duration, thumbnail file, and two independent status fields. |

**Processing status** (what the provider has done): `PENDING → UPLOADING → PROCESSING → READY`, or `FAILED` (retry by uploading again).

**Publication status** (what members can see): `DRAFT ⇄ PUBLISHED → ARCHIVED`.

Database guarantees (migration `videos/0002_integrity`):

- `PUBLISHED` requires `READY`, `published_at` and a provider id (check constraint);
- `ARCHIVED` is terminal and carries `archived_at`;
- the provider video id is unique per provider and cannot change once set;
- videos and categories cannot be deleted.

### Effects on administration and future migrations

- Nobody can delete a video or a category, not even through the Django admin
  or the shell. Videos are archived and categories deactivated instead.
  Archiving is permanent. The console asks for an explicit confirmation first.
- A deactivated category hides itself and all of its videos from members.
  Its videos are hidden even if they are published.
- `TRUNCATE` (which the test runner uses) does not fire these row triggers.
- A future migration that really must delete or un-archive rows has to do it
  deliberately: `ALTER TABLE videos_video DISABLE TRIGGER video_guard;`, then the
  change, then `ENABLE TRIGGER`, all inside that migration. Only the table owner
  can do this. Production should run the application as a non-owner role (see
  `scripts/database/production_roles.example.sql`).
- Schema changes such as adding columns or indexes are unaffected.

Permissions: `videos.manage_videos`, `videos.publish_videos` and
`videos.manage_categories`. They are granted to the **Content Managers** group by
`apps.accounts.roles.sync_role_groups` after every `migrate`. The sync is
**additive**: it adds missing role permissions and never removes permissions a
superuser added to a group, so a permission removed from the code must be
taken out of the group by hand. Super Administrators have all permissions.
Every check also requires an active staff account.

## Providers (`apps/videos/providers/`)

### Bunny Stream

Bunny uses two different secrets. Do not mix them up.

| Setting | What it is | Used for |
| --- | --- | --- |
| `BUNNY_STREAM_API_KEY` | Library **API key** (Stream → library → API) | `AccessKey` header to `https://video.bunnycdn.com`; signing TUS uploads |
| `BUNNY_STREAM_TOKEN_SECURITY_KEY` | Library **token authentication key** (Stream → library → Security) | Signing embed (player) URLs and CDN URLs (thumbnails) |
| `BUNNY_STREAM_LIBRARY_ID` | Numeric library id | All calls |
| `BUNNY_STREAM_CDN_HOSTNAME` | e.g. `vz-xxxx.b-cdn.net` | Thumbnails; CSP `img-src` |

Neither key is ever sent to a browser, logged, or written to the audit log. The
browser receives only:

- a TUS signature `SHA256(library_id + api_key + expires + video_id)`, valid for 6 hours and for one video;
- an embed URL signed with `SHA256(token_key + video_id + expires)`, valid for 10 minutes (`VIDEO_PLAYBACK_TOKEN_SECONDS`);
- thumbnail URLs with an advanced CDN token (`HS256-` HMAC), valid for 1 hour.

Errors: HTTP 408/425/429/5xx, timeouts and network failures are **temporary**
and are retried by jobs. Other 4xx responses are **permanent**. The HTTP timeout
is 10 seconds. No database transaction is open during any Bunny call.

#### Required Bunny library settings (deployment step)

1. **Security → Embed view token authentication: ON.** Without it the player URL works without a token.
2. **Security → CDN token authentication: ON.** The embed token protects only the player page. HLS playlists and video segments are protected only by CDN token authentication.
3. **Allowed referrers:** add the ClipCash domain(s).
4. **Direct play / MP4 fallback:** OFF, unless deliberately wanted.
5. The TUS endpoint `https://video.bunnycdn.com/tusupload` must accept uploads from the ClipCash origin. It does by default. CSP `connect-src` allows it.

> **Verify with a real Bunny library before go-live.** Whether Bunny's embed
> player correctly passes CDN segment tokens when both protections are enabled
> cannot be tested automatically, because the tests never call live Bunny APIs.
> See the real-Bunny checklist below.

### Demo provider

For local development and client demonstrations without Bunny.

- `VIDEO_PROVIDER=demo` is the default when `DJANGO_DEBUG=True`.
- Outside DEBUG it is refused unless `VIDEO_DEMO_ALLOWED=True`. Set that only on an isolated demo deployment. The check `videos.E002` is reported by `check --deploy`.
- Demo videos are created `READY` with a `demo-…` id and are labelled **"Demo sample"** everywhere.
- Demo videos have **no uploads** and no fake processing. There is no file-upload endpoint.
- Playback shows a labelled placeholder, or `VIDEO_DEMO_SAMPLE_URL` if set. Use only a clip you made yourself or are licensed to use, for example a file in `static/videos/`.
- Demo videos disappear from the library automatically if demo mode is switched off.

## Upload and processing flow

1. The console creates the video, a `PENDING` draft.
2. The admin chooses a file. `static/js/video-upload.js` POSTs `{name, size, type}` to `/manage/videos/<id>/upload/authorize/`.
3. The server validates the type and size (`VIDEO_UPLOAD_CONTENT_TYPES`, `VIDEO_UPLOAD_MAX_MB`).
   - It creates the Bunny video **once**: a conditional UPDATE claims `remote_creation_started_at`, and a stale claim is released after 5 minutes.
   - It returns the TUS signature.
   - Status becomes `UPLOADING`.
4. The browser uploads directly to Bunny with TUS, in 8 MB chunks.
5. The browser reports `finished`: status becomes `PROCESSING` and a `videos.sync_status` job is queued.
6. The `videos.sync_status` job (Phase 02D runner, `transactional=False`) runs as follows:
   - it fetches the status from Bunny;
   - Bunny status 4 or 8 → `READY` (duration and thumbnail are stored);
   - status 5 or 6 → `FAILED`;
   - while processing → retry with backoff (30 s to 10 min, 20 attempts).
   A permanent failure records `video.provider.sync_failed`.
7. `videos.sweep_unfinished` runs every 5 minutes:
   - it re-queues syncs for videos stuck in UPLOADING or PROCESSING;
   - it fails uploads with no activity for 24 h.
8. The admin publishes the video. This needs the publish permission, a `READY` video and an active category.

Archived Bunny videos can have their media deleted by a Super Administrator
(job `videos.delete_media`; a 404 from Bunny counts as success).

## Member access and playback

- `/videos/`, `/videos/category/<slug>/` and `/videos/<slug>/` sit behind the `videos` feature flag.
  - They list only `PUBLISHED` + `READY` videos in active categories (or no category).
  - Drafts and archived videos return 404.
  - Thumbnails are signed per page. The category is fetched with `select_related`, so there are no N+1 queries (covered by a test).
- Playback (`services.authorize_playback`):
  - the videos flag must be on and the video playable;
  - then `apps.memberships.access.check_access(user, entitlement=video.required_entitlement)` must allow it;
  - only then is a signed player URL generated.
  Refusal reasons are shown as plain messages, such as signing in, getting a membership, or a missing entitlement.
- **Content-Security-Policy** (`apps/core/csp.py`, not `/admin/`):
  - scripts and styles from `'self'` only (no inline code);
  - frames from `player.mediadelivery.net` only;
  - images from the Bunny CDN.

## Phase 06 boundary (verified viewing / rewards)

Phase 06 can build on:

- `Video.id`, `Video.provider_video_id`, `Video.duration_seconds`;
- the signal `apps.videos.services.playback_authorized(user, video, expires_at)`, sent when a signed player URL is issued. **This means "playback was allowed", not "the video was watched".** It must never be used to credit anyone;
- the audit events below.

Verified viewing needs its own evidence (for example player events checked
server-side), its own anti-fraud rules, and real advertiser funding before
any reward exists.

## Audit events (category `CONTENT`)

| Event | Event |
| --- | --- |
| `video.category.created` | `video.upload.failed` |
| `video.category.updated` | `video.processing.completed` |
| `video.category.status_changed` | `video.processing.failed` |
| `video.created` | `video.published` |
| `video.updated` | `video.unpublished` |
| `video.upload.initiated` | `video.archived` |
| `video.upload.completed` | `video.media.deletion_requested` |
| `video.media.deleted` | `video.provider.sync_failed` |

`video.action.denied` (SECURITY) records refused attempts. Routine playback
is not audited, because it is not a business event.

## Known limitations

- A signed player URL stays valid until it expires (10 minutes), even if the membership ends in the meantime.
- If Bunny creates a video but saving its id fails, an orphan video is left in Bunny. It is unused, and can be deleted in the Bunny dashboard.
- Upload progress is not resumable after a page reload. The admin chooses the file again, and a new TUS upload is started.
- If the browser closes before reporting `finished`, the sweep job still picks the video up through Bunny's status.
- Swahili translations of the new pages are not written yet.
