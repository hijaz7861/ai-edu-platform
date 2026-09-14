
#!/usr/bin/env python3

from pathlib import Path
import os, sys, subprocess, json, shutil, time, socket
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
RUN = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

REPORT = ROOT / f"production_master_{RUN}.json"
BACKUP = ROOT / ".self-healing" / "backups" / f"production_{RUN}"
LOGDIR = ROOT / "logs"

BACKUP.mkdir(parents=True, exist_ok=True)
LOGDIR.mkdir(parents=True, exist_ok=True)

result = {
    "run": RUN,
    "project": str(ROOT),
    "started_utc": datetime.now(timezone.utc).isoformat(),
    "stages": [],
    "repairs": [],
    "deployment": {},
    "public_runtime": {},
    "final_status": "FAILED",
}


def record(stage, status, detail=""):
    x = {
        "stage": stage,
        "status": status,
        "detail": detail
    }
    result["stages"].append(x)
    print(f"[{status}] {stage} — {detail}")


def shell(cmd, timeout=600):
    try:
        p = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 999, "", repr(e)


def backup():
    for name in [
        "app.py",
        "db.py",
        "requirements.txt",
        "render.yaml",
        "Procfile",
    ]:
        p = ROOT / name
        if p.exists():
            dest = BACKUP / name
            shutil.copy2(p, dest)

    for directory in ["services", "tests"]:
        src = ROOT / directory
        if src.exists():
            shutil.copytree(
                src,
                BACKUP / directory,
                dirs_exist_ok=True
            )

    record(
        "BACKUP",
        "PASS",
        str(BACKUP)
    )


def ensure_production_files():
    changed = []

    req = ROOT / "requirements.txt"

    if req.exists():
        text = req.read_text(encoding="utf-8")
    else:
        text = ""

    if "gunicorn" not in text.lower():
        with req.open("a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("gunicorn>=22,<24\n")
        changed.append("added gunicorn")

    # Render production descriptor.
    render = ROOT / "render.yaml"

    render_text = """services:
  - type: web
    name: ai-edu-platform
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn --bind 0.0.0.0:$PORT app:app
    healthCheckPath: /health
"""

    if not render.exists():
        render.write_text(
            render_text,
            encoding="utf-8"
        )
        changed.append("created render.yaml")

    # Procfile fallback for providers using it.
    proc = ROOT / "Procfile"

    if not proc.exists():
        proc.write_text(
            "web: gunicorn --bind 0.0.0.0:$PORT app:app\n",
            encoding="utf-8"
        )
        changed.append("created Procfile")

    for x in changed:
        result["repairs"].append(x)

    record(
        "PRODUCTION CONFIG",
        "PASS",
        ", ".join(changed) if changed else "already configured"
    )


def syntax():
    files = list(ROOT.rglob("*.py"))

    for p in files:
        if ".git" in p.parts or ".self-healing" in p.parts:
            continue

        rc, out, err = shell(
            [sys.executable, "-m", "py_compile", str(p)]
        )

        if rc:
            record(
                "SYNTAX",
                "FAIL",
                f"{p}: {err[-1000:]}"
            )
            return False

    record(
        "SYNTAX",
        "PASS",
        f"{len(files)} Python files checked"
    )
    return True


def pytest():
    rc, out, err = shell(
        [sys.executable, "-m", "pytest", "-q"],
        timeout=900
    )

    if rc:
        record(
            "PYTEST",
            "FAIL",
            (out + "\n" + err)[-2500:]
        )
        return False

    record(
        "PYTEST",
        "PASS",
        out[-1500:]
    )
    return True


def db_integrity():
    db = ROOT / "storage" / "platform.db"

    if not db.exists():
        record(
            "DATABASE",
            "FAIL",
            "platform.db not found"
        )
        return False

    import sqlite3

    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=ON")

    integrity = c.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0]

    fk = c.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()

    c.close()

    ok = integrity == "ok" and len(fk) == 0

    record(
        "DATABASE",
        "PASS" if ok else "FAIL",
        f"integrity={integrity}, foreign_keys={len(fk)}"
    )

    return ok


