#!/usr/bin/env python3
# Windows-friendly local file agent for Ollama.
# - Sandbox root: FILE_AGENT_ROOT (defaults to ~/agent_root)
# - Uses /api/chat when available, falls back to /api/generate otherwise.

import os, sys, json, re, requests, shutil, fnmatch
from pathlib import Path
from datetime import datetime

# -----------------------
# Config
# -----------------------
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
BASE_DIR = Path(os.environ.get("FILE_AGENT_ROOT", str(Path.home() / "agent_root"))).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
MAX_READ_BYTES = 1024 * 1024  # 1 MB cap for reads/grep previews

# -----------------------
# Guarded file ops
# -----------------------
def _safe(p: str) -> Path:
    p = Path(p)
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    else:
        p = p.resolve()
    if BASE_DIR not in p.parents and p != BASE_DIR:
        raise ValueError("Path escapes BASE_DIR")
    return p

def tool_list_dir(path="."):
    p = _safe(path)
    if not p.exists():
        return {"error":"path not found"}
    if not p.is_dir():
        return {"error":"not a directory"}
    out = []
    for child in sorted(p.iterdir(), key=lambda c:(not c.is_dir(), str(c.name).lower())):
        try:
            stat = child.stat()
            out.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
        except Exception as e:
            out.append({"name": child.name, "error": str(e)})
    return {"ok": True, "entries": out, "cwd": str(p)}

def tool_read_text(path, encoding="utf-8", start=0, length=65536):
    p = _safe(path)
    if not p.exists() or not p.is_file():
        return {"error": "file not found"}
    if p.stat().st_size > MAX_READ_BYTES:
        return {"error": f"file too large to read (> {MAX_READ_BYTES} bytes)"}
    with p.open("r", encoding=encoding, errors="replace") as f:
        try:
            f.seek(start)
        except Exception:
            pass
        data = f.read(length)
    return {"ok": True, "path": str(p), "data": data}

def tool_write_text(path, content, encoding="utf-8", overwrite=False):
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not overwrite:
        return {"error": "file exists; set overwrite=true to replace"}
    with p.open("w", encoding=encoding, errors="replace") as f:
        f.write(content)
    return {"ok": True, "path": str(p), "bytes": len(content)}

def tool_append_text(path, content, encoding="utf-8"):
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=encoding, errors="replace") as f:
        f.write(content)
    return {"ok": True, "path": str(p), "bytes_appended": len(content)}

def tool_create_dir(path):
    p = _safe(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(p)}

def tool_move_path(src, dst, overwrite=False):
    ps = _safe(src)
    pd = _safe(dst)
    pd.parent.mkdir(parents=True, exist_ok=True)
    if pd.exists() and not overwrite:
        return {"error": "destination exists; set overwrite=true"}
    shutil.move(str(ps), str(pd))
    return {"ok": True, "src": str(ps), "dst": str(pd)}

def tool_delete_path(path, confirm=False):
    if not confirm:
        return {"error":"destructive op requires confirm=true"}
    p = _safe(path)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    else:
        return {"error":"path not found"}
    return {"ok": True, "deleted": str(p)}

def tool_search_glob(pattern, root="."):
    r = _safe(root)
    hits = []
    for dirpath, dirnames, filenames in os.walk(r):
        for name in filenames + dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), start=BASE_DIR)
            if fnmatch.fnmatch(name, pattern):
                hits.append(rel.replace("\\", "/"))
    return {"ok": True, "pattern": pattern, "results": hits}

def tool_grep(pattern, path=".", max_matches=200):
    p = _safe(path)
    rx = re.compile(pattern)
    results = []
    def grep_file(fp: Path):
        try:
            if fp.stat().st_size > MAX_READ_BYTES: return
            with fp.open("r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        results.append({"file": str(fp.relative_to(BASE_DIR)).replace("\\","/"),
                                        "line": i, "text": line.rstrip("\n")})
                        if len(results) >= max_matches: return True
        except Exception:
            pass
        return False
    if p.is_file():
        grep_file(p)
    else:
        for dirpath, _, filenames in os.walk(p):
            for name in filenames:
                if grep_file(Path(dirpath)/name): break
            if len(results) >= max_matches: break
    return {"ok": True, "count": len(results), "results": results[:max_matches]}

def tool_stat(path):
    p = _safe(path)
    if not p.exists(): return {"error":"not found"}
    s = p.stat()
    return {"ok": True, "path": str(p), "is_dir": p.is_dir(), "size": s.st_size,
            "modified": datetime.fromtimestamp(s.st_mtime).isoformat()}

