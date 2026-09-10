#!/usr/bin/env python3
"""
vpipe-app — a local page for the video-pipeline teardown workflow.

Runs vpipe end to end (download/transcript/frames/sheet) and serves a page
to review the result and turn it into a teardown. Two ways out: run the
analysis here through the local `claude` CLI, which is already signed in to
a subscription, or copy the prompt and paste it into whichever chat you like
— Claude, ChatGPT, Gemini. Either way there is no API key to store and
nothing billed per video.

Stdlib only, on purpose — python3 is already required by vtranscribe, so
this adds nothing new to install. Binds to 127.0.0.1 only.
"""

from __future__ import annotations

import errno
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
from urllib.request import urlopen

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

# Both of these are allowlists rather than free text because the value goes
# straight into an argv for a subprocess. Anything not on the list is refused.
ASR_MODELS = [
    "tiny", "tiny.en", "base", "base.en",
    "small", "small.en", "medium", "medium.en",
]
DEFAULT_ASR_MODEL = "small"

# Aliases, not pinned ids — `claude` resolves each to the current model, so
# this list doesn't go stale every time a new one ships.
CLAUDE_MODELS = ["opus", "sonnet", "haiku"]
DEFAULT_CLAUDE_MODEL = "opus"

CLAUDE_BIN = shutil.which("claude")
CODEX_BIN = shutil.which("codex")
AUTH_RE = re.compile(r"authenticat|oauth|login|credential|not logged in", re.I)

CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

# Codex has no "list models" command, so this is read out of the shipped
# binary's own strings and curated down to the ones worth offering here.
# Whatever `~/.codex/config.toml` says is always included and always leads the
# list, so a model newer than this file stays reachable without an edit.
CODEX_MODELS = [
    "gpt-6-astra",
    "gpt-6",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.2",
    "gpt-5.1-codex-max",
]


def codex_model() -> str:
    """Report the model Codex is configured to use.

    Deliberately read rather than chosen: Codex has no "list models" command,
    so any dropdown here would be a guess that goes stale. `~/.codex/config.toml`
    is the same thing Codex itself reads, and changing the model there is the
    normal Codex way to do it.
    """
    try:
        for line in CODEX_CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("model") and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() == "model":
                    return value.strip().strip('"\'')
    except Exception:  # noqa: BLE001 - no config, unreadable, whatever
        pass
    return "default"


def codex_logged_in() -> bool:
    """`codex login status` exits 0 and says who you're signed in as."""
    if not CODEX_BIN:
        return False
    try:
        proc = subprocess.run(
            [CODEX_BIN, "login", "status"],
            capture_output=True, text=True, timeout=20,
        )
        # Codex reports this on stderr, not stdout.
        blob = f"{proc.stdout}{proc.stderr}".lower()
        return proc.returncode == 0 and "logged in" in blob
    except Exception:  # noqa: BLE001
        return False


def backend_state(name: str) -> dict:
    if name == "claude":
        return {
            "name": "claude",
            "label": "Claude",
            "installed": bool(CLAUDE_BIN),
            "logged_in": claude_logged_in(),
            "models": CLAUDE_MODELS,
            "default_model": DEFAULT_CLAUDE_MODEL,
            "install_hint": "npm install -g @anthropic-ai/claude-code",
        }
    return {
        "name": "codex",
        "label": "ChatGPT (Codex)",
        "installed": bool(CODEX_BIN),
        "logged_in": codex_logged_in(),
        # Config value first so it's the default, then the rest.
        "models": [codex_model()] + [m for m in CODEX_MODELS if m != codex_model()],
        "default_model": codex_model(),
        "install_hint": "npm install -g --prefix ~/.local @openai/codex",
    }


def claude_logged_in() -> bool:
    """Ask the CLI whether it's signed in.

    `auth status` is a local credential check — no request to the model, so
    this is free and fast enough to call on every page load. Anything
    unexpected counts as "not signed in": the cost of a false negative is an
    extra Sign in button, the cost of a false positive is a confusing failure
    several minutes into a run.
    """
    if not CLAUDE_BIN:
        return False
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=20,
        )
        return json.loads(proc.stdout).get("loggedIn") is True
    except Exception:  # noqa: BLE001 - missing, slow, or unparseable all mean "no"
        return False

# job_id -> {state, step, log, out_dir, error, link, notes, screenshots}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def slugify(filename: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", filename)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return slug or "video"


def open_terminal(command: str) -> bool:
    """Open a terminal window running `command`.

    Sign-in is an OAuth flow: it wants a real terminal and a browser, and the
    password is typed into Anthropic's page, never into this app. So the app's
    whole job here is to put that terminal one click away — it never sees or
    handles a credential.

    `command` is always one of the fixed constants below, never anything that
    came in over the wire.
    """
    if sys.platform == "darwin":
        subprocess.Popen([
            "osascript",
            "-e", f'tell application "Terminal" to do script "{command}"',
            "-e", 'tell application "Terminal" to activate',
        ])
        return True

    for term, args in (
        ("x-terminal-emulator", ["-e"]),
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xterm", ["-e"]),
    ):
        if shutil.which(term):
            subprocess.Popen([term, *args, "bash", "-lc", f"{command}; exec bash"])
            return True
    return False


