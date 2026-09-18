"""Offline merge into the Windows desktop Codex store (schema-checked)."""
from __future__ import annotations

from contextlib import contextmanager
import csv
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile

from transfer_core import (database, readonly, inspect_bundle, safe_name, normalized,
                           belongs, linked, remap_cwd, plain_windows_path)


def require_codex_closed():
    if os.name != 'nt':
        raise RuntimeError('데스크톱 복원은 Windows에서 실행하세요.')
    result = subprocess.run(['tasklist.exe', '/FO', 'CSV', '/NH'], capture_output=True,
                            text=True, errors='replace', check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    names = {row[0].lower() for row in csv.reader(io.StringIO(result.stdout)) if row}
    if names & {'codex.exe', 'chatgpt.exe', 'codex-app.exe'}:
        raise RuntimeError('Codex Windows 앱과 실행 중인 Codex 작업을 완전히 종료한 뒤 다시 복원하세요. 프로그램이 자동으로 강제 종료하지는 않습니다.')


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.transfer-tmp')
    with temporary.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(temporary, path)


def digest(path):
    if not Path(path).exists():
        return None
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def safe_local_path(path):
    path = Path(path).absolute()
    if not belongs(path.resolve(), path) or normalized(path.resolve()) != normalized(path):
        raise ValueError(f'링크를 통한 복원 경로는 지원하지 않습니다: {path}')
    for parent in (path, *path.parents):
        if parent.exists() and linked(parent):
            raise ValueError(f'복원 경로에 링크가 있습니다: {parent}')
    return path


def columns(db, table):
    if not table.replace('_', '').isalnum():
        raise ValueError('잘못된 테이블 이름')
    return {row[1]: row for row in db.execute(f'PRAGMA table_info("{table}")')}


def rows(db, table):
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')]


def insert_row(db, table, row):
    schema = columns(db, table)
    if not schema:
        raise ValueError(f'지원하지 않는 Codex 저장 형식: {table}')
    for name, info in schema.items():
        if info[3] and info[4] is None and name not in row:
            raise ValueError(f'필수 데이터가 없습니다: {table}.{name}. 양쪽 PC의 Codex 버전을 맞춰주세요.')
    values = {k: v for k, v in row.items() if k in schema}
    quoted = ','.join('"' + k.replace('"', '""') + '"' for k in values)
    db.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in values)})', list(values.values()))


def target_state(home):
    candidates = list(Path(home).glob('state_*.sqlite'))
    if len(candidates) != 1:
        raise ValueError('대상 Codex 데이터베이스를 하나로 식별할 수 없습니다. 대상 PC에서 Codex 앱을 한 번 실행하고 종료한 뒤 저장소 경로를 확인하세요.')
    with readonly(candidates[0]) as db:
        needed = {'threads': {'id', 'cwd', 'rollout_path', 'project_id', 'title'},
                  'projects': {'id', 'name', 'metadata', 'position', 'created_at_ms', 'updated_at_ms'},
                  'project_roots': {'project_id', 'position', 'path'}}
        for table, required in needed.items():
            if not required <= columns(db, table).keys():
                raise ValueError(f'지원하지 않는 대상 Codex 저장 형식: {table}. 대상 Codex 앱을 업데이트하세요.')
    return candidates[0]


def destination_for(manifest, parent):
    name = ntpath.basename(plain_windows_path(manifest['source_project']).rstrip('/\\'))
    safe_name(name)
    if '/' in name or not name:
        raise ValueError('원래 프로젝트 폴더 이름을 확인할 수 없습니다.')
    return safe_local_path(Path(parent).absolute() / name)


def preview_restore(bundle, parent, home):
    manifest = inspect_bundle(bundle)
    destination = destination_for(manifest, parent)
    home = safe_local_path(home)
    if destination.exists():
        raise FileExistsError(f'복원 위치에 같은 이름의 폴더가 있습니다: {destination}\n다른 상위 폴더를 선택하세요. 기존 파일은 덮어쓰지 않습니다.')
    if belongs(destination, home) or belongs(home, destination):
        raise ValueError('프로젝트와 Codex 저장소는 독립된 폴더여야 합니다.')
    dbpath = target_state(home)
    with readonly(dbpath) as db:
        existing = {r[0] for r in db.execute('SELECT id FROM threads')}
    return {'manifest': manifest, 'project_path': str(destination), 'codex_home': str(home),
            'state_file': str(dbpath), 'conflicts': [s['id'] for s in manifest['sessions'] if s['id'] in existing]}


