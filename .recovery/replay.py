"""Reconstruct source in memory from historical patch literals only."""
import contextlib
import datetime
import difflib
import io
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent / 'stage5c'
OLD = '/private/tmp/qplot-stage5c-repair.zJj0i7/'
def canonical(p, context):
    p = p.rstrip()
    if OLD.rstrip('/') in context:
        p = re.sub(r'(\*\*\* (?:Update|Add|Delete) File: )(?!/)', lambda m:m[0]+OLD, p)
    return p
patches = {}
failed = set()
actual = {}
for log in pathlib.Path('/Users/edward/.codex/sessions/2026/08').rglob('*.jsonl'):
    calls = {}
    for line in log.open():
        rec = json.loads(line); d = rec.get('payload', {})
        if d.get('type') == 'custom_tool_call': calls[d.get('call_id')] = d.get('input', '')
        if d.get('type') != 'custom_tool_call_output': continue
        source = calls.get(d.get('call_id'), '')
        output = ''.join(c.get('text','') for c in d.get('output',[]) if isinstance(c,dict))
        for m in re.finditer(r'(?:const patch = |tools.apply_patch\()(?="\*\*\* Begin Patch)', source):
            try: p, _ = json.JSONDecoder().raw_decode(source[m.end():])
            except ValueError: continue
            p = canonical(p, source)
            if 'Script failed' in output or 'verification failed' in output:
                failed.add(p)
            else:
                t = d.get('internal_chat_message_metadata_passthrough', {}).get('create_time')
                if t: actual[p] = t
for log in pathlib.Path('/Users/edward/.codex/sessions/2026/08').rglob('*.jsonl'):
    for line in log.open():
        record = json.loads(line)
        d = record.get('payload', {})
        review = d.get('type') == 'message' and d.get('role') == 'user'
        if d.get('type') == 'custom_tool_call':
            strings = [d.get('input', '')]
            for match in re.finditer(r'(?:cmd:\s*|"cmd":\s*)(?=")', strings[0]):
                try: command, _ = json.JSONDecoder().raw_decode(strings[0][match.end():])
                except ValueError: continue
                if 'apply_patch' in command and OLD.rstrip('/') in strings[0] and '*** Begin Patch' in command:
                    fragment = command[command.index('*** Begin Patch'):command.index('*** End Patch')+len('*** End Patch')]
                    fragment = re.sub(r'(\*\*\* (?:Update|Add|Delete) File: )(?!/)', lambda m:m[0]+OLD, fragment)
                    strings.append('const patch = ' + json.dumps(fragment))
        elif review:
            strings = [c.get('text', '') for c in d.get('content', [])]
        else:
            continue
        stamp = d.get('internal_chat_message_metadata_passthrough', {}).get('create_time') or datetime.datetime.fromisoformat(record['timestamp']).timestamp()
        if stamp > 1788307200: continue
        for s in strings:
            for m in re.finditer(r'(?:const patch = |tools.apply_patch\(|"patch":\s*|"input":\s*)(?="\*\*\* Begin Patch)', s):
                try: patch, _ = json.JSONDecoder().raw_decode(s[m.end():])
                except ValueError: continue
                patch = canonical(patch, s)
                if OLD not in patch or not patch.endswith('*** End Patch'): continue
                if '*** Delete File:' in patch and '*** Add File:' not in patch: continue
                if patch in failed: continue
                prior = patches.get(patch)
                if prior is None or (review and not prior[2]) or (review == prior[2] and stamp < prior[0]):
                    patches[patch] = (stamp, record.get('ordinal', 0), review)

contents = {}
def load(path):
    if path not in contents:
        r = subprocess.run(['git', 'show', 'a606d4e:' + path], cwd=ROOT, text=True, capture_output=True)
        contents[path] = r.stdout if r.returncode == 0 else ''
    return contents[path]