def pick_asr_model(value: str | None) -> str:
    """Fall back to the default rather than erroring — an unknown model in a
    query string shouldn't cost someone their upload."""
    value = (value or "").strip()
    return value if value in ASR_MODELS else DEFAULT_ASR_MODEL


def pick_claude_model(value: str | None) -> str:
    value = (value or "").strip()
    return value if value in CLAUDE_MODELS else DEFAULT_CLAUDE_MODEL


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
        # Analysis runs as a second, separate job on the same record: the
        # pipeline can be "done" while the teardown is still being written.
        "analysis_state": "idle",  # idle | running | done | error
        "analysis_error": None,
        "analysis_model": "",
        "analysis_backend": "",
    }


def build_prompt(job: dict, local: bool = False) -> str:
    """Assemble the teardown prompt.

    `local=True` targets `claude -p`, which has no attachments but can open
    files itself — so the images become paths to read rather than things
    claimed to be attached. Everything below that line is identical, which is
    the point: the two routes must not drift into asking for different work.
    """
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

    lines = [f"Video: {job.get('link') or '(not given)'}"]
    if local:
        out_rel = job["out_dir"]
        lines.append(
            "Read these files before answering — they are the evidence, and "
            "every claim has to cite them:"
        )
        if (out_dir_abs / "sheet.jpg").exists():
            lines.append(
                f"  {out_rel}/sheet.jpg — all {len(frames)} frames in timestamp "
                "order, hook denser than body, each tile labelled H/B + timestamp"
            )
        if frames:
            lines.append(
                f"  {out_rel}/frames/ — the same frames full-resolution, plus "
                "index.tsv mapping each one to its timestamp. Open individual "
                "frames from here when the sheet is too small to read."
            )
        for shot in screenshots:
            lines.append(f"  {out_rel}/screenshots/{shot} — viewer comments")
        if not frames:
            lines.append("  (no frames were extracted — work from the transcript alone)")
    else:
        lines.append(
            f"Attached to this message: {sheet_note or '(no frames extracted)'}"
        )
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


def analysis_command(backend: str, model: str, prompt: str, out_dir: str,
                     result_file: Path) -> list[str]:
    """Build the argv for one backend.

    Both are told to read the frames off disk and both are held to read-only:
    the analysis opens images and a transcript, it has no business editing the
    repo or running anything.
    """
    if backend == "claude":
        return [
            CLAUDE_BIN, "-p", prompt,
            "--model", model,
            "--allowedTools", "Read", "Glob",
            "--permission-mode", "dontAsk",
        ]

    cmd = [
        CODEX_BIN, "exec", prompt,
        "--sandbox", "read-only",
        # out/ is gitignored and the app may well be run outside a checkout.
        "--skip-git-repo-check",
        # Capture the final message directly instead of scraping it out of the
        # event log, which carries reasoning and tool chatter as well.
        "--output-last-message", str(result_file),
    ]
    sheet = REPO_ROOT / out_dir / "sheet.jpg"
    if sheet.exists():
        # Attaching beats making it shell out to find and open the file.
        cmd += ["--image", str(sheet)]
    if model and model != "default":
        cmd += ["--model", model]
    return cmd