def github():
    rc, out, err = shell(
        ["git", "status", "--short"]
    )

    if rc:
        record("GIT", "FAIL", err[-1000:])
        return False

    branch_rc, branch, branch_err = shell(
        ["git", "branch", "--show-current"]
    )

    log_rc, commit, log_err = shell(
        ["git", "log", "-1", "--oneline"]
    )

    record(
        "GIT",
        "PASS",
        f"branch={branch.strip()} commit={commit.strip()}"
    )

    return True


def render_detect():
    """
    We do NOT manufacture credentials.

    Existing Render credentials/configuration are detected only.
    """

    token_names = [
        "RENDER_API_KEY",
        "RENDER_API_TOKEN",
    ]

    configured = any(
        os.environ.get(x)
        for x in token_names
    )

    # render.yaml itself is valid deployment configuration,
    # but it does not prove authentication.
    render_file = (ROOT / "render.yaml").exists()

    if configured:
        record(
            "RENDER PROVIDER",
            "PASS",
            "Render credential detected in environment"
        )
        return True

    record(
        "RENDER PROVIDER",
        "WAITING",
        "render.yaml exists, but no Render credential is available in this runtime"
    )

    result["deployment"] = {
        "provider": "render",
        "configured": render_file,
        "authenticated": False,
        "deployed": False,
    }

    return False


