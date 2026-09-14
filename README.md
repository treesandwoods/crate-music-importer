# Crate Music Importer

![Crate Music Importer banner](raycast-extension/assets/banner.png)

A **macOS-only** Raycast extension and Python CLI for importing public Spotify albums and playlists into Music.app. Spotify supplies metadata and artwork; missing recordings are matched against YouTube and stored as tagged MP3s. Existing Music tracks are reused when their identity can be verified.

The importer does not operate an iPod or change Finder's whole-library sync settings. Windows and Linux are not supported: Music integration requires macOS AppleScript and Raycast.

## Prerequisites

- macOS, Music.app, and [Raycast](https://www.raycast.com/).
- Python **3.10 or newer**, with `venv` support.
- Node.js **22.22.2 or newer** and npm, for building the extension from this repository.
- [Homebrew](https://brew.sh/) for the external downloader stack: `yt-dlp`, `ffmpeg` (including `ffprobe`), and Deno for modern yt-dlp JavaScript challenges.
- Your own [Spotify developer app](https://developer.spotify.com/dashboard) for album search. Public playlist-link imports do not require album-search OAuth. Spotify account eligibility and development-mode limits are controlled by Spotify.

Install the external prerequisites after installing Homebrew:

```sh
brew install python node yt-dlp ffmpeg deno
```

This initial installation is separate from **Library Health & Updates**, which never installs or upgrades Python or application packages.

To install the extension from source, use an isolated Python environment, install the package, then install and build the Raycast extension normally:

```sh
python3 -m venv "$HOME/Library/Application Support/Crate Music Importer/venv"
"$HOME/Library/Application Support/Crate Music Importer/venv/bin/python3" -m pip install .
cd raycast-extension
npm ci
npm run build
```

The build bundles the Python source and installs the extension locally. Python, mutagen, and the external tools remain external prerequisites. Use `npm run dev` during development.

Open Raycast's extension preferences for **Crate Music Importer**:

| Preference | Configuration |
| --- | --- |
| Python Executable | `~/Library/Application Support/Crate Music Importer/venv/bin/python3` from the installation above. If blank, discovers the per-user environment above, then Python on PATH with standard Homebrew locations first. |
| Managed Music Directory | Defaults to `~/Music/mp3 Music`. Existing users must keep their existing managed directory, including `.state`. |
| Spotify Client ID | Your own developer-app ID; required for album search. Never enter a client secret. |
| Repository Folder | Optional development checkout containing `crate_music_importer/`. Leave blank to use the bundled backend. |
| CLI Executable | Optional absolute path to an installed `crate-music-importer` entry point. It must support the `--raycast` protocol. Overrides bundled execution. |

Paths may be absolute or begin with `~/`. Relative paths are rejected. Each macOS user has separate configuration, OAuth tokens, application state, and a default Music directory. Do not share a managed `.state` folder between accounts.

## Commands and use

On first use, open **Library Health & Updates** and explicitly run **Library Health Audit**. This read-only Music scan builds the persistent index used by previews. Allow Raycast to control Music when macOS asks, and grant file access to the configured Music directory if requested.

- **Albums Browse & Import** searches Spotify, previews each track against the saved Music index, and queues a confirmed album import. The detached worker revalidates exact Music IDs, finds only high-confidence YouTube recordings, downloads and tags missing MP3s with album metadata and artwork, adds them to Music, stabilizes the first new Music entry before continuing, and verifies the full album before reporting completion. Ambiguity or a failed check stops for review instead of creating a guess or duplicate.
- **Playlists Browse & Import** keeps **Import a New Playlist** first and lists prior imports below it with center-cropped square artwork and no rounded corners. A new public Spotify link follows the same preview, confirmation, matching, download, and exact-ID validation workflow. Selecting a saved playlist previews an occurrence-aware full sync: additions, removals, duplicate occurrences, Spotify position changes, and Music.app-only discrepancies are shown before confirmation. Applying it makes that Music.app playlist exactly match Spotify order; unexpected manual playlist entries are removed from the playlist but never deleted from the Music library. Every Spotify removal is labeled **unlink** or **delete permanently** before confirmation.
- **Review Activity & Problems** shows the durable FIFO job queue, per-track progress, completed history, review choices, resumable failures, retry actions, and cancellation for incomplete work. Jobs continue after Raycast closes. The same command delivers completion, failure, and approval-needed events as a HUD after a background launch or as a toast while the Activity view is open. Events remain pending until delivery succeeds, including a new completion after a reviewed job is retried.
- **Library Health & Updates** immediately shows the latest saved report and checks dependency versions without starting a scan. Explicit refresh and deep-check actions run a detached read-only audit, refresh the preview index only after a successful Music scan, and keep prior results visible while work runs. The same command can preview and confirm safe stable updates only for the detected Homebrew `yt-dlp`, `ffmpeg`/`ffprobe`, and Deno installations; it never performs a general upgrade.

Automatic YouTube selection requires a score strictly greater than `0.87`; manual choices remain deliberate. Incomplete Spotify responses, missing per-track artwork, incorrect downloaded durations, uncertain Music identity, or conflicting real albums stop progress instead of silently producing a partial import.

Previews use the persistent cache, but final writes validate exact persistent IDs again. Importer-owned playlist recordings may be upgraded in place for a real album only when ownership is proven. Full playlist updates change only the selected playlist membership; user-owned Music library tracks remain untouched. Crate Music Importer never changes Finder or iPod whole-library sync settings; continue using your normal sync afterward.

## Spotify OAuth and forks

1. Create your own app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and enable Web API access.
2. Add this **exact** redirect URI to its settings:

   ```text
   https://raycast.com/redirect?packageName=crate-music-importer
   ```

3. Put the app's Client ID in Raycast preferences, then open **Albums Browse & Import** and sign in.
4. If the app is in development mode, authorize the Spotify account in the developer dashboard as required by Spotify.

This uses Authorization Code with PKCE; no client secret is used or shipped. Tokens are stored through Raycast's secure OAuth API. Changing the client ID uses a separate token namespace. The source deliberately overrides the redirect URI so it matches the URI above. A fork may keep it; if you change it, change the authorization request and your developer-app registration together. Spotify requires exact URI matching: [redirect requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri). See also [Raycast OAuth](https://developers.raycast.com/api-reference/oauth).

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

Managed storage keeps finished MP3s under `Music/Album Artist/Album/`, with compilations under `Music/Compilations/Album/`. `.state/` contains manifests, caches, jobs, and logs; `.staging/` contains resumable working material. Never publish these or downloaded media. Changing the configured path selects another library; it does **not** migrate or merge existing content. Preserve the complete folder when moving it deliberately.

## Troubleshooting

- **Python not found / missing mutagen:** repeat the isolated Python installation and set Python Executable. Use Python 3.10+, not an older system Python.
- **Backend missing:** rebuild the extension or correct Repository Folder/CLI Executable. Ordinary installations should leave those two preferences blank.
- **Spotify invalid client or redirect:** check your own Client ID, the exact URI above, and account access in the Spotify dashboard.
- **Music cache unavailable:** run or refresh Library Health and grant the requested Automation permission. Refresh it after external library changes.
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
