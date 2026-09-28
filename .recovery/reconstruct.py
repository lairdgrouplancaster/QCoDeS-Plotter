"""Recover this project's recorded successful file changes, without executing logs."""
import collections
import difflib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent / 'stage5c'
OLD = '/private/tmp/qplot-stage5c-repair.zJj0i7/'
events = []
seen = set()
for log in pathlib.Path('/Users/edward/.codex/sessions/2026/08').rglob('*.jsonl'):
    for line in log.open():
        record = json.loads(line)
        payload = record.get('payload', {})
        item = payload.get('item', {})
        if item.get('type') != 'FileChange' or item.get('status') != 'completed':
            continue
        for path, change in item.get('changes', {}).items():
            if not path.startswith(OLD) or (item['id'], path) in seen:
                continue
            seen.add((item['id'], path))
            events.append((payload['completed_at_ms'], record.get('ordinal', 0), path[len(OLD):], change))
events.sort(key=lambda e: (e[0], e[1]))
contents = {}
failures = []
counts = collections.Counter()
queue = list(events)
retries = []
while queue:
    timestamp, ordinal, path, change = queue.pop(0)
    if path not in contents:
        result = subprocess.run(['git', 'show', 'a606d4e:' + path], cwd=ROOT, capture_output=True, text=True)
        contents[path] = result.stdout if result.returncode == 0 else ''
    if change['type'] == 'add':
        contents[path] = change['content']
        counts[path] += 1
        continue
    lines = contents[path].splitlines(keepends=True)
    diff = change['unified_diff'].splitlines(keepends=True)
    hunks = []
    for line in diff:
        if line.startswith('@@ '):
            match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)', line)
            hunks.append([int(match[1]) - 1, [], []])
        elif hunks and line[0:1] in (' ', '+', '-'):
            if line[0] != '+': hunks[-1][1].append(line[1:])
            if line[0] != '-': hunks[-1][2].append(line[1:])
    offset = 0
    ok = True
    for start, before, after in hunks:
        pos = start + offset
        remove_count = len(before)
        if lines[pos:pos + len(before)] != before:
            matches = [i for i in range(len(lines) - len(before) + 1) if lines[i:i + len(before)] == before]
            if not matches:
                normalized = [s.strip() for s in lines]
                target = [s.strip() for s in before]
                matches = [i for i in range(len(lines) - len(before) + 1) if normalized[i:i + len(before)] == target]
            if not matches:
                compact_lines = [re.sub(r'\s+', '', s) for s in lines]
                compact = ''.join(compact_lines)
                target = re.sub(r'\s+', '', ''.join(before))
                positions = [m.start() for m in re.finditer(re.escape(target), compact)] if target else []
                offsets = [0]
                for s in compact_lines: offsets.append(offsets[-1] + len(s))
                candidates = []
                for hit in positions:
                    starts = [i for i, n in enumerate(offsets[:-1]) if n == hit]
                    ends = [i for i, n in enumerate(offsets) if n == hit + len(target)]
                    if starts and ends:
                        a = min(starts, key=lambda i: abs(i - pos))
                        b = min(ends, key=lambda i: abs(i - (a + len(before))))
                        candidates.append((a,b))
                if candidates:
                    pos, end = min(candidates, key=lambda ab: abs(ab[0] - pos))
                    remove_count = end - pos
                else:
                    failures.append((timestamp, path, start, ''.join(before)[:240]))
                    retries.append((timestamp, ordinal, path, change))
                    ok = False
                    break
            else:
                pos = min(matches, key=lambda i: abs(i - pos))
        lines[pos:pos + remove_count] = after
        offset = pos - start + len(after) - remove_count
    if ok:
        contents[path] = ''.join(lines)
        counts[path] += 1
    if not queue and retries and len(retries) < len(events):
        events = retries
        queue = list(retries)
        retries = []
        failures = []

if '--patches' in sys.argv:
    patches = []
    for path, content in contents.items():
        target = ROOT / path
        before = target.read_text() if target.exists() else ''
        if before == content: continue
        if not target.exists():
            patch = '*** Add File: ' + str(target) + '\n' + ''.join('+' + s + '\n' for s in content.splitlines())
        else:
            diff = list(difflib.unified_diff(before.splitlines(), content.splitlines(), n=3))[2:]
            patch = '*** Update File: ' + str(target) + '\n' + '\n'.join('@@' if s.startswith('@@') else s for s in diff) + '\n'
        patches.append('*** Begin Patch\n' + patch + '*** End Patch')
    print(json.dumps({'patches': patches, 'failures': failures}))
else:
    print(json.dumps({'events': len(events), 'files': dict(counts), 'failures': failures}, indent=2))