def render_deploy():
    """
    Deployment is attempted only if an existing Render credential
    is available.

    The runner never prints the credential.
    """

    key = (
        os.environ.get("RENDER_API_KEY")
        or os.environ.get("RENDER_API_TOKEN")
    )

    if not key:
        return False, "Render authentication unavailable"

    # Use Render REST API to discover existing services.
    # No arbitrary remote shell.
    import urllib.request

    req = urllib.request.Request(
        "https://api.render.com/v1/services?limit=100",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()

        data = json.loads(raw)

    except Exception as e:
        return False, f"Render API verification failed: {e}"

    services = data if isinstance(data, list) else []

    target = None

    for item in services:
        service = item.get("service", item)

        name = str(
            service.get("name", "")
        ).lower()

        if name in (
            "ai-edu-platform",
            "ai_edu_platform"
        ):
            target = service
            break

    if not target:
        return False, (
            "Render authenticated, but no existing "
            "ai-edu-platform service was found. "
            "No blind service creation performed."
        )

    service_id = target.get("id")

    if not service_id:
        return False, "Render service has no ID"

    # Trigger deployment.
    deploy_req = urllib.request.Request(
        f"https://api.render.com/v1/services/{service_id}/deploys",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(
            deploy_req,
            timeout=30
        ) as r:
            deploy_raw = r.read().decode()
            deploy_data = json.loads(deploy_raw)

    except Exception as e:
        return False, f"deployment trigger failed: {e}"

    result["deployment"] = {
        "provider": "render",
        "service_id": service_id,
        "triggered": True,
        "deployment_response": deploy_data,
    }

    return True, f"deployment triggered for service {service_id}"


def public_verify():
    """
    Verify the deployed public URL.

    URL can be supplied by:
      AI_EDU_PUBLIC_URL

    or discovered from Render service metadata.
    """

    public_url = os.environ.get(
        "AI_EDU_PUBLIC_URL"
    )

    if not public_url:
        record(
            "PUBLIC RUNTIME",
            "WAITING",
            "AI_EDU_PUBLIC_URL is not configured"
        )
        return False

    public_url = public_url.rstrip("/")

    health_url = public_url + "/health"

    try:
        req = Request(
            health_url,
            method="GET"
        )

        with urlopen(req, timeout=30) as r:
            code = r.status
            body = r.read().decode(errors="replace")

        if code != 200:
            record(
                "PUBLIC HEALTH",
                "FAIL",
                f"HTTP {code}: {body[:500]}"
            )
            return False

        record(
            "PUBLIC HEALTH",
            "PASS",
            f"HTTP {code} {health_url}"
        )

    except Exception as e:
        record(
            "PUBLIC HEALTH",
            "FAIL",
            repr(e)
        )
        return False

    # Public institution -> returned ID -> class E2E.
    suffix = int(time.time() * 1000)

    payload = {
        "name": f"Production Runtime {suffix}",
        "code": f"PROD-E2E-{suffix}",
        "type": "school",
    }

    try:
        req = Request(
            public_url + "/institutions",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json"
            }
        )

        with urlopen(req, timeout=30) as r:
            body = json.loads(
                r.read().decode()
            )
            code = r.status

    except HTTPError as e:
        return False, f"institution HTTP {e.code}"

    except Exception as e:
        return False, repr(e)

    if code != 201 or not body.get("id"):
        record(
            "PUBLIC INSTITUTION",
            "FAIL",
            f"HTTP {code}: {body}"
        )
        return False

    institution_id = body["id"]

    record(
        "PUBLIC INSTITUTION",
        "PASS",
        f"returned_id={institution_id}"
    )

    class_payload = {
        "name": "Production E2E Class",
        "academic_year": "2026-2027",
    }

    class_url = (
        public_url
        + f"/institutions/{institution_id}/classes"
    )

    try:
        req = Request(
            class_url,
            data=json.dumps(class_payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json"
            }
        )

        with urlopen(req, timeout=30) as r:
            class_body = json.loads(
                r.read().decode()
            )
            class_code = r.status

    except HTTPError as e:
        record(
            "PUBLIC CLASS",
            "FAIL",
            f"HTTP {e.code}"
        )
        return False

    except Exception as e:
        record(
            "PUBLIC CLASS",
            "FAIL",
            repr(e)
        )
        return False

    ok = class_code == 201 and bool(
        class_body.get("id")
    )

    record(
        "PUBLIC CLASS",
        "PASS" if ok else "FAIL",
        f"HTTP {class_code}: {class_body}"
    )

    if not ok:
        return False

    result["public_runtime"] = {
        "url": public_url,
        "health": True,
        "institution": institution_id,
        "class": class_body.get("id"),
    }

    return True


# ================================================================
# MASTER PIPELINE
# ================================================================

try:

    print("=" * 78)
    print("AI EDUCATION PLATFORM — MASTER PRODUCTION SYSTEM")
    print("=" * 78)

    backup()

    ensure_production_files()

    if not syntax():
        raise SystemExit(20)

    if not pytest():
        raise SystemExit(21)

    if not db_integrity():
        raise SystemExit(22)

    if not github():
        raise SystemExit(23)

    render_auth = render_detect()

    # Deploy only when provider is actually authenticated.
    if render_auth:

        ok, detail = render_deploy()

        record(
            "CLOUD DEPLOY",
            "PASS" if ok else "FAIL",
            detail
        )

        if not ok:
            raise SystemExit(30)

        # Give deployment time to become reachable.
        print("Waiting for cloud deployment...")
        time.sleep(20)

        public_ok = public_verify()

        if not public_ok:
            raise SystemExit(31)

    else:
        record(
            "CLOUD DEPLOY",
            "WAITING",
            "Provider configuration/authentication required"
        )

        # Do NOT claim production live.
        result["final_status"] = "BUILD_VERIFIED_CLOUD_WAITING"

        result["finished_utc"] = datetime.now(
            timezone.utc
        ).isoformat()

        REPORT.write_text(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

        print()
        print("=" * 78)
        print("BUILD + TEST VERIFICATION COMPLETE")
        print("CLOUD DEPLOYMENT WAITING FOR VERIFIED PROVIDER AUTH")
        print("REPORT:", REPORT)
        print("=" * 78)

        raise SystemExit(0)

    result["final_status"] = "PRODUCTION_PUBLIC_VERIFIED"

    result["finished_utc"] = datetime.now(
        timezone.utc
    ).isoformat()

    REPORT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print()
    print("=" * 78)
    print("PRODUCTION PUBLIC VERIFICATION: PASS")
    print("PUBLIC URL:", result["public_runtime"].get("url"))
    print("REPORT:", REPORT)
    print("=" * 78)

except SystemExit:
    raise

except Exception as e:

    result["fatal_error"] = repr(e)
    result["finished_utc"] = datetime.now(
        timezone.utc
    ).isoformat()

    REPORT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print()
    print("=" * 78)
    print("MASTER PRODUCTION SYSTEM FAILED")
    print("ERROR:", repr(e))
    print("REPORT:", REPORT)
    print("=" * 78)

    raise
