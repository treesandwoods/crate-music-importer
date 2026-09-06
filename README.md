# Crate Music Importer

![Crate Music Importer banner](raycast-extension/assets/banner.png)

A **macOS-only** Raycast extension and Python CLI for importing public Spotify albums and playlists into Music.app. Spotify supplies metadata and artwork; missing recordings are matched against YouTube and stored as tagged MP3s. Existing Music tracks are reused when their identity can be verified.

The importer does not operate an iPod or change Finder's whole-library sync settings. Windows and Linux are not supported: Music integration requires macOS AppleScript and Raycast.

## Prerequisites

- macOS, Music.app, and [Raycast](https://www.raycast.com/).
- Python **3.10 or newer**, with `venv` support.
- Node.js **22 or newer** and npm, for building the extension from this repository.
- [Homebrew](https://brew.sh/) for the external downloader stack: `yt-dlp`, `ffmpeg` (including `ffprobe`), and Deno for modern yt-dlp JavaScript challenges.
- Your own [Spotify developer app](https://developer.spotify.com/dashboard) for album search. Public playlist-link imports do not require album-search OAuth. Spotify account eligibility and development-mode limits are controlled by Spotify.

Install the external prerequisites after installing Homebrew:

```sh
brew install python node yt-dlp ffmpeg deno
```

This initial installation is separate from **Update Dependencies**, which never installs or upgrades Python or application packages.

## Install from source

Clone or download this repository into any folder, then open a terminal in that folder. A fixed directory name is not required.

Create an isolated Python environment outside the checkout and install the application:

```sh
python3 -m venv "$HOME/Library/Application Support/Crate Music Importer/venv"
"$HOME/Library/Application Support/Crate Music Importer/venv/bin/python3" -m pip install .
cd raycast-extension
npm ci
npm run build
```

The production build installs the extension into the local Raycast extension directory. Its assets include the Python backend; the installed extension does not execute source from your checkout by default. Python, mutagen, and the external tools remain prerequisites, not bundled binaries. For development, use `npm run dev`.

Open Raycast's extension preferences for **Crate Music Importer**:

| Preference | Configuration |
| --- | --- |
| Python Executable | `~/Library/Application Support/Crate Music Importer/venv/bin/python3` from the installation above. If blank, discovers the per-user environment above, then Python on PATH with standard Homebrew locations first. |
| Managed Music Directory | Defaults to `~/Music/mp3 Music`. Existing users must keep their existing managed directory, including `.state`. |
| Spotify Client ID | Your own developer-app ID; required for album search. Never enter a client secret. |
| Repository Folder | Optional development checkout containing `crate_music_importer/`. Leave blank to use the bundled backend. |
| CLI Executable | Optional absolute path to an installed `crate-music-importer` entry point. It must support the `--raycast` protocol. Overrides bundled execution. |

Paths may be absolute or begin with `~/`. Relative paths are rejected. Each macOS user has separate configuration, OAuth tokens, application state, and a default Music directory. Do not share a managed `.state` folder between accounts.

## Spotify OAuth and forks

1. Create your own app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and enable Web API access.
2. Add this **exact** redirect URI to its settings:

   ```text
   https://raycast.com/redirect?packageName=crate-music-importer
   ```

3. Put the app's Client ID in Raycast preferences, then open **Albums Browse & Import** and sign in.
4. If the app is in development mode, authorize the Spotify account in the developer dashboard as required by Spotify.

This uses Authorization Code with PKCE; no client secret is used or shipped. Tokens are stored through Raycast's secure OAuth API. Changing the client ID uses a separate token namespace. The source deliberately overrides the redirect URI so it matches the URI above. A fork may keep it; if you change it, change the authorization request and your developer-app registration together. Spotify requires exact URI matching: [redirect requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri). See also [Raycast OAuth](https://developers.raycast.com/api-reference/oauth).

## First run and everyday use

Run **Rebuild Music Library Cache** once and confirm the read-only scan. Allow Raycast to control Music when macOS asks. In **System Settings → Privacy & Security → Automation**, grant access to the process you use: Raycast for the extension, or Terminal for the CLI. Grant file access to the configured Music directory if requested.

- **Albums Browse & Import**: search Spotify, preview an album, and deliberately queue Download + Add to Music.
- **Playlist Link Import**: preview a public Spotify playlist URL and confirm its import.
- **Review Activity & Problems**: follow durable jobs, resolve ambiguous Music/YouTube matches, retry failed work, or cancel incomplete progress.
- **Rebuild Music Library Cache**: deliberately refresh the saved index after changes outside the importer.
- **Update Dependencies**: review and confirm downloader updates as described below.

Previews use the persistent cache. Final Music writes revalidate exact persistent IDs. The importer preserves user-owned tracks and playlist memberships. Reusing an importer-owned playlist recording in a real album can update that same Music item in place when ownership is proven; ambiguous ownership or conflicting real albums stop for review.

Automatic YouTube selection requires a score strictly greater than `0.87`. Manual choices remain deliberate. Incomplete Spotify responses, missing per-track artwork, and incorrect downloaded durations stop progress instead of silently producing partial or incorrect imports.

Jobs run sequentially in a detached worker and survive closing Raycast. Keep the configured directory stable while jobs are queued or active. Do not reinstall the extension during an active job. Completion and attention notices use Raycast's compact notifications. Continue using your normal whole-library Finder/iPod sync afterward.

## Configuration and storage

Both the CLI and extension read optional per-user configuration at:

```text
~/Library/Application Support/Crate Music Importer/config.json
```

Example (keep your real configuration outside the repository):

```json
{
  "managedMusicDirectory": "~/Music/mp3 Music",
  "pythonPath": "~/Library/Application Support/Crate Music Importer/venv/bin/python3",
  "spotifyClientId": "YOUR_OWN_SPOTIFY_CLIENT_ID"
}
```

Nonempty Raycast preferences override this file. `CRATE_CONFIG_FILE` selects a different config file. CLI processes may override the Music directory with `CRATE_MANAGED_ROOT`; Raycast passes its selected directory explicitly to its backend and detached workers. `CRATE_PYTHON` selects the checkout launcher's interpreter. Installed Python console scripts use their own environment.

The optional reconciliation CLI also accepts `legacyAlbumsDirectory`, `musicLibraryPackage`, and `previousLibrariesDirectory` in the config file (or `CRATE_LEGACY_ALBUMS_ROOT`, `CRATE_MUSIC_LIBRARY_PACKAGE`, and `CRATE_PREVIOUS_LIBRARIES_ROOT`). Defaults are home-relative macOS Music locations. Set these explicitly if your Music library is on an external drive. Reconciliation is an advanced, separately confirmed audit/migration workflow; routine imports do not migrate a library.

Managed storage contains `tracks/` and `.state/` with manifests, caches, jobs, logs, and reconciliation journals. Never publish these or downloaded media. Changing the configured path selects another library; it does **not** migrate or merge existing content. Preserve the complete folder when moving it deliberately.

## Manual downloader updates

Open **Update Dependencies**, then **Check Dependency Versions**. Review each resolved executable, its installation method, installed and available stable versions, proposed command, and skipped tools. **Review and Confirm Updates** requires explicit confirmation before changing tools.

Only the active Homebrew-owned `yt-dlp`, `ffmpeg`/`ffprobe`, and installed Deno are eligible. Missing Deno is reported when required by the active Homebrew yt-dlp formula; install it separately. Unknown, pip, standalone, custom-tap, and unpaired installations are skipped rather than overwritten. Existing lookup order is preserved: PATH, Apple Silicon Homebrew, then Intel Homebrew. A higher-priority executable is never silently replaced by another installation.

Updates run sequentially using named Homebrew formula upgrades, never a general `brew upgrade` or `yt-dlp -U`. Automatic Homebrew cleanup and dependent-package upgrades are disabled. A dry-run must show only the reviewed formula; plans needing other packages, Python, migrations, or source builds are skipped. Install those prerequisites separately, then check again. The updater does not change Python, mutagen, npm/Raycast packages, or Crate Music Importer's source.

A per-user lock prevents overlapping updater commands and tool use by imports/previews. Updates refuse active import jobs. This coordinates Crate Music Importer processes; do not run another package manager or another application using these tools during an update.

After an update attempt, the command resolves the actual executables again, checks versions and FFmpeg/ffprobe availability, and performs a yt-dlp metadata extraction with **no media download**. A failed tool does not hide other results. Network or YouTube failures are reported separately from package-update results. The last diagnostic result is stored in application state as `dependencies-last-result.json`, without tokens or unrelated Homebrew inventory. Music, media, manifests, and jobs are not modified by the updater.

Equivalent CLI commands:

```sh
crate-music-importer dependencies status
crate-music-importer dependencies update --confirm-plan PLAN_ID_FROM_STATUS
```

A changed plan requires another review. Status checks use the stable Homebrew formula API, not prereleases or arbitrary release assets. Updates to Crate Music Importer itself still require a new installation/build.

## Troubleshooting

- **Python not found / missing mutagen:** repeat the isolated Python installation and set Python Executable. Use Python 3.10+, not an older system Python.
- **Backend missing:** rebuild the extension or correct Repository Folder/CLI Executable. Ordinary installations should leave those two preferences blank.
- **Spotify invalid client or redirect:** check your own Client ID, the exact URI above, and account access in the Spotify dashboard.
- **Music cache unavailable:** run Rebuild Music Library Cache and grant the requested Automation permission. Refresh it after external library changes.
- **Permission denied:** grant Raycast file/Automation access as appropriate. Do not use `sudo` for the app or its updater. A Homebrew permission failure needs repair through Homebrew's normal installation guidance.
- **Unknown installation / skipped update:** check the displayed path. The updater intentionally does not overwrite pip, custom-tap, or manually installed executables.
- **Offline / unavailable formula / timeout:** no reliable update plan is assumed. Restore connectivity, check versions again, and review the new plan.
- **Active job blocks updates:** let the job finish or use the existing activity workflow to cancel it. Do not manually delete lock files.
- **Partial update:** inspect every tool's result; retry after resolving its error. Package updates are not rolled back automatically.
- **Metadata smoke test failed:** inspect its diagnostic error. YouTube availability, throttling, and network access can fail independently of a successful Homebrew update.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/python3 -m pip install .
.venv/bin/python3 -m unittest discover -s tests/ipod_import
cd raycast-extension
npm ci
npm test
npm run typecheck
npm run lint
npm run build
```

The build regenerates the bundled backend from Python source. Tests mock network and package-manager actions; they must not upgrade tools or write a real Music library.

## License and responsible use

Crate Music Importer source is available under the [MIT License](LICENSE). External dependencies retain their own licenses; the repository does not distribute their binaries or downloaded music.

Use the software only for content you own or are authorized to download and use. You are responsible for complying with applicable copyright law and Spotify, YouTube, and other platform terms. Publishing this source does not grant rights to recordings or permission to bypass platform restrictions. Do not publish downloaded media, credentials, or working-library state with the source. Crate Music Importer is not affiliated with Spotify, YouTube, Apple, or Raycast.