def run_analysis(job_id: str, backend: str, model: str) -> None:
    """Run the teardown through a local CLI and save teardown.md.

    Shelling out to a CLI rather than calling an API is the whole point: both
    CLIs are already signed in to a subscription, so there's no API key stored
    here and nothing billed per video. The cost is that an expired session
    arrives as a subprocess failure, which is why that one case gets
    translated into something you can act on.
    """
    job = JOBS[job_id]
    job["analysis_state"] = "running"
    job["analysis_error"] = None
    job["analysis_model"] = model
    job["analysis_backend"] = backend

    result_file = REPO_ROOT / job["out_dir"] / ".codex-last-message.txt"
    try:
        prompt = build_prompt(job, local=True)
        cmd = analysis_command(backend, model, prompt, job["out_dir"], result_file)
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800,
        )

        if backend == "codex" and result_file.exists():
            output = result_file.read_text(encoding="utf-8").strip()
            result_file.unlink(missing_ok=True)
        else:
            output = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0 or not output:
            detail = stderr or output or f"exited with status {proc.returncode}"
            if AUTH_RE.search(detail):
                label = "Claude Code" if backend == "claude" else "Codex"
                job["analysis_state"] = "error"
                job["analysis_error"] = (
                    f"{label} isn't signed in. Press the Sign in button above, "
                    "then press Analyze again."
                )
                return
            job["analysis_state"] = "error"
            job["analysis_error"] = detail[:1000]
            return

        dest = REPO_ROOT / job["out_dir"] / "teardown.md"
        dest.write_text(output, encoding="utf-8")
        job["analysis_state"] = "done"
    except subprocess.TimeoutExpired:
        job["analysis_state"] = "error"
        job["analysis_error"] = "analysis timed out after 30 minutes"
    except Exception as e:  # noqa: BLE001 - surface it, don't swallow it
        job["analysis_state"] = "error"
        job["analysis_error"] = str(e)
    finally:
        result_file.unlink(missing_ok=True)


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

        if path == "/capabilities":
            self._send_json({
                "asr_models": ASR_MODELS,
                "default_asr_model": DEFAULT_ASR_MODEL,
                "backends": [backend_state("claude"), backend_state("codex")],
                "has_ffmpeg": bool(shutil.which("ffmpeg")),
                "has_ytdlp": bool(shutil.which("yt-dlp")),
            })
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
                "analysis_state": job["analysis_state"],
                "analysis_error": job["analysis_error"],
                "analysis_model": job["analysis_model"],
                "analysis_backend": job["analysis_backend"],
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
                teardown = out_dir_abs / "teardown.md"
                payload["has_teardown"] = teardown.exists()
                if teardown.exists():
                    payload["teardown"] = teardown.read_text(encoding="utf-8")
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

            asr_model = pick_asr_model(self.headers.get("X-Asr-Model"))

            job_id = slug
            with JOBS_LOCK:
                JOBS[job_id] = new_job(out_dir_hint=out_dir_rel)
            args = ["--skip-download", "-o", out_dir_rel, "--sheet",
                    "--model", asr_model]
            threading.Thread(
                target=run_pipeline,
                args=(job_id, args, out_dir_rel),
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
            asr_model = pick_asr_model(data.get("asr_model"))

            job_id = f"job_{int(time.time() * 1000)}"
            with JOBS_LOCK:
                JOBS[job_id] = new_job()
                JOBS[job_id]["link"] = url
            args = [url, "--sheet", "--model", asr_model]
            threading.Thread(
                target=run_pipeline,
                args=(job_id, args, None),
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

        if path == "/login":
            data = self._read_json_body()
            backend = (data.get("backend") or "claude").strip()
            if backend not in ("claude", "codex"):
                self._send_json({"error": f"unknown backend: {backend}"}, 400)
                return

            state = backend_state(backend)
            if not state["installed"]:
                self._send_json(
                    {"error": f"{state['label']} isn't installed — {state['install_hint']}"},
                    409,
                )
                return

            # --claudeai is explicit so this signs in to the subscription
            # rather than to Console, which bills per request. Codex's own
            # login defaults to Sign in with ChatGPT.
            command = (
                "claude auth login --claudeai" if backend == "claude" else "codex login"
            )
            if not open_terminal(command):
                self._send_json(
                    {"error": f"couldn't open a terminal — run `{command}` yourself"}, 500
                )
                return
            self._send_json({"ok": True})
            return

        if path == "/auth-status":
            self._send_json({
                "backends": [backend_state("claude"), backend_state("codex")],
            })
            return

        if path == "/analyze":
            data = self._read_json_body()
            job = self._job_or_404(data.get("job_id", ""))
            if job is None:
                return
            if job["state"] != "done":
                self._send_json({"error": "the pipeline hasn't finished yet"}, 409)
                return
            backend = (data.get("backend") or "claude").strip()
            if backend not in ("claude", "codex"):
                self._send_json({"error": f"unknown backend: {backend}"}, 400)
                return

            state = backend_state(backend)
            if not state["installed"]:
                self._send_json(
                    {"error": f"{state['label']} isn't installed — {state['install_hint']}"},
                    409,
                )
                return
            if not state["logged_in"]:
                self._send_json(
                    {"error": f"{state['label']} isn't signed in — press Sign in first"},
                    409,
                )
                return
            if job["analysis_state"] == "running":
                self._send_json({"error": "an analysis is already running"}, 409)
                return

            requested = (data.get("model") or "").strip()
            if backend == "claude":
                model = pick_claude_model(requested)
            else:
                # Allowlisted for the same reason as everything else here: it
                # goes into an argv.
                model = (
                    requested if requested in state["models"]
                    else state["default_model"]
                )

            threading.Thread(
                target=run_analysis,
                args=(data.get("job_id"), backend, model),
                daemon=True,
            ).start()
            self._send_json({"ok": True, "backend": backend, "model": model})
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


def already_running(url: str) -> bool:
    """Is the thing on our port this app, or something else entirely?"""
    try:
        # Generous: this check runs while the machine may be busy transcribing,
        # and a timeout here misreports the app as a foreign process.
        with urlopen(f"{url}capabilities", timeout=8) as resp:
            return "backends" in json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreachable or not us
        return False


def main() -> None:
    if not VPIPE.exists():
        print(f"error: {VPIPE} not found — run this from the video-pipeline repo", file=sys.stderr)
        sys.exit(1)

    OUT_ROOT.mkdir(exist_ok=True)
    url = f"http://{HOST}:{PORT}/"

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # Almost always this is the app already running — starting it twice is
        # an easy thing to do. Opening the page you wanted beats a traceback.
        if already_running(url):
            print(f"vpipe-app is already running at {url} — opening it")
            webbrowser.open(url)
            return
        print(
            f"error: port {PORT} is in use by something else.\n"
            f"       Close it, or find it with:  lsof -nP -iTCP:{PORT} -sTCP:LISTEN",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"vpipe-app running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")


if __name__ == "__main__":
    main()
