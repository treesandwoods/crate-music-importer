"""Internal activity refresh using one manifest and one reconciled job listing."""

from pathlib import Path
from typing import Any

from crate_music_importer.ipod_import.constants import MANAGED_ROOT
from crate_music_importer.ipod_import.jobs import JobStore, jobs_snapshot, reconcile_terminal_jobs
from crate_music_importer.ipod_import.manifest import ManagedPaths, load_manifest
from crate_music_importer.ipod_import.resolver import resolver_snapshot


def activity_snapshot(*, root: Path = MANAGED_ROOT) -> dict[str, Any]:
	paths = ManagedPaths(root)
	manifest = load_manifest(paths)
	jobs = reconcile_terminal_jobs(JobStore(root), manifest)
	return {
		"resolver": resolver_snapshot(paths, manifest=manifest, jobs=jobs),
		"jobs": jobs_snapshot(root=root, manifest=manifest, jobs=jobs),
	}
