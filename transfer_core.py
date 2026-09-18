"""Local, versioned project transfer. Never writes to the source Codex home."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import ntpath
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone

VERSION = 1
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".next", ".cache"}
SECRET_NAMES = {"auth.json", "credentials.json", "id_rsa", "id_ed25519", ".env"}


def default_home():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def plain_windows_path(path):
    value = str(path)
    prefix = chr(92) * 2 + '?' + chr(92)
    if value[:8].lower() == (prefix + 'UNC' + chr(92)).lower():
        return chr(92) * 2 + value[8:]
    return value.removeprefix(prefix)


def normalized(path):
    return ntpath.normcase(ntpath.normpath(plain_windows_path(path)))


def belongs(path, root):
    p, r = normalized(path), normalized(root)
    return p == r or p.startswith(r.rstrip("\\") + "\\")


@contextmanager
def database(path, read_only=False):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) if read_only else sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def readonly(path):
    return database(path, True)


def linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 1024)


def scan_sessions(home, project):
    home, project = Path(home).resolve(), Path(project).resolve()
    if not home.is_dir() or not project.is_dir():
        raise ValueError("프로젝트와 Codex 저장소 폴더를 확인하세요.")
    rows, warnings = {}, []
    dbs = sorted(home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if dbs:
        with readonly(dbs[0]) as db:
            db.row_factory = sqlite3.Row
            for r in db.execute("SELECT * FROM threads"):
                r = dict(r)
                if belongs(r["cwd"], project):
                    rows[r["id"]] = r
    for folder in ("sessions", "archived_sessions"):
        for path in (home / folder).rglob("*.jsonl"):
            if linked(path):
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    record = json.loads(f.readline())
                meta = record.get("payload", {})
                if record.get("type") != "session_meta" or not belongs(meta.get("cwd", ""), project):
                    continue
                sid = str(uuid.UUID(meta.get("id") or meta.get("session_id")))
                row = rows.setdefault(sid, {"id": sid, "cwd": meta["cwd"], "title": sid,
                                      "rollout_path": str(path), "archived": folder == "archived_sessions"})
                # Recover a stale index only from matching session metadata inside this home.
                indexed = Path(row['rollout_path'])
                if not indexed.is_file() or not belongs(indexed.resolve(), home) or linked(indexed):
                    if belongs(path.resolve(), home):
                        row['rollout_path'] = str(path)
            except (OSError, ValueError, TypeError) as exc:
                warnings.append(f"대화 파일 읽기 실패: {path.name}: {exc}")
    result = []
    for r in rows.values():
        path = Path(r["rollout_path"])
        if not path.is_file() or not belongs(path.resolve(), home) or linked(path):
            warnings.append(f"대화 원본 누락/외부 경로: {r['id']}")
            continue
        uuid.UUID(r["id"])
        result.append({"id": r["id"], "title": r.get("title") or r["id"],
                       "cwd": r["cwd"], "path": str(path), "archived": bool(r.get("archived")),
                       "history_mode": r.get("history_mode", "legacy")})
    return sorted(result, key=lambda r: r["title"]), warnings


def project_files(project, include_git=False):
    project = Path(project).resolve()
    if (project / ".git").is_file():
        raise ValueError("Git worktree는 이 버전에서 지원하지 않습니다. 일반 체크아웃으로 옮긴 뒤 내보내세요.")
    skipped = []
    files = []
    for root, dirs, names in os.walk(project, followlinks=False):
        root = Path(root)
        for name in list(dirs):
            p = root / name
            if name in SKIP_DIRS or (name == ".git" and not include_git) or linked(p):
                dirs.remove(name)
                skipped.append(p.relative_to(project).as_posix() + "/")
        for name in names:
            p = root / name
            low = name.lower()
            if linked(p) or low in SECRET_NAMES or low.startswith(".env.") and low not in {".env.example", ".env.sample"} or low.endswith((".pem", ".pfx", ".key", ".codextransfer")):
                skipped.append(p.relative_to(project).as_posix())
            elif p.is_file():
                files.append(p)
    return files, skipped


def filtered_database(source, output, ids):
    """Copy only known per-thread tables; retain schema and migration records."""
    with readonly(source) as src, database(output) as dst:
        src.execute("BEGIN")
        schema = src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        for kind, name, sql in schema:
            if kind == "table":
                dst.execute(sql)
        keep = {"threads": "id", "thread_dynamic_tools": "thread_id", "thread_turns": "thread_id",
                "thread_items": "thread_id", "thread_realtime_items": "thread_id"}
        for kind, name, _ in schema:
            if kind != "table" or name not in {*keep, "_sqlx_migrations"}:
                continue
            if name == "_sqlx_migrations":
                data = src.execute('SELECT * FROM "_sqlx_migrations"')
            else:
                # Parameter count stays bounded even for large collections.
                data = src.execute(f'SELECT * FROM "{name}"')
            columns = [d[0] for d in data.description]
            for row in data:
                if name != "_sqlx_migrations" and row[columns.index(keep[name])] not in ids:
                    continue
                values = list(row)
                if name == "threads":
                    for field, value in {"project_id": None, "thread_section_id": None, "is_pinned": 0}.items():
                        if field in columns:
                            values[columns.index(field)] = value
                dst.execute(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in values)})', values)
        for kind, name, sql in schema:
            if kind == "index":
                dst.execute(sql)


def export_bundle(project, home, output, note="", include_git=False, progress=lambda s: None):
    project, home, output = Path(project).resolve(), Path(home).resolve(), Path(output).resolve()
    if not project.is_dir() or not home.is_dir():
        raise ValueError("프로젝트와 Codex 저장소 폴더를 확인하세요.")
    if project == home or home.is_relative_to(project) or project.is_relative_to(home):
        raise ValueError("Codex 저장소와 프로젝트는 서로 독립된 폴더여야 합니다.")
    if output.is_relative_to(project):
        raise ValueError("패키지는 프로젝트 폴더 밖에 저장하세요.")
    if output.exists():
        raise FileExistsError("같은 이름의 패키지가 있습니다. 새 이름을 선택하세요.")
    sessions, warnings = scan_sessions(home, project)
    if warnings:
        raise ValueError("완전한 백업을 만들 수 없습니다.\n" + "\n".join(warnings[:10]))
    files, skipped = project_files(project, include_git)
    manifest = {"format": "codex-project-transfer", "version": VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(), "source_project": str(project),
                "sessions": [], "files": {}, "excluded": skipped,
                "limitations": ["로그인·전역 설정·전역 메모리·외부 첨부파일은 제외", "데스크톱 채팅 목록 자동 통합 미지원"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-export-", dir=output.parent) as tmp:
        tmp = Path(tmp)
        archive = tmp / "package.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            def add_file(path, name):
                before = path.stat()
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as src, z.open(name, "w", force_zip64=True) as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        dst.write(block); digest.update(block); size += len(block)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"작업 중 파일이 변경됐습니다. 작업을 멈춘 후 다시 내보내세요: {path.name}")
                manifest["files"][name] = {"sha256": digest.hexdigest(), "size": size}
            def add_bytes(data, name):
                z.writestr(name, data)
                manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for i, path in enumerate(files):
                progress(f"프로젝트 파일 {i + 1}/{len(files)}")
                add_file(path, "project/" + path.relative_to(project).as_posix())
            for session in sessions:
                progress("대화 저장: " + session["title"][:50])
                sid = session["id"]
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
                name = f"codex/sessions/rollout-{stamp}-{sid}.jsonl"
                add_file(Path(session["path"]), name)
                manifest["sessions"].append({k: v for k, v in session.items() if k != "path"} | {"archive_path": name})
            ids = {s["id"] for s in sessions}
            for pattern in ("state_*.sqlite", "thread_history_*.sqlite"):
                candidates = sorted(home.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    progress("대화 인덱스 저장")
                    db = tmp / candidates[0].name
                    filtered_database(candidates[0], db, ids)
                    add_file(db, "codex/" + db.name)
            if (home / "AGENTS.md").is_file():
                add_file(home / "AGENTS.md", "reference/global-AGENTS.md")
            add_bytes(note.encode("utf-8"), "reference/HANDOFF.md")
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        # Atomic publication; never replace an existing package.
        with output.open("xb") as target, archive.open("rb") as source:
            try:
                shutil.copyfileobj(source, target)
            except BaseException:
                target.close(); output.unlink(missing_ok=True); raise
    return manifest


def safe_name(name):
    p = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
    if not name or "\\" in name or p.is_absolute() or p.as_posix() != name:
        raise ValueError("잘못된 압축 경로")
    for part in p.parts:
        if part in {".", ".."} or any(ord(c) < 32 or c in ':<>"|?*' for c in part) or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved:
            raise ValueError("안전하지 않은 압축 경로")
    return p


def inspect_bundle(path):
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > 200000:
            raise ValueError("파일 수 제한 초과")
        names = set()
        total = 0
        for info in infos:
            safe_name(info.filename)
            name = info.filename.casefold()
            if name in names or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("중복 경로 또는 링크가 포함된 패키지")
            names.add(name); total += info.file_size
            if total > 100 * 1024**3:
                raise ValueError("압축 해제 용량 제한(100 GiB) 초과")
        if z.getinfo("manifest.json").file_size > 32 * 1024**2:
            raise ValueError("패키지 목록 크기 제한 초과")
        m = json.loads(z.read("manifest.json"))
        if m.get("format") != "codex-project-transfer" or m.get("version") != VERSION:
            raise ValueError("지원하지 않는 패키지 형식")
        if {i.filename for i in infos} != {*m["files"], "manifest.json"}:
            raise ValueError("패키지 파일 목록 불일치")
        for name, entry in m["files"].items():
            if not name.startswith(("project/", "codex/", "reference/")) or z.getinfo(name).file_size != entry["size"]:
                raise ValueError("잘못된 패키지 파일 정보")
            if name.startswith("codex/") and not re.fullmatch(r"codex/(?:sessions/rollout-[0-9T-]+-[0-9a-f-]{36}\.jsonl|(?:state|thread_history)_\d+\.sqlite)", name):
                raise ValueError("허용되지 않은 Codex 저장소 파일")
        ids = set()
        for s in m["sessions"]:
            uuid.UUID(s["id"])
            if s["id"] in ids or not re.fullmatch(r"codex/sessions/rollout-[0-9T-]+-" + re.escape(s["id"]) + r"\.jsonl", s["archive_path"]) or s["archive_path"] not in m["files"]:
                raise ValueError("잘못된 대화 정보")
            ids.add(s["id"])
        return m


def remap_cwd(value, old, new):
    if not belongs(value, old):
        return value
    relative = ntpath.relpath(plain_windows_path(value), plain_windows_path(old))
    return str(Path(new).joinpath(*relative.split("\\"))) if relative != "." else str(new)


def restore_bundle(bundle, destination, progress=lambda s: None):
    bundle, destination = Path(bundle).resolve(), Path(destination).absolute()
    if destination.exists():
        raise FileExistsError("복원 폴더는 새 폴더여야 합니다. 기존 폴더는 덮어쓰지 않습니다.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.resolve() != destination.parent:
        raise ValueError("복원 상위 폴더에 링크가 있습니다.")
    m = inspect_bundle(bundle)
    required = sum(e["size"] for e in m["files"].values())
    if shutil.disk_usage(destination.parent).free < required * 2 + 100 * 1024**2:
        raise ValueError("복원에 필요한 여유 공간이 부족합니다.")
    with tempfile.TemporaryDirectory(prefix="codex-restore-", dir=destination.parent) as tmp:
        stage = Path(tmp) / "restore"
        stage.mkdir()
        with zipfile.ZipFile(bundle) as z:
            for i, (name, entry) in enumerate(m["files"].items()):
                progress(f"검증 및 복원 {i + 1}/{len(m['files'])}")
                p = stage.joinpath(*safe_name(name).parts)
                p.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with z.open(name) as src, p.open("xb") as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        digest.update(block); dst.write(block)
                if digest.hexdigest() != entry["sha256"]:
                    raise ValueError(f"파일 무결성 검증 실패: {name}")
        (stage / "project").mkdir(exist_ok=True)
        (stage / "codex").mkdir(exist_ok=True)
        final_project = destination / "project"
        for s in m["sessions"]:
            path = stage / s["archive_path"]
            edited = path.with_suffix(".new")
            with path.open(encoding="utf-8") as src, edited.open("w", encoding="utf-8", newline="\n") as dst:
                for line in src:
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    if event.get("type") in {"session_meta", "turn_context"} and isinstance(payload, dict) and "cwd" in payload:
                        payload["cwd"] = remap_cwd(payload["cwd"], m["source_project"], final_project)
                    dst.write(json.dumps(event, ensure_ascii=False) + "\n")
            edited.replace(path)
        for db in (stage / "codex").glob("state_*.sqlite"):
            with database(db) as conn:
                conn.execute("PRAGMA trusted_schema=OFF")
                for s in m["sessions"]:
                    conn.execute("UPDATE threads SET cwd=?, rollout_path=?, archived=0 WHERE id=?",
                                 (remap_cwd(s["cwd"], m["source_project"], final_project), str(destination / s["archive_path"]), s["id"]))
        for db in (stage / "codex").glob("thread_history_*.sqlite"):
            # Rollout byte offsets no longer match after cwd conversion; let Codex rebuild.
            with database(db) as conn:
                conn.execute("PRAGMA trusted_schema=OFF")
                for table in ("thread_history_projection_state", "thread_turns", "thread_items", "thread_realtime_items"):
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        conn.execute(f'DELETE FROM "{table}"')
        index = [{"id": s["id"], "thread_name": s["title"], "updated_at": m["created_at"]} for s in m["sessions"]]
        (stage / "codex" / "session_index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in index), encoding="utf-8")
        receipt = m | {"restored_to": str(destination), "restored_at": datetime.now(timezone.utc).isoformat()}
        (stage / "transfer.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        (stage / "READ-ME.txt").write_text("Codex Project Transfer\n\nproject: 프로젝트 파일\ncodex: 독립된 Codex 저장소\nreference: 인수인계와 원본 전역 지침\n\n앱의 '복원된 작업 열기'에서 로그인 후 대화를 이어가세요.\nCodex CLI 설치가 필요합니다. 데스크톱 채팅 목록 자동 통합은 지원하지 않습니다.\n전역 설정, 전역 메모리, 외부 첨부파일과 플러그인은 별도 설정이 필요합니다.\n", encoding="utf-8")
        if destination.exists():
            raise FileExistsError("복원 경로가 작업 도중 생성됐습니다.")
        stage.rename(destination)
    return receipt
