#!/usr/bin/env python3
"""
cursor-hd: local UI for inventorying installed cursor themes and batch-upgrading
them to rebuilt Xcursor size ladders plus GNOME 51 scalable cursor metadata.

Run `python3 app.py`, then open http://127.0.0.1:5057.
"""
import os
import shutil
import threading
import time
import uuid

from flask import Flask, jsonify, request, render_template

import gnome51
import model_utils
import scanner
import upgrade as upgrade_module
from local_security import local_api_request_error
from job_utils import public_job_snapshot

app = Flask(__name__)


@app.before_request
def _protect_loopback_api():
    """Block cross-origin browser writes to the loopback management API."""
    if request.path != "/api" and not request.path.startswith("/api/"):
        return None
    reason = local_api_request_error(
        host=request.host,
        origin=request.headers.get("Origin"),
        method=request.method,
        mimetype=request.mimetype,
    )
    if reason:
        return jsonify({"error": reason}), 403
    return None


JOBS = {}
JOBS_LOCK = threading.Lock()

# Scans run off the HTTP request thread so the page can poll incremental status
# while individual cursor files and SVG metadata are being inspected.
SCANS = {}
SCANS_LOCK = threading.Lock()
MAX_SCANS = 8

# Serializes writes to any given output theme directory - without this,
# two overlapping jobs targeting the same out_dir race at commit time and
# one silently clobbers the other's freshly-written result.
ACTIVE_OUT_DIRS = set()
OUT_DIRS_LOCK = threading.Lock()


def _try_acquire_out_dirs(paths):
    """Atomically locks every path in `paths`, or none of them. Returns
    (True, reals) on success, (False, conflicting_reals) if any is already
    locked by another in-flight job."""
    reals = [os.path.realpath(p) for p in paths]
    with OUT_DIRS_LOCK:
        conflicts = [r for r in reals if r in ACTIVE_OUT_DIRS]
        if conflicts:
            return False, conflicts
        ACTIVE_OUT_DIRS.update(reals)
        return True, reals


def _release_out_dirs(reals):
    with OUT_DIRS_LOCK:
        ACTIVE_OUT_DIRS.difference_update(reals)


MAX_JOBS = 50


def _prune_jobs():
    """Keep JOBS from growing forever on a long-running local server. Never
    evicts a still-running job; drops oldest-finished first once over the cap."""
    with JOBS_LOCK:
        if len(JOBS) <= MAX_JOBS:
            return
        finished = sorted(
            (j for j in JOBS.values() if j["status"] != "running"),
            key=lambda j: j["started"],
        )
        overflow = len(JOBS) - MAX_JOBS
        for j in finished[:overflow]:
            JOBS.pop(j["id"], None)


def _known_theme_path(path):
    if not isinstance(path, str) or not os.path.isdir(path):
        return False
    real = os.path.realpath(path)
    return real in {p for p, _ in scanner.find_theme_dirs()}


@app.route("/api/models")
def api_models():
    """Report supported model checkpoints found only in the project weights/."""
    return jsonify(model_utils.available_models())