def map_ids(value, ids):
    if isinstance(value, list):
        return [map_ids(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: ids.get(v, v) if k in {'id', 'session_id', 'thread_id', 'parent_thread_id'} and isinstance(v, str)
                else map_ids(v, ids) for k, v in value.items()}
    return value


def rewrite_rollout(source, output, old_root, new_root, ids, expected_id):
    first = True
    with source.open(encoding='utf-8') as src, output.open('w', encoding='utf-8', newline='\n') as dst:
        for line in src:
            event = json.loads(line)
            if first:
                meta = event.get('payload', {})
                if event.get('type') != 'session_meta' or (meta.get('id') or meta.get('session_id')) != expected_id:
                    raise ValueError('대화 원본 ID가 패키지 정보와 다릅니다.')
                first = False
            event = map_ids(event, ids)
            payload = event.get('payload', {})
            if event.get('type') in {'session_meta', 'turn_context'} and isinstance(payload, dict):
                if 'cwd' in payload:
                    payload['cwd'] = remap_cwd(payload['cwd'], old_root, new_root)
                # Retain policy type but limit transferred writable paths to this project.
                policy = payload.get('sandbox_policy')
                if isinstance(policy, dict) and isinstance(policy.get('writable_roots'), list):
                    policy['writable_roots'] = [remap_cwd(v, old_root, new_root) for v in policy['writable_roots'] if belongs(v, old_root)]
            dst.write(json.dumps(event, ensure_ascii=False) + '\n')
        if first:
            raise ValueError('빈 대화 원본입니다.')


def update_global(state, home, destination, project_id, sessions, ids):
    now = int(time.time() * 1000)
    state.setdefault('local-projects', {})[project_id] = {
        'id': project_id, 'name': destination.name, 'rootPaths': [str(destination)], 'createdAt': now, 'updatedAt': now}
    order = state.setdefault('project-order', [])
    if project_id not in order:
        order.append(project_id)
    host = 'local:' + str(home)
    mappings = state.setdefault('app-server-project-id-by-legacy-project-id-by-host', {})
    for key in mappings:
        if key.startswith('local:') and normalized(key[6:]) == normalized(home):
            host = key; break
    mappings.setdefault(host, {})[project_id] = project_id
    for session in sessions:
        sid = ids[session['id']]
        state.setdefault('thread-project-assignments', {})[sid] = {'projectKind': 'local', 'projectId': project_id}
        state.setdefault('thread-writable-roots', {})[sid] = [str(destination)]
        state.setdefault('thread-workspace-root-hints', {})[sid] = str(destination)
    # Older desktop builds use this additional root list.
    key = 'electron-saved-workspace-roots'
    if key in state and str(destination) not in state[key]:
        state[key].append(str(destination))
    return state


@contextmanager
def transfer_lock(home):
    # Windows releases the byte lock automatically after a crash.
    import msvcrt
    path = Path(home) / 'project-transfer.lock'
    with path.open('a+b') as f:
        if f.tell() == 0:
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('다른 복원 작업이 진행 중입니다.') from exc
        try:
            yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def snapshot_database(source, target):
    with readonly(source) as src, database(target) as dst:
        src.backup(dst)


def recover_pending(home, *, process_check=require_codex_closed):
    """Undo an interrupted, uncommitted operation. Preserve extracted project files."""
    process_check()
    home = safe_local_path(home)
    recovered = []
    with transfer_lock(home):
        for journal in (home / 'transfer-backups').glob('*/journal.json'):
            state = json.loads(journal.read_text(encoding='utf-8'))
            if state.get('status') not in {'applying', 'recovery_required'}:
                continue
            # Never rewind a DB that the user has since changed in Codex.
            for item in state['files']:
                target = home.joinpath(*safe_name(item['relative']).parts)
                if item['kind'] != 'database' and digest(target) not in {item['before_hash'], item.get('after_hash')}:
                    raise RuntimeError(f'중단 이후 변경된 파일이 있어 자동 복구를 멈췄습니다: {target}\n백업: {journal.parent}')
            rollback_files(home, journal.parent, state)
            state['status'] = 'rolled_back'
            atomic_json(journal, state)
            recovered.append(str(journal.parent))
    return recovered


def rollback_files(home, backup, journal):
    # Validate everything before undoing any part of a multi-file operation.
    for item in journal['files']:
        target = home.joinpath(*safe_name(item['relative']).parts)
        if item['kind'] == 'database':
            with readonly(target) as db:
                db.row_factory = sqlite3.Row
                for sid, expected in journal.get('expected_rows', {}).items():
                    current = db.execute('SELECT * FROM threads WHERE id=?', (sid,)).fetchone()
                    if current and dict(current) != expected:
                        raise RuntimeError('복원 이후 대화가 변경되어 자동 되돌리기를 중단했습니다: ' + sid)
        elif digest(target) not in {item['before_hash'], item.get('after_hash')}:
            raise RuntimeError('복원 이후 변경된 파일이 있습니다: ' + str(target))
    for item in reversed(journal['files']):
        target = home.joinpath(*safe_name(item['relative']).parts)
        original = backup / item['backup'] if item['backup'] else None
        if item['kind'] == 'database':
            # The merge itself is a SQLite transaction. Remove only inserted rows,
            # preserving unrelated writes even after an unexpected restart.
            with database(target) as db:
                for sid, expected in journal.get('expected_rows', {}).items():
                    db.row_factory = sqlite3.Row
                    current = db.execute('SELECT * FROM threads WHERE id=?', (sid,)).fetchone()
                    if current and dict(current) != expected:
                        raise RuntimeError('복원 이후 대화가 변경되어 자동 되돌리기를 중단했습니다: ' + sid)
                    db.execute('DELETE FROM thread_dynamic_tools WHERE thread_id=?', (sid,)) if columns(db, 'thread_dynamic_tools') else None
                    db.execute('DELETE FROM threads WHERE id=?', (sid,))
                db.execute('DELETE FROM project_roots WHERE project_id=?', (journal['project_id'],))
                db.execute('DELETE FROM projects WHERE id=?', (journal['project_id'],))
        elif digest(target) == item['before_hash']:
            continue
        elif digest(target) != item.get('after_hash'):
            raise RuntimeError('복원 도중 변경된 파일을 보존했습니다. 백업을 확인하세요: ' + str(target))
        elif original:
            tmp = target.with_name(target.name + '.transfer-rollback')
            shutil.copyfile(original, tmp); os.replace(tmp, target)
        elif target.exists():
            target.unlink()


def restore_desktop(bundle, parent, home, progress=lambda s: None, *,
                    process_check=require_codex_closed, fault=lambda stage: None):
    process_check()
    info = preview_restore(bundle, parent, home)
    m = info['manifest']; destination = Path(info['project_path']); home = Path(info['codex_home'])
    dbpath = Path(info['state_file'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    needed = sum(entry['size'] for entry in m['files'].values())
    for folder in (destination.parent, home):
        if shutil.disk_usage(folder).free < needed * 2 + dbpath.stat().st_size + 100 * 1024**2:
            raise ValueError('파일과 안전 백업을 저장할 여유 공간이 부족합니다.')
    with transfer_lock(home):
        for journal_path in (home / 'transfer-backups').glob('*/journal.json'):
            if json.loads(journal_path.read_text(encoding='utf-8')).get('status') in {'applying', 'recovery_required'}:
                raise RuntimeError('완료되지 않은 복원이 있습니다. 먼저 중단된 복원 복구를 실행하세요.')
        with tempfile.TemporaryDirectory(prefix='.codex-transfer-', dir=destination.parent) as work:
            stage = Path(work)
            with zipfile.ZipFile(bundle) as z:
                for i, (name, entry) in enumerate(m['files'].items()):
                    progress(f'검증 및 준비 {i + 1}/{len(m["files"])}')
                    target = stage.joinpath(*safe_name(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                    if digest(target) != entry['sha256']:
                        raise ValueError(f'파일 무결성 검증 실패: {name}')
            (stage / 'project').mkdir(exist_ok=True)
            source_dbs = list((stage / 'codex').glob('state_*.sqlite'))
            if m['sessions'] and len(source_dbs) != 1:
                raise ValueError('데스크톱 복원에 필요한 대화 인덱스가 없습니다. 원본 PC에서 다시 백업하세요.')
            source_rows = {}
            tools = []
            if source_dbs:
                with readonly(source_dbs[0]) as src:
                    source_rows = {r['id']: r for r in rows(src, 'threads')}
                    if columns(src, 'thread_dynamic_tools'):
                        tools = rows(src, 'thread_dynamic_tools')
            with readonly(dbpath) as db:
                existing = {r[0] for r in db.execute('SELECT id FROM threads')}
                target_columns = columns(db, 'threads')
            ids = {s['id']: str(uuid.uuid4()) if s['id'] in existing else s['id'] for s in m['sessions']}
            project_id = str(uuid.uuid4())
            imported = []
            publication = []
            stamp = time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime())
            for s in m['sessions']:
                if s['id'] not in source_rows:
                    raise ValueError(f'패키지에 대화 인덱스가 없습니다: {s["id"]}')
                row = source_rows[s['id']].copy()
                missing = {k for k, v in row.items() if k not in target_columns and v is not None}
                if missing:
                    raise ValueError('대상 Codex가 백업보다 오래된 저장 형식입니다. 양쪽 버전을 맞춰주세요: ' + ', '.join(sorted(missing)))
                sid = ids[s['id']]
                relative = f'sessions/rollout-{stamp}-{sid}.jsonl'
                target = safe_local_path(home / relative)
                if target.exists():
                    raise FileExistsError(f'대화 파일이 이미 존재합니다: {target}')
                edited = stage / (sid + '.jsonl')
                rewrite_rollout(stage / s['archive_path'], edited, m['source_project'], destination, ids, s['id'])
                row.update(id=sid, rollout_path=str(target), cwd=remap_cwd(s['cwd'], m['source_project'], destination),
                           project_id=project_id, thread_section_id=None, is_pinned=0)
                # Rebuild the desktop's paginated view from the complete rollout.
                row['history_mode'] = 'legacy'
                for field in ('section_position', 'section_entered_at_ms'):
                    if field in row: row[field] = None
                policy = row.get('sandbox_policy')
                if policy:
                    try:
                        policy = json.loads(policy)
                        if isinstance(policy, dict) and isinstance(policy.get('writable_roots'), list):
                            policy['writable_roots'] = [remap_cwd(v, m['source_project'], destination) for v in policy['writable_roots'] if belongs(v, m['source_project'])]
                            row['sandbox_policy'] = json.dumps(policy)
                    except ValueError:
                        pass
                imported.append(row)
                publication.append((relative, edited))
            global_path = safe_local_path(home / '.codex-global-state.json')
            state = json.loads(global_path.read_text(encoding='utf-8')) if global_path.exists() else {}
            updated = update_global(state, home, destination, project_id, m['sessions'], ids)
            staged_global = stage / 'global.json'; atomic_json(staged_global, updated)
            publication.append(('.codex-global-state.json', staged_global))
            index_path = safe_local_path(home / 'session_index.jsonl')
            index = index_path.read_bytes() if index_path.exists() else b''
            if index and not index.endswith(b'\n'): index += b'\n'
            for s in m['sessions']:
                index += (json.dumps({'id': ids[s['id']], 'thread_name': s['title'], 'updated_at': m['created_at']}, ensure_ascii=False) + '\n').encode('utf-8')
            staged_index = stage / 'index.jsonl'; staged_index.write_bytes(index)
            publication.append(('session_index.jsonl', staged_index))
            backup = safe_local_path(home / 'transfer-backups' / str(uuid.uuid4()))
            backup.mkdir(parents=True)
            snapshot_database(dbpath, backup / dbpath.name)
            journal = {'status': 'prepared', 'project_id': project_id, 'project_path': str(destination),
                       'inserted_ids': list(ids.values()), 'files': [
                           {'relative': dbpath.name, 'kind': 'database', 'backup': dbpath.name, 'before_hash': None}]}
            for i, (relative, source) in enumerate(publication):
                target = safe_local_path(home / relative)
                saved = f'original-{i}' if target.exists() else None
                if saved: shutil.copyfile(target, backup / saved)
                journal['files'].append({'relative': relative, 'kind': 'file', 'backup': saved,
                                         'before_hash': digest(target), 'after_hash': digest(source)})
            atomic_json(backup / 'journal.json', journal)
            process_check()
            receipt = {'format': 'codex-desktop-transfer', 'version': 2, 'project_path': str(destination),
                       'codex_home': str(home), 'project_id': project_id, 'backup_path': str(backup),
                       'sessions': [s | {'original_id': s['id'], 'id': ids[s['id']]} for s in m['sessions']]}
            atomic_json(backup / 'receipt.json', receipt)
            if (stage / 'reference').exists():
                shutil.copytree(stage / 'reference', backup / 'reference')
            journal['status'] = 'applying'; atomic_json(backup / 'journal.json', journal)
            try:
                with database(dbpath) as db:
                    db.execute('PRAGMA foreign_keys=ON')
                    db.execute('BEGIN IMMEDIATE')
                    for sid in ids.values():
                        if db.execute('SELECT 1 FROM threads WHERE id=?', (sid,)).fetchone():
                            raise RuntimeError('복원 준비 중 같은 대화가 생성됐습니다. 다시 시도하세요.')
                    now = int(time.time() * 1000)
                    position = db.execute('SELECT coalesce(max(position), -1)+1 FROM projects').fetchone()[0]
                    insert_row(db, 'projects', {'id': project_id, 'name': destination.name, 'metadata': '{}',
                                              'position': position, 'created_at_ms': now, 'updated_at_ms': now})
                    insert_row(db, 'project_roots', {'project_id': project_id, 'position': 0, 'path': str(destination)})
                    for row in imported: insert_row(db, 'threads', row)
                    for row in tools:
                        if row['thread_id'] in ids:
                            insert_row(db, 'thread_dynamic_tools', row | {'thread_id': ids[row['thread_id']]})
                    db.row_factory = sqlite3.Row
                    journal['expected_rows'] = {sid: dict(db.execute('SELECT * FROM threads WHERE id=?', (sid,)).fetchone()) for sid in ids.values()}
                    atomic_json(backup / 'journal.json', journal)
                    fault('database_prepared')
                    process_check()
                    if destination.exists(): raise FileExistsError(f'복원 위치가 이미 존재합니다: {destination}')
                    (stage / 'project').rename(destination)
                    for relative, source in publication:
                        target = home / relative
                        original = next(f for f in journal['files'] if f['relative'] == relative)
                        if digest(target) != original['before_hash']:
                            raise RuntimeError('복원 도중 Codex 저장소가 변경됐습니다. 앱 종료 후 다시 시도하세요.')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(target.name + '.transfer-tmp')
                        shutil.copyfile(source, temporary)
                        os.replace(temporary, target)
                    fault('files_published')
                fault('database_committed')
                journal['status'] = 'committed'; atomic_json(backup / 'journal.json', journal)
            except BaseException:
                try:
                    rollback_files(home, backup, journal)
                    if destination.exists():
                        # Move only the newly created folder back into our verified temporary tree.
                        if not (stage / 'project').exists(): destination.rename(stage / 'project')
                    journal['status'] = 'rolled_back'; atomic_json(backup / 'journal.json', journal)
                except Exception:
                    journal['status'] = 'recovery_required'; atomic_json(backup / 'journal.json', journal)
                raise
            progress('복원 완료 · Codex Windows 앱을 다시 실행하세요.')
            return receipt