TOOLS = {
    "list_dir": tool_list_dir,
    "read_text": tool_read_text,
    "write_text": tool_write_text,
    "append_text": tool_append_text,
    "create_dir": tool_create_dir,
    "move_path": tool_move_path,
    "delete_path": tool_delete_path,
    "search_glob": tool_search_glob,
    "grep": tool_grep,
    "stat": tool_stat,
}

# -----------------------
# System / protocol prompt
# -----------------------
SYSTEM = f"""
You are a local file-operations assistant. You can request tools to interact with files
INSIDE this root only:

ROOT: {BASE_DIR}

Guardrails:
- Never try to access paths outside ROOT.
- Use relative paths unless necessary.
- For risky ops (delete/move/overwrite), explain intent before finalizing.
- Keep outputs concise; when reading large text, ask for narrower reads.
- Prefer UTF-8 text.

Tool protocol:
- When you need a tool, respond ONLY with a single JSON object:
  {{"tool": "<name>", "args": {{...}}}}
- After you receive a tool result, you may either call another tool or produce
  your final answer as a normal message.
- Do NOT include commentary in tool JSON. Do NOT wrap in markdown fences.

Available tools and required args:
- list_dir(path?)
- read_text(path, encoding?, start?, length?)
- write_text(path, content, encoding?, overwrite?)
- append_text(path, content, encoding?)
- create_dir(path)
- move_path(src, dst, overwrite?)
- delete_path(path, confirm?)  # confirm must be true to proceed
- search_glob(pattern, root?)
- grep(pattern, path?, max_matches?)
- stat(path)
"""

# -----------------------
# Ollama chat/generate bridge (with fallback)
# -----------------------
def _ollama_supports_chat():
    # quick probe: if POST /api/chat returns 404, assume no chat support
    try:
        probe = {
            "model": MODEL,
            "messages": [{"role":"user","content":"ping"}],
            "stream": False
        }
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=probe, timeout=5)
        if r.status_code == 404:
            return False
        if r.ok:
            return True
        # If it's some other response (e.g., 405 due to wrong method elsewhere), assume chat possibly OK
        return r.status_code != 405
    except Exception:
        # If server unreachable, let main call raise later
        return True

def chat(messages):
    """
    Use /api/chat when available; otherwise flatten to a prompt and use /api/generate.
    Returns a dict with .message.content to match /api/chat shape.
    """
    use_chat = _ollama_supports_chat()

    if use_chat:
        payload = {"model": MODEL, "messages": messages, "stream": False}
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120)
        r.raise_for_status()
        return r.json()

    # Fallback to /api/generate: create a simple chat template
    sys_txt = ""
    lines = []
    for m in messages:
        role = m.get("role")
        content = m.get("content","")
        if role == "system":
            sys_txt += content + "\n"
        elif role in ("user","assistant","tool"):
            lines.append(f"{role.capitalize()}:\n{content}\n")
    if sys_txt:
        lines.insert(0, f"[SYSTEM]\n{sys_txt}\n[/SYSTEM]\n")
    lines.append("Assistant:")

    payload = {"model": MODEL, "prompt": "\n".join(lines), "stream": False}
    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return {"message": {"content": data.get("response", "")}}

# -----------------------
# REPL loop
# -----------------------
def maybe_parse_tool(s: str):
    s = s.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "tool" in obj and "args" in obj:
            name = obj["tool"]
            args = obj["args"] if isinstance(obj["args"], dict) else {}
            return name, args
    except Exception:
        pass
    return None, None

def main():
    print(f"[file-agent] Root: {BASE_DIR}")
    print("[file-agent] Type your request. Example: 'Create notes/todo.txt with two lines…'")
    print("--------------------------------------------------------------")

    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            user = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user:
            continue
        if user.lower() in {"exit", "quit"}:
            break

        history.append({"role": "user", "content": user})
        # inner loop: allow the model to call tools repeatedly
        while True:
            resp = chat(history)
            content = resp["message"]["content"]
            name, args = maybe_parse_tool(content)

            if name and name in TOOLS:
                # Execute tool
                try:
                    result = TOOLS[name](**args)
                except TypeError as e:
                    result = {"error": f"bad args: {e}"}
                except Exception as e:
                    result = {"error": str(e)}
                tool_msg = json.dumps({"tool": name, "args": args, "result": result})[:200000]
                history.append({"role": "tool", "content": tool_msg})
                # Loop again so the model can see result and continue
                continue
            else:
                # Final assistant message for this turn
                history.append({"role": "assistant", "content": content})
                print(f"\nAssistant>\n{content}")
                break

if __name__ == "__main__":
    main()
