#!/usr/bin/env python3
"""
vpipe-app — a local page for the video-pipeline teardown workflow.

Runs vpipe end to end (download/transcript/frames/sheet) and serves a page
to review the result, assemble the analysis prompt, and hand it to whichever
chat you're logged into — Claude, ChatGPT, Gemini. No API keys: the app's
job stops at "prompt ready to paste," the same way the manual workflow did.

Stdlib only, on purpose — python3 is already required by vtranscribe, so
this adds nothing new to install. Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
VPIPE = BIN_DIR / "vpipe"
TEMPLATE = REPO_ROOT / "prompts" / "03-ad-teardown.md"
OUT_ROOT = REPO_ROOT / "out"
APP_DIR = Path(__file__).resolve().parent

HOST = "127.0.0.1"
PORT = 8765

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DONE_RE = re.compile(r"Done → (\S+)")
STEP_LABELS = [
    "Resolving video id",
    "Downloading",
    "Transcribing",
    "Extracting frames",
    "Composing",
    "Contact sheet",
    "Done",
]

# job_id -> {state, step, log, out_dir, error, link, notes, screenshots}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def slugify(filename: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", filename)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return slug or "video"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def read_body_to_file(rfile, length: int, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    remaining = length
    with open(dest, "wb") as f:
        while remaining > 0:
            chunk = rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)


def read_frames_tsv(out_dir_abs: Path) -> list[dict]:
    tsv = out_dir_abs / "frames" / "index.tsv"
    if not tsv.exists():
        return []
    frames = []
    with open(tsv, encoding="utf-8") as f:
        header = True
        for line in f:
            if header:
                header = False
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            file, seconds, segment = parts
            frames.append({"file": file, "seconds": float(seconds), "segment": segment})
    return frames


def run_pipeline(job_id: str, args: list[str], known_out_dir: str | None) -> None:
    job = JOBS[job_id]
    job["state"] = "running"
    job["step"] = "starting"
    job["log"] = ""
    out_dir = known_out_dir
    try:
        proc = subprocess.Popen(
            [str(VPIPE), *args],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for raw_line in proc.stdout:
            line = strip_ansi(raw_line.rstrip("\n"))
            if not line:
                continue
            job["log"] = (job["log"] + line + "\n")[-4000:]
            for label in STEP_LABELS:
                if label in line:
                    job["step"] = label
                    break
            m = DONE_RE.search(line)
            if m:
                out_dir = m.group(1)

        returncode = proc.wait(timeout=1800)
        if returncode != 0:
            job["state"] = "error"
            job["error"] = f"vpipe exited with status {returncode} — see log"
            return

        if not out_dir or not (REPO_ROOT / out_dir).is_dir():
            job["state"] = "error"
            job["error"] = "vpipe finished but its output directory could not be determined"
            return

        job["out_dir"] = out_dir
        job["state"] = "done"
    except subprocess.TimeoutExpired:
        job["state"] = "error"
        job["error"] = "timed out after 30 minutes"
    except Exception as e:  # noqa: BLE001 - surface whatever went wrong, don't swallow it
        job["state"] = "error"
        job["error"] = str(e)


def new_job(out_dir_hint: str | None = None) -> dict:
    return {
        "state": "queued",
        "step": "",
        "log": "",
        "out_dir": out_dir_hint,
        "error": None,
        "link": "",
        "notes": "",
        "screenshots": [],
    }


def build_prompt(job: dict) -> str:
    out_dir_abs = REPO_ROOT / job["out_dir"]
    transcript_path = out_dir_abs / "video.txt"
    transcript = transcript_path.read_text(encoding="utf-8").strip() if transcript_path.exists() else ""

    frames = read_frames_tsv(out_dir_abs)
    sheet_note = ""
    if (out_dir_abs / "sheet.jpg").exists():
        sheet_note = f"sheet.jpg — {len(frames)} frames in timestamp order, hook denser than body (H/B + timestamp labelled on each tile)"
    elif frames:
        sheet_note = f"{len(frames)} individual frames (no sheet.jpg — attach frames/ directly)"

    screenshots = job.get("screenshots") or []
    if screenshots:
        sheet_note += f", plus {len(screenshots)} screenshot(s): {', '.join(screenshots)}"

    lines = [
        f"Video: {job.get('link') or '(not given)'}",
        f"Attached to this message: {sheet_note or '(no frames extracted)'}",
    ]
    if job.get("notes"):
        lines.append(f"Notes from me: {job['notes']}")
    lines.append("")
    lines.append("Transcript ([start -> end] per line; blank means no speech detected):")
    lines.append(transcript or "(no speech detected)")
    lines.append("")
    lines.append("---")
    lines.append("")

    template_text = TEMPLATE.read_text(encoding="utf-8")
    marker = "## 0. Why this one"
    idx = template_text.find(marker)
    lines.append(template_text[idx:] if idx != -1 else template_text)

    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "vpipe-app/1.0"

    # ---- helpers -----------------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _job_or_404(self, job_id: str) -> dict | None:
        job = JOBS.get(job_id)
        if job is None:
            self._send_json({"error": f"no such job: {job_id}"}, 404)
        return job

    # ---- routing -------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            self._send_file(APP_DIR / "index.html")
            return

        if path.startswith("/out/"):
            # job["out_dir"] is already "out/<slug>" (matching vpipe's own
            # out_dir convention), so this route maps straight onto
            # REPO_ROOT — no extra "out/" prefix to strip or re-add.
            rel = unquote(path.lstrip("/"))
            target = (REPO_ROOT / rel).resolve()
            out_root = OUT_ROOT.resolve()
            if target != out_root and out_root not in target.parents:
                self._send_json({"error": "forbidden"}, 403)
                return
            self._send_file(target)
            return

        if path.startswith("/status/"):
            job_id = unquote(path[len("/status/") :])
            job = self._job_or_404(job_id)
            if job is None:
                return
            payload = {
                "state": job["state"],
                "step": job["step"],
                "log": job["log"],
                "error": job["error"],
                "link": job["link"],
                "notes": job["notes"],
                "screenshots": job["screenshots"],
            }
            if job["out_dir"]:
                payload["out_dir"] = job["out_dir"]
            if job["state"] == "done":
                out_dir_abs = REPO_ROOT / job["out_dir"]
                transcript_path = out_dir_abs / "video.txt"
                payload["transcript"] = (
                    transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
                )
                payload["frames"] = read_frames_tsv(out_dir_abs)
                payload["has_sheet"] = (out_dir_abs / "sheet.jpg").exists()
                payload["has_video"] = (out_dir_abs / "video.mp4").exists()
            self._send_json(payload)
            return

        if path.startswith("/prompt/"):
            job_id = unquote(path[len("/prompt/") :])
            job = self._job_or_404(job_id)
            if job is None:
                return
            if job["state"] != "done":
                self._send_json({"error": "job is not done yet"}, 409)
                return
            self._send_text(build_prompt(job))
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/upload":
            filename = unquote(self.headers.get("X-Filename", "video.mp4"))
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self._send_json({"error": "empty upload"}, 400)
                return
            slug = slugify(filename)
            out_dir_rel = f"out/{slug}"
            dest = REPO_ROOT / out_dir_rel / "video.mp4"
            read_body_to_file(self.rfile, length, dest)

            job_id = slug
            with JOBS_LOCK:
                JOBS[job_id] = new_job(out_dir_hint=out_dir_rel)
            threading.Thread(
                target=run_pipeline,
                args=(job_id, ["--skip-download", "-o", out_dir_rel, "--sheet"], out_dir_rel),
                daemon=True,
            ).start()
            self._send_json({"job_id": job_id, "out_dir": out_dir_rel})
            return

        if path == "/url":
            data = self._read_json_body()
            url = (data.get("url") or "").strip()
            if not url:
                self._send_json({"error": "missing url"}, 400)
                return
            job_id = f"job_{int(time.time() * 1000)}"
            with JOBS_LOCK:
                JOBS[job_id] = new_job()
                JOBS[job_id]["link"] = url
            threading.Thread(
                target=run_pipeline,
                args=(job_id, [url, "--sheet"], None),
                daemon=True,
            ).start()
            self._send_json({"job_id": job_id})
            return

        if path == "/meta":
            data = self._read_json_body()
            job_id = data.get("job_id", "")
            job = self._job_or_404(job_id)
            if job is None:
                return
            if "link" in data:
                job["link"] = data["link"]
            if "notes" in data:
                job["notes"] = data["notes"]
            self._send_json({"ok": True})
            return

        if path == "/screenshot":
            job_id = unquote(self.headers.get("X-Job-Id", ""))
            job = self._job_or_404(job_id)
            if job is None:
                return
            if not job["out_dir"]:
                self._send_json({"error": "job has no output directory yet"}, 409)
                return
            filename = unquote(self.headers.get("X-Filename", "screenshot.png"))
            length = int(self.headers.get("Content-Length", "0"))
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename)
            dest = REPO_ROOT / job["out_dir"] / "screenshots" / safe_name
            read_body_to_file(self.rfile, length, dest)
            job["screenshots"].append(safe_name)
            self._send_json({"ok": True, "path": f"{job['out_dir']}/screenshots/{safe_name}"})
            return

        if path == "/report":
            job_id = unquote(self.headers.get("X-Job-Id", ""))
            job = self._job_or_404(job_id)
            if job is None:
                return
            if not job["out_dir"]:
                self._send_json({"error": "job has no output directory yet"}, 409)
                return
            length = int(self.headers.get("Content-Length", "0"))
            text = self.rfile.read(length).decode("utf-8") if length else ""
            dest = REPO_ROOT / job["out_dir"] / "teardown.md"
            dest.write_text(text, encoding="utf-8")
            self._send_json({"ok": True, "path": f"{job['out_dir']}/teardown.md"})
            return

        if path == "/open":
            data = self._read_json_body()
            job = self._job_or_404(data.get("job_id", ""))
            if job is None:
                return
            if not job["out_dir"]:
                self._send_json({"error": "job has no output directory yet"}, 409)
                return
            target = str(REPO_ROOT / job["out_dir"])
            opened = False
            for opener in ("open", "xdg-open"):
                if shutil.which(opener):
                    subprocess.Popen([opener, target])
                    opened = True
                    break
            self._send_json({"ok": opened, "path": target})
            return

        self._send_json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    if not VPIPE.exists():
        print(f"error: {VPIPE} not found — run this from the video-pipeline repo", file=sys.stderr)
        sys.exit(1)

    OUT_ROOT.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"vpipe-app running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")


if __name__ == "__main__":
    main()