@app.route("/api/status")
def api_status():
    return jsonify({
        "running_as_root": hasattr(os, "geteuid") and os.geteuid() == 0,
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan")
def api_scan():
    """Start a background inventory scan, or reuse the active one."""
    with SCANS_LOCK:
        for scan in SCANS.values():
            if scan.get("status") == "running":
                return jsonify({"scan_id": scan["id"]}), 202

        scan_id = uuid.uuid4().hex[:12]
        scan = {
            "id": scan_id,
            "status": "running",
            "phase": "starting",
            "message": "Starting scan of installed cursor themes…",
            "root_index": 0,
            "roots_total": None,
            "themes_total": None,
            "themes_done": 0,
            "theme_index": 0,
            "theme": None,
            "cursor_index": 0,
            "cursor_total": None,
            "cursor": None,
            "started": time.time(),
            "result": None,
            "error": None,
        }
        SCANS[scan_id] = scan
        finished = sorted(
            (entry for entry in SCANS.values() if entry["status"] != "running"),
            key=lambda entry: entry.get("started", 0),
        )
        while len(SCANS) > MAX_SCANS and finished:
            old = finished.pop(0)
            SCANS.pop(old["id"], None)

    def update_progress(event):
        with SCANS_LOCK:
            current = SCANS.get(scan_id)
            if current is not None and current["status"] == "running":
                current.update(event)

    def run_scan():
        try:
            result = scanner.scan_all(progress_cb=update_progress)
            with SCANS_LOCK:
                current = SCANS.get(scan_id)
                if current is not None:
                    current.update({
                        "status": "complete", "phase": "complete",
                        "message": f"Scan complete: {len(result)} themes",
                        "themes_total": len(result), "themes_done": len(result),
                        "result": result, "error": None,
                        "elapsed_seconds": round(time.time() - current["started"], 2),
                    })
        except Exception as exc:
            with SCANS_LOCK:
                current = SCANS.get(scan_id)
                if current is not None:
                    current.update({
                        "status": "error", "phase": "error",
                        "message": "Cursor theme scan failed", "error": str(exc),
                        "elapsed_seconds": round(time.time() - current["started"], 2),
                    })

    threading.Thread(target=run_scan, name=f"cursor-scan-{scan_id}", daemon=True).start()
    return jsonify({"scan_id": scan_id}), 202


@app.route("/api/scan/<scan_id>")
def api_scan_status(scan_id):
    """Return current scan status; only include inventory after completion."""
    with SCANS_LOCK:
        scan = SCANS.get(scan_id)
        if scan is None:
            return jsonify({"error": "unknown or expired scan"}), 404
        payload = dict(scan)
    if payload.get("status") == "running":
        payload.pop("result", None)
    return jsonify(payload)


@app.route("/api/theme")
def api_theme():
    path = request.args.get("path")
    if not path or not _known_theme_path(path):
        return jsonify({"error": "invalid or undiscovered theme path"}), 400
    return jsonify(scanner.inspect_theme(path))


@app.route("/api/upgrade_batch", methods=["POST"])
def api_upgrade_batch():
    """Upgrade selected installed themes as one operation.

    The upscaling method is always an explicit batch choice. Lanczos is the
    default. Real-ESRGAN is never selected merely because a checkpoint exists.
    If Real-ESRGAN fails during processing after preflight, the job pauses and
    the UI can explicitly resume the same batch with Lanczos without rescanning.
    """
    body = request.get_json(force=True) or {}
    backend_choice = body.get("backend", "lanczos")
    if backend_choice not in ("lanczos", "realesrgan"):
        return jsonify({"error": "backend must be either 'lanczos' or 'realesrgan'"}), 400
    raw_paths = body.get("theme_paths", [])
    if not isinstance(raw_paths, list):
        return jsonify({"error": "theme_paths must be a list"}), 400
    theme_paths = list(dict.fromkeys(p for p in raw_paths if isinstance(p, str)))
    if not theme_paths:
        return jsonify({"error": "select at least one cursor theme"}), 400
    if len(theme_paths) > 250:
        return jsonify({"error": "select no more than 250 themes per operation"}), 400
    if shutil.which("xcursorgen") is None:
        return jsonify({
            "error": "xcursorgen is required to rebuild the size ladder. On Arch Linux install xorg-xcursorgen."
        }), 400

    planned = []
    errors = []
    for path in theme_paths:
        if not _known_theme_path(path):
            errors.append(f"Not an installed/discovered theme: {path}")
            continue
        try:
            info = scanner.inspect_theme(path)
            plan = upgrade_module.plan_theme_upgrade(path, info)
            planned.append({"path": path, "info": info, "plan": plan})
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        return jsonify({"error": "Nothing was changed. Fix these pre-flight issues first:\n" + "\n".join(errors)}), 409

    output_paths = [os.path.realpath(item["plan"]["out_dir"]) for item in planned]
    if len(set(output_paths)) != len(output_paths):
        return jsonify({"error": "The selection contains themes that resolve to the same output path. Nothing was changed."}), 409

    try:
        backend, model_path = model_utils.resolve_backend_choice(backend_choice)
    except ValueError as exc:
        return jsonify({"error": str(exc) + " No themes were changed."}), 409

    lock_paths = []
    for item in planned:
        out_dir = item["plan"]["out_dir"]
        lock_paths.extend((out_dir, gnome51.scalable_theme_target(out_dir)))
    ok, locked = _try_acquire_out_dirs(lock_paths)
    if not ok:
        return jsonify({"error": "One or more selected theme paths are already being upgraded: " + ", ".join(locked)}), 409

    if backend == "realesrgan":
        try:
            from xcur_upscale import preflight_realesrgan
            preflight_realesrgan(model_path)
        except Exception as exc:
            _release_out_dirs(locked)
            return jsonify({
                "error": f"Real-ESRGAN preflight failed. No themes were changed. {exc}",
                "fallback_available": True,
                "fallback_backend": "lanczos",
            }), 409

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "running",
        "log": [],
        "started": time.time(),
        "result": None,
        "error": None,
        "themes_total": len(planned),
        "themes_done": 0,
        "backend": backend,
        "model": os.path.basename(model_path) if model_path else None,
        "fallback_available": False,
        "fallback_index": None,
        "lock_reals": locked,
    }
    job["log"].append({
        "phase": "upscale",
        "cursor": "<backend>",
        "status": "started",
        "message": (
            f"Using Real-ESRGAN model {os.path.basename(model_path)} from ./weights"
            if backend == "realesrgan" else "Using Lanczos (CPU); Real-ESRGAN will not be used"
        ),
    })
    with JOBS_LOCK:
        JOBS[job_id] = job
    _prune_jobs()

    def progress_for(theme_name):
        def callback(entry):
            payload = dict(entry)
            payload["theme"] = theme_name
            with JOBS_LOCK:
                job["log"].append(payload)
        return callback

    def finish_job(results, final_backend):
        succeeded = sum(result.get("status") == "ok" for result in results)
        failed = len(results) - succeeded
        with JOBS_LOCK:
            job["status"] = "done"
            job["backend"] = final_backend
            job["fallback_available"] = False
            job["fallback_index"] = None
            job["result"] = {
                "themes": results,
                "succeeded": succeeded,
                "failed": failed,
                "scale_factor": upgrade_module.SCALE_FACTOR,
                "backend": final_backend,
                "model": os.path.basename(model_path) if final_backend == "realesrgan" and model_path else None,
                "weights_dir": model_utils.weights_directory(),
            }

    def run(start_index=0, current_backend=backend):
        results = job.get("partial_results", [])
        try:
            for index in range(start_index, len(planned)):
                item = planned[index]
                info = item["info"]
                name = info.get("name") or item["plan"]["theme_id"]
                try:
                    result = upgrade_module.upgrade_theme(
                        item["path"], info, progress_cb=progress_for(name),
                        backend=current_backend,
                        model_path=model_path if current_backend == "realesrgan" else None,
                    )
                    entry = {"theme": name, **result, "status": "ok"}
                    results.append(entry)
                    with JOBS_LOCK:
                        job["log"].append({
                            "theme": name, "phase": "complete", "status": "ok",
                            "cursor": "<theme>",
                            "message": f"Updated {result.get('out_dir')} and generated {result.get('scalable_dir')}",
                        })
                except Exception as exc:
                    message = str(exc)
                    if current_backend == "realesrgan" and message.startswith("REALESRGAN_RUNTIME_FAILURE:"):
                        with JOBS_LOCK:
                            job["status"] = "needs_fallback"
                            job["backend"] = current_backend
                            job["fallback_available"] = True
                            job["fallback_index"] = index
                            job["partial_results"] = results
                            job["fallback_error"] = message.removeprefix("REALESRGAN_RUNTIME_FAILURE:").strip()
                            job["log"].append({
                                "theme": name,
                                "phase": "warning",
                                "status": "warning",
                                "cursor": "<theme>",
                                "error": job["fallback_error"],
                                "message": "Real-ESRGAN failed. The theme was rolled back. Choose CPU fallback or cancel.",
                            })
                        return
                    entry = {
                        "theme": name,
                        "source": item["path"],
                        "out_dir": item["plan"]["out_dir"],
                        "status": "error",
                        "error": message,
                    }
                    results.append(entry)
                    with JOBS_LOCK:
                        job["log"].append({
                            "theme": name, "phase": "error", "status": "error",
                            "cursor": "<theme>", "error": message,
                        })
                with JOBS_LOCK:
                    job["themes_done"] = index + 1
                    job["partial_results"] = results
            finish_job(results, current_backend)
        except Exception as exc:
            with JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(exc)
        finally:
            with JOBS_LOCK:
                waiting = job.get("status") == "needs_fallback"
            if not waiting:
                _release_out_dirs(locked)

    job["runner"] = run
    threading.Thread(target=run, name=f"cursor-upgrade-{job_id}", daemon=True).start()
    return jsonify({"job_id": job_id, "themes_total": len(planned)})


@app.route("/api/job/<job_id>/fallback", methods=["POST"])
def api_job_fallback(job_id):
    """Explicitly resume a paused Real-ESRGAN batch with Lanczos CPU."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") != "needs_fallback" or not job.get("fallback_available"):
            return jsonify({"error": "this job is not waiting for a CPU fallback"}), 409
        runner = job.get("runner")
        index = job.get("fallback_index")
        if runner is None or index is None:
            return jsonify({"error": "fallback state is unavailable"}), 409
        job["status"] = "running"
        job["backend"] = "lanczos"
        job["fallback_available"] = False
        job["fallback_index"] = None
        job["log"].append({
            "phase": "fallback",
            "cursor": "<backend>",
            "status": "warning",
            "message": "Explicit CPU fallback selected. Resuming the same batch without rescanning.",
        })
    threading.Thread(target=runner, args=(index, "lanczos"), name=f"cursor-upgrade-{job_id}-cpu", daemon=True).start()
    return jsonify({"status": "running", "backend": "lanczos"})


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    """Cancel a paused batch and release its output locks."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") != "needs_fallback":
            return jsonify({"error": "this job is not waiting for a fallback decision"}), 409
        job["status"] = "cancelled"
        job["fallback_available"] = False
        job["fallback_index"] = None
        job["error"] = "Operation cancelled after Real-ESRGAN failure."
        job["log"].append({
            "phase": "operation", "cursor": "<batch>", "status": "error",
            "message": "Operation cancelled. No rescan was started.",
        })
    _release_out_dirs(job.get("lock_reals", []))
    return jsonify({"status": "cancelled"})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    """Return a JSON-safe snapshot, excluding internal runtime-only fields.

    Jobs keep a Python callable in ``runner`` so a paused Real-ESRGAN batch
    can resume without rescanning. That callable, and the internal output-lock
    paths, must never be serialized as part of the public API response.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        snapshot = public_job_snapshot(job)
    return jsonify(snapshot)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=bool(os.environ.get("DEBUG")))
