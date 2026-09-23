"""Read Codex's displayed names without storing prompts or changing its state."""
from __future__ import annotations

import hashlib
import json
import ntpath
import sqlite3


def path_key(path):
    return ntpath.normcase(ntpath.normpath(path.removeprefix('\\\\?\\'))) if path else ''


def label(value):
    return value.strip() if isinstance(value, str) and value.strip() and '\n' not in value and '\r' not in value and len(value) <= 512 else ''


def task_select(columns):
    """Older `title` is often the first prompt, including its shortened prefix."""
    keys = [k for k in ('id', 'rollout_path', 'name', 'model', 'model_provider', 'cwd',
                       'archived', 'thread_source', 'project_id', 'agent_nickname') if k in columns]
    if {'title', 'first_user_message'} <= columns:
        keys.append("CASE WHEN length(title) BETWEEN 1 AND 160 AND length(first_user_message)>0 "
                    "AND substr(first_user_message,1,length(title))<>title "
                    "AND instr(title,char(10))=0 AND instr(title,char(13))=0 THEN title ELSE NULL END AS title")
    return ','.join(keys)


class CodexNames:
    def __init__(self, home, db):
        self.scope = hashlib.sha256(path_key(str(home)).encode()).hexdigest()[:16]
        self.projects = {}
        self.roots = {}
        self.projectless_roots = set()
        self.state = {}
        try:
            value = json.loads((home / '.codex-global-state.json').read_text(encoding='utf-8'))
            if isinstance(value, dict):
                self.state = value
        except (OSError, ValueError):
            pass
        projectless = self.state.get('projectless-thread-ids', [])
        self.projectless = {tid for tid in projectless if isinstance(tid, str)} if isinstance(projectless, list) else set()
        outputs = self.state.get('thread-projectless-output-directories', {})
        if isinstance(outputs, dict):
            self.projectless.update(outputs)
            self.projectless_roots.update(path_key(ntpath.dirname(path)) for path in outputs.values() if isinstance(path,str) and path)
        try:
            for tid, cwd in db.execute('select id,cwd from threads'):
                if tid in self.projectless and cwd:
                    self.projectless_roots.add(path_key(cwd))
        except sqlite3.Error:
            pass
        self.aliases = {}
        mappings = self.state.get('app-server-project-id-by-legacy-project-id-by-host', {})
        if isinstance(mappings, dict):
            for host, mapping in mappings.items():
                if host.startswith('local:') and path_key(host[6:]) == path_key(str(home)) and isinstance(mapping, dict):
                    self.aliases.update({k: v for k, v in mapping.items() if isinstance(v, str)})
        local = self.state.get('local-projects', {})
        if isinstance(local, dict):
            for original_id, project in local.items():
                if not isinstance(project, dict):
                    continue
                identity = self.aliases.get(original_id, original_id)
                name = label(project.get('name'))
                if name:
                    self.projects[identity] = name
                roots = project.get('rootPaths', [])
                if isinstance(roots, list):
                    self.roots[identity] = [p for p in roots if isinstance(p, str) and p]
        # The app-server is authoritative after migration, including renames.
        try:
            columns = {r[1] for r in db.execute('pragma table_info(projects)')}
            if {'id', 'name'} <= columns:
                for identity, name in db.execute('select id,name from projects'):
                    if label(name):
                        self.projects[identity] = name.strip()
        except sqlite3.Error:
            pass

    def resolve(self, meta):
        tid = meta['id']
        title = label(meta.get('name')) or label(meta.get('title')) or label(meta.get('agent_nickname')) or f'작업 {tid[:8]}·{tid[-6:]}'
        cwd = meta.get('cwd') or ''
        result = {'display_title': title}
        identity = meta.get('project_id')
        source = 'app_server'
        assignments = self.state.get('thread-project-assignments', {})
        assignment = assignments.get(tid) if isinstance(assignments, dict) else None
        if not identity and isinstance(assignment, dict) and assignment.get('projectKind') == 'local':
            identity = assignment.get('projectId')
            source = 'app_assignment'
        if identity:
            identity = self.aliases.get(identity, identity)
        else:
            if tid in self.projectless:
                return dict(result, project=f'projectless:{self.scope}', project_id='',
                            project_name='프로젝트 없는 작업', project_source='app_projectless')
            hints = self.state.get('thread-workspace-root-hints', {})
            hint = hints.get(tid, '') if isinstance(hints, dict) else ''
            candidates = []
            for pid, roots in self.roots.items():
                for root in roots:
                    normalized = path_key(root)
                    if any(path_key(p) == normalized or path_key(p).startswith(normalized.rstrip('\\') + '\\')
                           for p in (hint, cwd) if p):
                        candidates.append((len(normalized), pid))
            if candidates:
                length = max(item[0] for item in candidates)
                identities = {pid for size, pid in candidates if size == length}
                if len(identities) == 1:
                    identity = identities.pop()
                    source = 'workspace_root'
        if identity:
            name = self.projects.get(identity) or ntpath.basename(cwd.rstrip('/\\')) or f'프로젝트 {identity[-8:]}'
            return dict(result, project=f'project:{self.scope}:{identity}', project_id=identity,
                        project_name=name, project_source=source)
        if path_key(cwd) in self.projectless_roots:
            return dict(result, project=f'projectless:{self.scope}', project_id='',
                        project_name='프로젝트 없는 작업', project_source='app_projectless_workspace')
        labels = self.state.get('electron-workspace-root-labels', {})
        names = {path_key(k): label(v) for k, v in labels.items()} if isinstance(labels, dict) else {}
        name = names.get(path_key(cwd))
        if name:
            return dict(result, project=path_key(cwd), project_id='',project_name=name,project_source='workspace_label')
        # An arbitrary working folder is not a saved Codex project. Keep its
        # path in task details, while the selector groups unmatched tasks.
        return dict(result, project=f'unassigned:{self.scope}', project_id='',
                    project_name='기타 작업', project_source='unassigned_workspace')
