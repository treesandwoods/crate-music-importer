# Streamline reliability validation

Three concurrent pre-edit audits used GPT 6 Luna with High reasoning: state/recovery, import correctness, and frontend/performance. Findings were reviewed before assigning separate implementation ownership. Regression fixtures reproduced the state races and crash windows before their fixes. Automated checks use temporary library/application-state roots, controlled interleavings, mock Music/network/media/package operations, and deferred UI requests.

## State, cache, and import fixes

| Severity | Exact code location and trigger | Evidence, fix, and regression coverage |
| --- | --- | --- |
| High | `jobs.py:enqueue`, `_run_queue`, `cancel_incomplete`, `recover_interrupted` | Startup could overwrite newer progress; enqueue could miss worker handoff; cancellation could overwrite completion or race pickup. Targeted lifecycle transactions, runner handoff checks, starter leases, cancellation reservations, and dead-worker recovery now protect these transitions. Barrier/controlled fixtures also cover notification acknowledgments, retry races, phase-only progress, malformed jobs, and unsafe paths. |
| High | `manifest.py:save_manifest`, `update_manifest`; pipeline/health/resolver/playlist checkpoint writers | Two loaded snapshots lost unrelated updates. Loaded/cloned snapshots now checkpoint field deltas under a short transaction; concurrent choices, bindings, source links, and artwork survive. In-memory rebasing retains worker references, stale writes cannot resurrect deleted records, and unchanged transactions do not write. No schema migration was added. |
| High | `playlist_update.py:_ensure_addition_ids`, `_validate_permanent_deletions`, `apply_playlist_update`; `jobs.py:_delete_source_progress` | Crashes after a Music addition could duplicate it, and deletion/cancellation could become stuck after Music/file removal. Durable proof and per-step checkpoints now recognize verified completed operations. Fault injection covers Music deletion, cache removal, file unlink, recording removal persistence, changed ownership/hash/location, renewed references, and reappearing exact IDs. |
| Medium | `music_cache.py:load_music_cache`, incremental upsert/remove, `MusicCacheSession`; health scan publication | Concurrent writers lost entries, concurrent stale readers duplicated scans, and incremental changes falsely renewed full-scan age. Scan/update coordination and rechecks prevent this; `scanned_at` changes only after a full scan. Sessions reuse validated data but still write durably at each changed checkpoint, and reload after external changes or failed writes. Failed rebuilds preserve prior bytes and direct users to Library Health. Album-search checks remain read-only. |
| Medium | `music_lock.py:music_operation_lock`, `music.py:_osascript`, `cli.py:run` | Parallel CLI/worker automation could overlap. One reentrant process lock serializes complete CLI apply operations and adapter calls. Tests prove exclusion with controlled concurrent calls; state transaction locks never span Music/network calls. |
| Medium | `spotify.py:_finalize_source` | Exactly 100 reported playlist tracks were always rejected; multi-disc albums used whole-album track totals. Declared complete 100-track sources now work, unknown capped 100-track responses remain blocked, and per-disc totals are correct. Incomplete response protection, duplicate occurrences, edition matching, artwork, duration, and the strict automatic YouTube threshold remain unchanged. |
| Medium | `health_audit.py:_load`; `manifest.py:load_manifest`, `_validate_manifest` | Corrupt JSON/controller fields could crash status reads. Invalid state now gives actionable errors while preserving original files and any valid saved report; invalid state cannot be migrated before validation. |

## Frontend audit findings


| Severity | Location and trigger | Evidence and status | Change and regression check |
| --- | --- | --- | --- |
| High | `review-problems.tsx`: any active job caused a one-second refresh; each refresh launched separate resolver and jobs subprocesses. | Reproduced by tracing the timer and the two calls. The combined-activity route now supplies both snapshots from one backend process; the one-second refresh cadence is unchanged. | One `loadActivity(signal)` read per refresh. Existing review/activity view-model tests cover rendering and unchanged-snapshot reuse. |
| High | Saved Playlists startup: the saved-list read performed artwork discovery before rows returned; frontend waited for image downloads/cropping before clearing loading. | Reproduced by tracing `loadSavedPlaylists` and `cacheArtwork`. Backend `--cached` now returns saved rows without Spotify artwork discovery; rows display before background refresh and artwork cropping. | Mocked UI test resolves cached rows before the fresh request; checks that a fresh-list response cannot overwrite an artwork refresh for the same link. No network or image download is used in this test. |
| High | Album catalog search: search called Spotify API without a caller signal, and the API helper only had a timeout on its signal-combination path. Token requests also had no bound. | Reproduced by inspecting the fetch init objects. | Both API and token fetches now install `AbortSignal.timeout(15_000)`. A mocked fetch test stubs timeout creation, asserts exactly 15,000 ms, triggers the stub signal, and verifies the fetch signal aborts. A mocked 401 test asserts stored OAuth tokens are removed. |
| Medium | Source/playlist preview refresh: starting a second preview or leaving the screen could leave older work running; older completion could replace newer state. The 660-second subprocess timeout made hung requests long-lived. | Deterministic deferred-promise tests exercise out-of-order completion; the original paths had no serial guard or abort signal. | New preview serials ignore superseded results; unmount/new preview aborts subprocess reads. Tests cover album source preview and saved playlist update preview overlap. |
| Medium | Import and saved-playlist update actions could be triggered repeatedly while confirmation was open. | Deterministic test invokes each submission twice before resolving confirmation. | Synchronous refs block the second submit before showing a second confirmation. Source and playlist update tests assert one confirmation and one queue call. |
| Medium | Refreshing playlist artwork could apply a result from the old Spotify link, and a pending full-list refresh could overwrite a newer cover after the link stayed the same. | Stale link was reproducible by resolving mocked artwork after changing the stored link; same-link race was exercised by resolving a pending list response after the explicit artwork refresh. | Validate returned source URL and request revision before/after image work; merge newer artwork when a fresh saved-list response arrives. UI test asserts both stale-link and same-link pending-response cases. |
| Low | Import form repeated explanation that the next page previews before the later confirmation; README repeated workflow invariants across adjacent paragraphs. | Read-only copy audit. | Shortened the form helper and consolidated README command workflows while retaining command/action labels, warnings, confirmations, and behavior descriptions. |

