import psutil

for p in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        cmd = ' '.join(p.info['cmdline'] or [])
        if 'better_memory.mcp' not in cmd:
            continue
        env = p.environ()
        pid = p.info['pid']
        print(f'PID {pid}')
        print(f'  cwd = {p.cwd()}')
        print(f'  ppid = {p.ppid()}')
        try:
            parent = p.parent()
            print(f'  parent = {parent.name() if parent else None}')
        except Exception:
            pass
        for key in ('CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDECODE'):
            print(f'  {key} = {env.get(key)!r}')
        related = sorted(k for k in env if 'CLAUDE' in k.upper() or 'SESSION' in k.upper())
        print(f'  all related keys: {related}')
        path = env.get('PATH') or env.get('Path') or ''
        git_in_path = [seg for seg in path.split(';') if 'git' in seg.lower()]
        print(f'  git-related PATH segments: {git_in_path}')
        print(f'  PATH length = {len(path)} bytes, {len(path.split(chr(59)))} segs')
        print()
    except Exception as e:
        print(f'  err: {e}')
