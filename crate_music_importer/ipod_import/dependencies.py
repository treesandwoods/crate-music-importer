"""Manual, formula-specific Homebrew downloader updates with a reviewable JSON plan."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

from crate_music_importer.ipod_import.constants import MANAGED_ROOT
from crate_music_importer.ipod_import.dependency_lock import dependency_lock, state_directory

FORMULAE = {"yt-dlp": "yt-dlp", "ffmpeg": "ffmpeg", "deno": "deno"}
# Public YouTube metadata probe; no media or playlist downloads.
SMOKE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def redact(value: str) -> str:
	text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[redacted]", value)
	text = re.sub(r"(?i)((?:access_token|refresh_token|client_secret|password|token|authorization|cookie)\s*[=:]\s*)[^\s&,;]+", r"\1[redacted]", text)
	text = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[redacted]@", text)
	text = re.sub(r"(?i)([\"\'](?:access_token|refresh_token|client_secret|password|authorization|cookie)[\"\']\s*:\s*)[\"\'][^\"\']*[\"\']", r'\1"[redacted]"', text)
	return text[-3000:]


def resolve_tool(name: str) -> str | None:
	for candidate in (shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
		if candidate and Path(candidate).is_file():
			return str(candidate)
	return None


def run_command(command: list[str], timeout: int = 30) -> str:
	environment = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1", "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1", "HOMEBREW_NO_ASK": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
	process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=environment)
	try:
		stdout, stderr = process.communicate(timeout=timeout)
	except subprocess.TimeoutExpired as exc:
		os.killpg(process.pid, signal.SIGKILL)
		process.communicate()
		raise RuntimeError(f"Command timed out after {timeout}s: {Path(command[0]).name}") from exc
	if process.returncode:
		raise RuntimeError(redact(stderr or stdout or f"Command exited {process.returncode}"))
	return stdout.strip()


def parse_version(name: str, text: str) -> str:
	pattern = r"(?:ffmpeg|ffprobe) version\s+(\S+)" if name in ("ffmpeg", "ffprobe") else r"(?:deno\s+)?(\d+(?:\.\d+){1,3}(?:[-_+][\w.]+)?)"
	match = re.search(pattern, text)
	if not match:
		raise ValueError(f"Cannot parse {name} version output.")
	return match.group(1)


def version(name: str, path: str) -> str:
	return parse_version(name, run_command([path, "-version" if name in ("ffmpeg", "ffprobe") else "--version"]))


def formula_metadata(formula: str) -> dict[str, Any]:
	request = Request(f"https://formulae.brew.sh/api/formula/{formula}.json", headers={"User-Agent": "Crate-Music-Importer"})
	with urlopen(request, timeout=20) as response:
		value = json.load(response)
	if not isinstance(value, dict) or not value.get("versions", {}).get("stable"):
		raise ValueError(f"Stable Homebrew metadata is unavailable for {formula}.")
	return value


def homebrew_owns(path: str | None, cellar: str | None, formula: str) -> bool:
	if not path or not cellar:
		return False
	try:
		relative = Path(path).resolve().relative_to(Path(cellar).resolve() / formula)
		if len(relative.parts) < 3 or relative.parts[1] not in ("bin", "libexec"):
			return False
		receipt = Path(cellar).resolve() / formula / relative.parts[0] / "INSTALL_RECEIPT.json"
		value = json.loads(receipt.read_text())
		return value.get("source", {}).get("tap") == "homebrew/core"
	except (ValueError, OSError, TypeError):
		return False


def safe_upgrade_plan(output: str, formula: str, available: str) -> bool:
	"""Fail closed if Homebrew plans dependencies, migrations, or unexpected changes."""
	text = re.sub(r"\x1b\[[0-9;]*m", "", output)
	if re.search(r"(?i)would (?:install|upgrade|reinstall).*depend|migrat|reinstall|would install", text):
		return False
	changes = re.findall(r"^\s*([a-z0-9@+_.\-/]+)\s+(\S+)\s+->\s+(\S+)", text, re.M)
	return len(changes) == 1 and changes[0][0] == formula and changes[0][2] == available


def stable_version_key(value: str) -> tuple[int, ...]:
	if not re.fullmatch(r"\d+(?:\.\d+)*(?:_\d+)?", value):
		raise ValueError("Only stable numeric Homebrew versions are supported; HEAD and custom builds are skipped.")
	base, _, revision = value.partition("_")
	return (*([int(part) for part in base.split(".")] + [0] * 4)[:4], int(revision or 0))


def plan_id(tools: list[dict[str, Any]]) -> str:
	fields = [{key: t.get(key) for key in ("name", "resolvedPath", "installationMethod", "previousVersion", "availableVersion", "command", "status", "error")} for t in tools]
	return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def status() -> dict[str, Any]:
	with dependency_lock():
		return _status()


def _status() -> dict[str, Any]:
	brew = resolve_tool("brew")
	cellar = None
	brew_error = None
	if brew:
		try:
			cellar = run_command([brew, "--cellar"])
		except Exception as exc:
			brew_error = redact(str(exc))
	rows = []
	metadata: dict[str, dict[str, Any]] = {}
	for name, formula in FORMULAE.items():
		path = resolve_tool(name)
		row: dict[str, Any] = {"name": name, "resolvedPath": path, "installationMethod": "homebrew" if homebrew_owns(path, cellar, formula) else "unknown" if path else "missing", "previousVersion": None, "availableVersion": None, "resultingVersion": None, "status": "skipped", "error": None, "command": None}
		try:
			if path:
				row["previousVersion"] = row["resultingVersion"] = version(name, path)
			meta = formula_metadata(formula)
			metadata[name] = meta
			stable = str(meta["versions"]["stable"])
			revision = int(meta.get("revision") or 0)
			row["availableVersion"] = stable + (f"_{revision}" if revision else "")
			if not path:
				required = name != "deno" or "deno" in metadata.get("yt-dlp", {}).get("dependencies", [])
				row["error"] = f"{name} is missing. Install it with Homebrew, then check again." if required else "Deno is not installed or required by the current Homebrew yt-dlp formula."
			elif row["installationMethod"] != "homebrew":
				row["error"] = brew_error or "The active executable is not owned by this Homebrew installation. Update it through its installer or select a Homebrew executable in PATH."
			else:
				installed = Path(path).resolve().relative_to(Path(cellar).resolve() / formula).parts[0]
				row["status"] = "current" if stable_version_key(installed) >= stable_version_key(row["availableVersion"]) else "outdated"
				if row["status"] == "outdated":
					command = [brew, "upgrade", "--formula", "--force-bottle", formula]
					preview = run_command([*command, "--dry-run"], timeout=90)
					if safe_upgrade_plan(preview, formula, row["availableVersion"]):
						row["command"] = command
					else:
						row["status"] = "skipped"
						row["error"] = "Homebrew cannot guarantee this formula alone will change. Update prerequisite packages separately, then check again."
				if name == "ffmpeg":
					probe = resolve_tool("ffprobe")
					if not homebrew_owns(probe, cellar, "ffmpeg") or Path(probe).resolve().parent != Path(path).resolve().parent:
						row["status"], row["command"] = "skipped", None
						row["error"] = "The active ffprobe is not paired with this ffmpeg installation. Fix PATH or reinstall FFmpeg first."
		except Exception as exc:
			row["status"] = "failed"
			row["error"] = redact(f"Cannot check {name}: {exc}")
		rows.append(row)
	return {"operation": "status", "operationTime": datetime.now(timezone.utc).isoformat(), "dependencies": rows, "planId": plan_id(rows), "note": "Updates that would change supporting packages are skipped. No general brew upgrade is used."}


def active_import() -> bool:
	for path in (MANAGED_ROOT / ".state/jobs").glob("*.json"):
		try:
			job = json.loads(path.read_text())
			if job.get("status") == "running":
				return True
		except (OSError, ValueError):
			continue
	return False


def validate_tools() -> dict[str, Any]:
	checks: dict[str, Any] = {}
	for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
		path = resolve_tool(name)
		try:
			checks[name] = {"path": path, "version": version(name, path) if path else None, "error": None if path else "Not installed"}
		except Exception as exc:
			checks[name] = {"path": path, "version": None, "error": redact(str(exc))}
	path = resolve_tool("yt-dlp")
	try:
		if not path:
			raise RuntimeError("yt-dlp is unavailable.")
		payload = json.loads(run_command([path, "--ignore-config", "--skip-download", "--no-playlist", "--dump-single-json", SMOKE_URL], timeout=90))
		if not payload.get("id") or not payload.get("title"):
			raise RuntimeError("Metadata extraction returned no video ID or title.")
		checks["metadataSmokeTest"] = {"status": "passed", "error": None}
	except Exception as exc:
		checks["metadataSmokeTest"] = {"status": "failed", "error": redact(str(exc))}
	return checks


def update(confirmed_plan: str) -> dict[str, Any]:
	with dependency_lock(exclusive=True):
		if active_import():
			raise RuntimeError("An import job is active. Wait for it to finish before updating downloader tools.")
		result = _status()
		if result["planId"] != confirmed_plan:
			raise RuntimeError("The dependency plan changed. Check versions and confirm the new plan before updating.")
		result["operation"] = "update"
		for row in result["dependencies"]:
			if row["status"] != "outdated":
				continue
			try:
				preview = run_command([*row["command"], "--dry-run"], timeout=90)
				if not safe_upgrade_plan(preview, row["name"], row["availableVersion"]):
					raise RuntimeError("Homebrew changed its plan. Check and confirm again; no update was started for this tool.")
				run_command(row["command"], timeout=600)
				path = resolve_tool(row["name"])
				row["resolvedPath"] = path
				row["resultingVersion"] = version(row["name"], path) if path else None
				if not path:
					raise RuntimeError("The executable is missing after the update.")
				cellar = run_command([row["command"][0], "--cellar"])
				if not homebrew_owns(path, cellar, row["name"]) or Path(path).resolve().relative_to(Path(cellar).resolve() / row["name"]).parts[0] != row["availableVersion"]:
					raise RuntimeError("Homebrew did not install the reviewed stable version. Check again for a changed formula or pinned installation.")
				row["status"] = "updated"
			except Exception as exc:
				row["status"] = "failed"
				row["error"] = redact(str(exc))
		result["validation"] = validate_tools()
		for row in result["dependencies"]:
			actual = result["validation"][row["name"]]
			row["resolvedPath"] = actual["path"]
			row["resultingVersion"] = actual["version"]
			if actual["error"] and row["status"] in ("updated", "current"):
				row["status"], row["error"] = "failed", actual["error"]
			if row["name"] == "ffmpeg" and result["validation"]["ffprobe"]["error"]:
				row["status"], row["error"] = "failed", "ffprobe is unavailable or failed its version check. Reinstall the ffmpeg formula."
		target = state_directory() / "dependencies-last-result.json"
		temporary = target.with_suffix(".tmp")
		temporary.write_text(json.dumps(result, indent=2))
		os.replace(temporary, target)
		return result