No live Spotify, Music.app, import, OAuth, or dependency-update operation was used for these checks. UI/network tests use local mocks and fixture values.

## Identical before/after benchmarks

[`scripts/benchmark_streamline.py`](../scripts/benchmark_streamline.py) runs identical synthetic fixtures against base commit `b4d03bdf000859ff0232efe613f3c19308b93732` and the final implementation. Each timing is the median of three repetitions. Fixture setup is excluded. In-process counts include the original review-dispatch manifest read; subprocess timings execute the actual fixture-only CLI from each checkout, without the counting hooks. Raw measurements and outcome fingerprints are saved in [`benchmark-before.json`](benchmark-before.json) and [`benchmark-after.json`](benchmark-after.json).

Every paired activity/cache outcome SHA-256 matched after excluding only managed-root paths and write timestamps. All source/history rows are retained. No benchmark invokes Music, Spotify, a package manager, or media tools.

| Activity fixture | In-process ms before → after | Actual subprocess ms before → after | Processes | Manifest reads | Job-file reads |
| --- | ---: | ---: | ---: | ---: | ---: |
| 20 recordings, 2 sources, 2 history rows | 1.44 → 0.52 | 207.26 → 144.22 | 2 → 1 | 4 → 1 | 10 → 2 |
| 5000 recordings, 50 sources, 1000 history rows | 1973.52 → 111.17 | 2072.44 → 187.89 | 2 → 1 | 4 → 1 | 53,000 → 1,000 |

| Cache fixture (20 upserts) | ms before → after | Cache reads | Durable writes | Scans |
| --- | ---: | ---: | ---: | ---: |
| 20 tracks, unchanged | 13.50 → 3.47 | 20 → 1 | 20 → 0 | 0 → 0 |
| 20 tracks, changed | 14.49 → 16.34 | 20 → 1 | 20 → 20 | 0 → 0 |
| 5,000 tracks, unchanged | 1567.38 → 22.98 | 20 → 1 | 20 → 0 | 0 → 0 |
| 5,000 tracks, changed | 1550.45 → 1352.34 | 20 → 1 | 20 → 20 | 0 → 0 |

Small changed-cache writes have modest coordination overhead; the reduction is in reads, not fewer recovery checkpoints. Unchanged upserts avoid rewriting the complete cache. These are local fixture measurements, not live Music/Spotify latency claims. The active one-second UI refresh cadence remains unchanged.

Saved-playlist initial delivery makes zero artwork network requests with `--cached`; the existing artwork refresh runs afterward. Mocked UI fixtures prove rows become usable while cover discovery/cropping is pending and reject stale links/results. Network request duration was not measured live.

## Validation boundaries

- **Automated:** Python 3.14 in an isolated venv/application-state directory: `python3 -m unittest discover -s tests/ipod_import` passed **301 tests**. Node 26.8.1: `npm ci`, **87 npm tests**, typecheck, lint, and build passed. Coverage includes empty/large sources, duplicate occurrences, multiple discs, ambiguous identities, missing artwork/locations, incomplete responses, API/token timeouts, expired tokens, rapid navigation, repeat submissions, malformed state, shared references, concurrent rebuilds, and crash boundaries. Network, downloader upgrades, and Music mutations are mocked.
- **Packaged/installed:** The generated and installed Python bundles match repository source byte-for-byte, and installed command definitions match. Fixture-only packaged album/playlist previews, cached saved-list delivery, and combined activity JSON passed in temporary roots. The production build registered the extension successfully; merged-main rebuild is part of delivery verification.
- **Live:** No live imports, Music deletions, file cleanup, OAuth requests, or dependency upgrades were performed. Raycast rendering and Cloud Library/network behavior were not exercised against the user's real library.

## Documentation and remaining limits

Removed the repeated album-search description and shortened the import helper. Consolidated README workflows and corrected cache guidance in AGENTS.md without removing ownership, sync, update-lock, matching, or confirmation safeguards. The project-file inventory found no obsolete project file requiring deletion; removed one unused test-file tail expression.

PID liveness still uses process IDs; a reused live PID cannot be distinguished portably from the recorded process. New startup leases record their starter so a definitively dead starter is actionable immediately. Album edition label normalization is an audit-only policy uncertainty and was preserved as requested. External Music/Spotify changes remain subject to exact-ID, source-plan, and membership validation; live service timing is unmeasured.