def apply(patch):
    updates = {}
    lines = patch.splitlines(keepends=True)
    i = 1
    while i < len(lines) and not lines[i].startswith('*** End Patch'):
        header = lines[i].rstrip('\n'); i += 1
        if not header.startswith(('*** Update File: ', '*** Add File: ', '*** Delete File: ')):
            return False, ('header', header)
        kind, path = header[4:].split(' File: ', 1)
        if not path.startswith(OLD):
            return False, ('outside', path)
        path = path[len(OLD):]
        current = updates.get(path, load(path))
        if kind == 'Delete':
            updates[path] = ''
            continue
        if kind == 'Add':
            added = []
            while i < len(lines) and lines[i].startswith('+'):
                added.append(lines[i][1:]); i += 1
            updates[path] = ''.join(added)
            continue
        cursor = 0
        while i < len(lines) and not lines[i].startswith('*** '):
            if not lines[i].startswith('@@'):
                return False, (path, 'missing @@', lines[i])
            anchor = lines[i][2:].strip(); i += 1
            if anchor:
                at = current.find(anchor, cursor)
                if at >= 0: cursor = at + len(anchor)
            before, after = [], []
            while i < len(lines) and not lines[i].startswith(('@@', '*** ')):
                l = lines[i]; i += 1
                if l[0:1] in (' ', '-') : before.append(l[1:])
                if l[0:1] in (' ', '+') : after.append(l[1:])
            a, b = ''.join(before), ''.join(after)
            at = current.find(a, cursor)
            if at < 0 and anchor: at = current.find(a)
            if at < 0 and path.endswith('.py'):
                formatted = subprocess.run([str(ROOT.parent.parent / '.venv-mac/bin/python'), '-m', 'ruff', 'format', '--stdin-filename', path, '-'], input=current, text=True, capture_output=True, cwd=ROOT)
                if formatted.returncode == 0 and a in formatted.stdout:
                    current = formatted.stdout
                    at = current.find(a)
            if at >= 0:
                current = current[:at] + b + current[at+len(a):]
                cursor = at + len(b)
                continue
            # Ruff may have reflowed lines between recorded patches.
            pattern = r'\s*'.join(re.escape(c) for c in re.sub(r'\s+', '', a))
            hits = list(re.finditer(pattern, current)) if pattern else []
            hits = [h for h in hits if h.start() >= cursor] or hits
            if not hits:
                unresolved.append((path, a[:180]))
                continue
            hit = hits[0]
            start = current.rfind('\n', 0, hit.start()) + 1
            end = current.find('\n', hit.end())
            if end < 0: end = len(current)
            else: end += 1
            if a.startswith('\n') and start > 0 and current[start-2:start] == '\n\n': start -= 1
            current = current[:start] + b + current[end:]
            cursor = start + len(b)
        updates[path] = current
    contents.update(updates)
    return True, None

pending = sorted(patches, key=lambda p: (actual.get(p, patches[p][0]), patches[p][1]))
applied = []
unresolved = []
for cycle in range(1):
    remaining = []
    failures = []
    for patch in pending:
        ok, error = apply(patch)
        if ok: applied.append(patch)
        else:
            remaining.append(patch)
            failures.append(error)
    if len(remaining) == len(pending): break
    pending = remaining

if '--patches' in sys.argv:
    out = []
    for path, content in contents.items():
        target = ROOT / path
        before = target.read_text() if target.exists() else ''
        if before == content: continue
        if not target.exists():
            patch = '*** Add File: ' + str(target) + '\n' + ''.join('+' + s + '\n' for s in content.splitlines())
        else:
            diff = list(difflib.unified_diff(before.splitlines(), content.splitlines(), n=3))[2:]
            patch = '*** Update File: ' + str(target) + '\n' + '\n'.join('@@' if s.startswith('@@') else s for s in diff) + '\n'
        out.append('*** Begin Patch\n' + patch + '*** End Patch')
    print(json.dumps({'patches': out, 'failures': failures}))
else:
    print(json.dumps({'applied':len(applied), 'pending':len(pending), 'failures':failures}, indent=2))
