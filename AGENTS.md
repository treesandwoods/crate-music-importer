# Repository guidelines

## Structure and conventions
Crate Music Importer is a macOS-only Raycast extension and Python CLI.
- `crate_music_importer/ipod_import/`: matching, media, Music integration, cache, jobs, configuration, and dependency management.
- `raycast-extension/`: Raycast commands, assets, and backend packaging script.
- `tests/ipod_import/`: backend tests with synthetic fixtures.

Read relevant code before editing. Python uses tabs, type hints, and double-quoted strings.
Do not embed personal filesystem paths, credentials, library state, or local OAuth configuration.
Use the shared configuration readers; user preferences override per-user configuration.
The build copies Python source into extension assets. Do not hand-edit that generated copy.

## Validation
Use Python 3.10+ and an isolated environment. Run the entire Python suite:
`python3 -m unittest discover -s tests/ipod_import`.
In `raycast-extension`, run `npm ci`, `npm test`, `npm run typecheck`, `npm run lint`, and `npm run build`.
Mock package-manager and network operations in automated updater tests.
Do not run live package upgrades, imports, or Music mutations as a side effect of tests.
Report mocked, packaged, and live validation boundaries separately.

## Behavior to preserve
Normal previews use the persistent Music cache; full scans require the deliberate rebuild command.
Preserve exact-ID validation, ownership checks, resumable checkpoints, per-track artwork, duration validation,
and the strict YouTube score threshold above 0.87.
Honor the configured managed directory and never migrate existing state implicitly.
Never change Finder's whole-library iPod sync or delete user-owned Music tracks.
Preserve existing command labels and visual design unless the user authorizes a change.
Downloader consumers and the manual updater must share the dependency lock.
Update only reviewed Homebrew downloader formulae; skip plans involving other packages.
