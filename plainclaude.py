"""
plainclaude.py

Watches Claude Desktop (Windows) via UI Automation, rewrites each completed
assistant response in plainer language using a local Ollama model, and shows
the result in a separate always-on-top window with markdown + LaTeX rendering.

Read-only by construction: this tool never sends input to Claude, never
touches credentials, and never intercepts network traffic. It reads the
rendered conversation from the app's accessibility tree — the same OS
interface screen readers use — and all language-model processing happens
locally via Ollama. Nothing is stored.

See README.md for setup, configuration, and compatibility notes.

Setup:
  pip install uiautomation requests pywebview
  ollama pull qwen3:8b   (Ollama service on :11434)
  Launch Claude Desktop, then: python plainclaude.py
  (--dump prints the full prompt sent to the model; --dump-tree the subtree)
"""

import argparse
import ctypes
import hashlib
import json
import queue
import re
import threading
import time
from ctypes import wintypes

import requests
import uiautomation as auto

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:14b"
POLL_INTERVAL = 1.0
NOISE_BUTTONS = {"Copy", "Retry", "Edit", "Share", "Good response",
                 "Bad response", "Read aloud", "More actions"}
MSG_NAME_RE = re.compile(r"^Message \d+ of \d+$")
ASSISTANT_PREFIX = "Claude responded:"
USER_PREFIX = "You said:"
FINISHED_MARKER = "finished the response"

REWRITE_INSTRUCTIONS = """You are rewriting an AI assistant's answer to make it easier to read.

You are given conversation context (the user's question, and when available the assistant's previous answer that the question refers to), followed by the ANSWER TO REWRITE.

Rules:
- Rewrite only the text in <answer_to_rewrite>. The context sections are reference material: do not rewrite them, summarize them, or answer the question yourself.
- Preserve every piece of information: all claims, numbers, results, caveats, and actions the assistant took (e.g. if it says it ran a benchmark or a test, your rewrite must say so too). Do not drop anything. Do not add anything.
- When the assistant used tools or took actions (ran code, ran a benchmark, searched the web, read or edited files), your rewrite must explicitly say which action was taken and what it showed. Never turn "I ran X and measured Y" into a bare claim that Y is true — the fact that it was measured rather than assumed is information.
- Lines like [Claude activity: ...] describe actions the assistant took while working. Distinguish two kinds. Markers that describe thinking or analysis ("Thought for 13s", "Analyzed the structure of...") are internal reasoning: ignore them entirely and never open the rewrite by narrating them. Markers that describe concrete external actions (ran a command, edited a file, searched the web) are evidence: mention them naturally whenever they support a claim (e.g. "Claude ran a command to benchmark this and found..."). Never reproduce the bracket notation itself.
- Preserve every concrete specific: numeric values, intervals and endpoints, parameter settings, and named methods. "Solved numerically via bisection on [a, b]" must keep a and b.
- Code blocks: never reproduce code and never rewrite it line by line — the reader has the original code next to this window. Instead, describe in prose what the code does: its purpose, overall structure, the key functions or steps, important parameter values, and any caveats the assistant stated about it. You may quote a short snippet (a few lines at most) only when a specific line is itself the point. Prose surrounding the code still gets a full rewrite under the rules above. When a response is mostly code, your output should be much shorter than the original — for code, the length rule above does not apply.
- Write in full sentences. Short section headings are welcome when they help a reader skim. Bullet points are allowed for genuinely enumerable content, but every bullet must be a complete sentence — never a telegraphic fragment.
- Start directly with the content. No introductory filler ("Here's the breakdown:", "After analyzing...") and no narration of your own rewriting process.
- Define every symbol and variable in words at its first appearance in your rewrite, even if it was defined in an earlier message — use the context sections to recover the definitions. The reader must never need to open a previous message to know what a symbol means. This includes symbols inside formulas you carry over: if you write $\\mu = pb - q$, say what $p$, $b$, and $q$ each are.
- Your rewrite may be nearly as long as the original. Clarity comes from unpacking dense sentences, not from shortening.
- The text was scraped from a rendered app: mathematical formulas may appear duplicated or garbled (the same formula repeated two or three times in a row in different notations). Silently fix this and write each formula once.
- Write all mathematics as LaTeX: inline math between single dollar signs like $\\sigma^2$, and standalone equations between double dollar signs like $$E[X] = m$$. Never write raw unicode math symbols outside of LaTeX.
- Use standard markdown for emphasis, lists, headings, and code (fenced code blocks).
- Output only the rewrite, nothing else."""

# ------------------------------------------------- accessibility poke

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

_IID_IAccessible = _GUID(0x618736E0, 0x3C3D, 0x11CF,
                         (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA,
                                              0x00, 0x38, 0x9B, 0x71))
_OBJID_CLIENT = ctypes.c_long(-4)


def poke_accessibility(top_hwnd: int) -> None:
    hwnds = [top_hwnd]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _):
        hwnds.append(hwnd)
        return True

    ctypes.windll.user32.EnumChildWindows(top_hwnd, _enum, 0)
    for hwnd in hwnds:
        pacc = ctypes.c_void_p()
        ctypes.windll.oleacc.AccessibleObjectFromWindow(
            hwnd, _OBJID_CLIENT, ctypes.byref(_IID_IAccessible),
            ctypes.byref(pacc))

# ------------------------------------------------- window / root location

def _enum_child_windows(top_hwnd: int):
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        out.append((hwnd, buf.value))
        return True

    ctypes.windll.user32.EnumChildWindows(top_hwnd, _enum, 0)
    return out


def find_claude_root(timeout=15):
    """Return (top_window_control, uia_root_control) for the Claude Desktop
    main window. Scans ALL candidate 'Claude' top-levels (the app owns several:
    main window, quick launcher, hidden helpers) and prefers one exposing a
    Chrome_RenderWidgetHostHWND child, rooting the UIA search there. Falls back
    to rooting at a visible top-level itself, since newer Chromium builds may
    render without a separate child HWND."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = []
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ClassName == "Chrome_WidgetWin_1" and "Claude" in (w.Name or ""):
                    candidates.append(w)
            except Exception:
                continue
        for w in candidates:
            try:
                for hwnd, cls in _enum_child_windows(w.NativeWindowHandle):
                    if cls == "Chrome_RenderWidgetHostHWND":
                        return w, auto.ControlFromHandle(hwnd)
            except Exception:
                continue
        for w in candidates:
            try:
                if ctypes.windll.user32.IsWindowVisible(w.NativeWindowHandle):
                    return w, auto.ControlFromHandle(w.NativeWindowHandle)
            except Exception:
                continue
        time.sleep(1)
    raise RuntimeError("No suitable Claude Desktop window found (checked all "
                       "'Claude' top-level windows, with and without a "
                       "render-widget child).")


def get_primary_pane_from_root(root):
    pane = root.Control(searchDepth=25, ControlType=auto.ControlType.GroupControl,
                        Name="Primary pane")
    if pane.Exists(5, 0.5):
        return pane
    raise RuntimeError("'Primary pane' not found under the Claude window tree.")


def get_chat_container(pane):
    chat = pane.Control(searchDepth=15, ControlType=auto.ControlType.GroupControl,
                        Name="Chat messages")
    if chat.Exists(3, 0.3):
        return chat
    raise RuntimeError("'Chat messages' group not found.")

# ------------------------------------------------- message extraction

def find_message_groups(chat, max_depth=4):
    found = []
    frontier = [(chat, 0)]
    while frontier:
        ctrl, d = frontier.pop(0)
        if d > max_depth:
            continue
        try:
            children = ctrl.GetChildren()
        except Exception:
            continue
        for c in children:
            try:
                name = c.Name or ""
            except Exception:
                name = ""
            if MSG_NAME_RE.match(name):
                found.append(c)
            else:
                frontier.append((c, d + 1))
    return found


def shallow_text_names(ctrl, max_depth=3):
    names = []
    frontier = [(ctrl, 0)]
    while frontier:
        c, d = frontier.pop(0)
        if d > max_depth:
            continue
        try:
            children = c.GetChildren()
        except Exception:
            continue
        for k in children:
            try:
                if k.ControlType == auto.ControlType.TextControl and k.Name:
                    names.append(k.Name)
                frontier.append((k, d + 1))
            except Exception:
                continue
    return names


def group_role(g):
    for n in shallow_text_names(g):
        if n.startswith(ASSISTANT_PREFIX):
            return "assistant"
        if n.startswith(USER_PREFIX):
            return "user"
    return None


def harvest_paragraphs(ctrl, paragraphs, depth=0, max_depth=50):
    def only_text_below(c, d=0):
        if d > 10:
            return True
        try:
            kids = c.GetChildren()
        except Exception:
            return True
        for k in kids:
            try:
                if k.ControlType != auto.ControlType.TextControl:
                    return False
            except Exception:
                return False
            if not only_text_below(k, d + 1):
                return False
        return True

    def leaf_texts(c, out, d=0):
        if d > 12:
            return
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        if not kids:
            try:
                n = c.Name
            except Exception:
                n = None
            if n:
                out.append(n)
            return
        for k in kids:
            leaf_texts(k, out, d + 1)

    if depth > max_depth:
        return
    try:
        children = ctrl.GetChildren()
    except Exception:
        return

    for c in children:
        try:
            ct = c.ControlType
            name = c.Name or ""
        except Exception:
            continue
        if name.startswith(ASSISTANT_PREFIX) or name.startswith(USER_PREFIX):
            continue
        if ct == auto.ControlType.StatusBarControl:
            continue
        if ct == auto.ControlType.ButtonControl:
            bn = (name or "").strip()
            if (bn and bn not in NOISE_BUTTONS and len(bn) < 120
                    and not re.match(r"Thought for \d+", bn)):
                marker = f"[Claude activity: {bn}]"
                if not paragraphs or paragraphs[-1] != marker:
                    paragraphs.append(marker)
            continue
        if ct == auto.ControlType.TextControl or only_text_below(c):
            runs = []
            leaf_texts(c, runs)
            para = "".join(runs).strip()
            if para and (not paragraphs or paragraphs[-1] != para):
                paragraphs.append(para)
        else:
            harvest_paragraphs(c, paragraphs, depth + 1, max_depth)


def harvest_group(g):
    paragraphs = []
    harvest_paragraphs(g, paragraphs)
    return "\n\n".join(paragraphs).strip()


def extract_target_with_context(pane):
    chat = get_chat_container(pane)
    groups = find_message_groups(chat)
    roles = [group_role(g) for g in groups]

    t_idx = None
    for i in range(len(groups) - 1, -1, -1):
        if roles[i] == "assistant":
            t_idx = i
            break
    if t_idx is None:
        return None

    target = harvest_group(groups[t_idx])
    if not target:
        return None

    q_idx = None
    for i in range(t_idx - 1, -1, -1):
        if roles[i] == "user":
            q_idx = i
            break
    question = harvest_group(groups[q_idx]) if q_idx is not None else None

    p_idx = None
    if q_idx is not None:
        for i in range(q_idx - 1, -1, -1):
            if roles[i] == "assistant":
                p_idx = i
                break
    prev_answer = harvest_group(groups[p_idx]) if p_idx is not None else None

    return {
        "msg_id": groups[t_idx].Name or "?",
        "target": target,
        "question": question,
        "prev_answer": prev_answer,
    }


def get_status_text(pane):
    try:
        sb = pane.Control(searchDepth=12,
                          ControlType=auto.ControlType.StatusBarControl)
        if sb.Exists(1, 0.3):
            runs = []
            for c in sb.GetChildren():
                try:
                    if c.Name:
                        runs.append(c.Name)
                except Exception:
                    pass
            return " ".join(runs)
    except Exception:
        pass
    return ""

# ------------------------------------------------- Ollama

def build_prompt(bundle) -> str:
    parts = [REWRITE_INSTRUCTIONS, ""]
    if bundle.get("prev_answer"):
        parts += ["<previous_assistant_answer>", bundle["prev_answer"],
                  "</previous_assistant_answer>", ""]
    if bundle.get("question"):
        parts += ["<user_question>", bundle["question"],
                  "</user_question>", ""]
    parts += ["<answer_to_rewrite>", bundle["target"], "</answer_to_rewrite>"]
    return "\n".join(parts)


def simplify(bundle) -> str:
    resp = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": build_prompt(bundle)}],
        "stream": False,
    }, timeout=300)
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()

# ------------------------------------------------- watcher

def full_attach(post_status):
    post_status("Looking for Claude Desktop...")
    win, root = find_claude_root()
    poke_accessibility(win.NativeWindowHandle)
    return win, get_primary_pane_from_root(root)


def watcher(post_status, post_message, stop: threading.Event):
    with auto.UIAutomationInitializerInThread():
        try:
            win, pane = full_attach(post_status)
        except Exception as e:
            post_status(f"FAILED: {e}")
            return

        try:
            bundle = extract_target_with_context(pane)
            baseline_text = bundle["target"] if bundle else ""
        except Exception:
            baseline_text = ""
        last_hash = hashlib.sha256(baseline_text.encode()).hexdigest()
        post_status("Watching.")
        was_finished = True
        finished_streak = 0

        while not stop.is_set():
            time.sleep(POLL_INTERVAL)
            try:
                status = get_status_text(pane)
            except Exception:
                status = ""
            if not status:
                try:
                    win, pane = full_attach(post_status)
                    status = get_status_text(pane)
                except Exception:
                    time.sleep(3)
                    continue

            finished = FINISHED_MARKER in status
            if not finished:
                was_finished = False
                finished_streak = 0
                post_status("Claude is responding...")
                continue

            finished_streak += 1
            if was_finished:
                continue
            if finished_streak < 2:
                continue
            was_finished = True

            time.sleep(0.5)
            bundle = None
            for attempt in (1, 2):
                try:
                    bundle = extract_target_with_context(pane)
                except Exception as e:
                    post_status(f"Extraction error: {e}")
                if bundle:
                    break
                try:
                    win, pane = full_attach(post_status)
                except Exception:
                    break
                time.sleep(1.0)

            if not bundle:
                post_status("Finished, but no assistant text found "
                            "(after re-attach retry).")
                continue

            h = hashlib.sha256(bundle["target"].encode()).hexdigest()
            if h == last_hash:
                post_status("Watching.")
                continue
            last_hash = h

            post_status(f"Rewriting {bundle['msg_id']} "
                        f"({len(bundle['target']):,} chars)...")
            try:
                post_message(simplify(bundle))
                post_status("Watching.")
            except Exception as e:
                post_status(f"Ollama error: {e}")

# ------------------------------------------------- webview UI

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link rel="stylesheet"
  href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/contrib/auto-render.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<style>
  body { font-family: 'Segoe UI', sans-serif; font-size: 14px; margin: 0;
         background: #1f1e1b; color: #e8e6e1; }
  #status { position: sticky; top: 0; z-index: 10; background: #2a2926;
            padding: 6px 10px; font-size: 12px; color: #a8a49c;
            border-bottom: 1px solid #3a3936; }
  #messages { padding: 10px 14px 30px; }
  .msg { border-bottom: 2px solid #3a3936; padding-bottom: 14px;
         margin-bottom: 14px; line-height: 1.5; }
  .msg pre { background: #2e2d2a; padding: 8px; border-radius: 6px;
             overflow-x: auto; font-size: 12.5px; }
  .msg code { background: #2e2d2a; padding: 1px 4px; border-radius: 4px; }
  .msg a { color: #8ab4f8; }
  .katex-display { overflow-x: auto; overflow-y: hidden; }

  body.light { background: #fafaf7; color: #1a1a1a; }
  body.light #status { background: #ececec; color: #444;
                       border-bottom: 1px solid #ddd; }
  body.light .msg { border-bottom: 2px solid #e0ddd5; }
  body.light .msg pre, body.light .msg code { background: #f0efe9; }
  body.light .msg a { color: #1a5fb4; }
</style>
</head>
<body class="__THEME__">
<div id="status">Starting...</div>
<div id="messages"></div>
<script>
function setStatus(s) {
  document.getElementById('status').textContent = s;
}

// Protect math spans from the markdown renderer, then restore + KaTeX.
function renderContent(text) {
  const stash = [];
  // display math first ($$...$$), then inline ($...$ not preceded/followed by $)
  let t = text.replace(/\\$\\$([\\s\\S]+?)\\$\\$/g, (m) => {
    stash.push(m); return `\\u0000MATH${stash.length - 1}\\u0000`;
  });
  t = t.replace(/(?<!\\$)\\$(?!\\$)([^\\$\\n]+?)\\$(?!\\$)/g, (m) => {
    stash.push(m); return `\\u0000MATH${stash.length - 1}\\u0000`;
  });
  let html = marked.parse(t);
  html = html.replace(/\\u0000MATH(\\d+)\\u0000/g, (_, i) => stash[+i]);
  const div = document.createElement('div');
  div.className = 'msg';
  div.innerHTML = html;
  document.getElementById('messages').appendChild(div);
  renderMathInElement(div, {
    delimiters: [
      {left: '$$', right: '$$', display: true},
      {left: '$', right: '$', display: false}
    ],
    throwOnError: false
  });
  window.scrollTo(0, document.body.scrollHeight);
}
</script>
</body>
</html>"""


def run_webview_ui(stop, theme="dark"):
    import webview  # pywebview

    window = webview.create_window(
        "plainclaude", html=HTML_PAGE.replace("__THEME__", theme),
        width=460, height=680, on_top=True)

    def post_status(s):
        try:
            window.evaluate_js(f"setStatus({json.dumps(s)})")
        except Exception:
            pass

    def post_message(text):
        try:
            window.evaluate_js(f"renderContent({json.dumps(text)})")
        except Exception:
            pass

    def start_watcher():
        threading.Thread(target=watcher,
                         args=(post_status, post_message, stop),
                         daemon=True).start()

    webview.start(start_watcher)
    stop.set()


def run_tk_ui(stop):
    """Fallback: plain-text window if pywebview is unavailable."""
    import tkinter as tk
    from tkinter import scrolledtext

    q = queue.Queue()
    root_win = tk.Tk()
    root_win.title("Claude — simplified (plain text fallback)")
    root_win.geometry("420x600")
    root_win.attributes("-topmost", True)
    status = tk.Label(root_win, text="Starting...", anchor="w")
    status.pack(fill="x", padx=6, pady=(6, 0))
    box = scrolledtext.ScrolledText(root_win, wrap="word", font=("Segoe UI", 11))
    box.pack(fill="both", expand=True, padx=6, pady=6)

    threading.Thread(
        target=watcher,
        args=(lambda s: q.put(("status", s)),
              lambda m: q.put(("message", m)), stop),
        daemon=True).start()

    def drain():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "status":
                    status.config(text=payload)
                else:
                    box.insert("end", payload + "\n\n" + "─" * 40 + "\n\n")
                    box.see("end")
        except queue.Empty:
            pass
        root_win.after(200, drain)

    drain()
    try:
        root_win.mainloop()
    finally:
        stop.set()

# ------------------------------------------------- entry

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true",
                    help="print the full prompt sent to the model and exit")
    ap.add_argument("--dump-tree", action="store_true",
                    help="print the chat subtree and exit")
    ap.add_argument("--light", action="store_true",
                    help="use the light theme (default is dark)")
    args = ap.parse_args()

    if args.dump or args.dump_tree:
        with auto.UIAutomationInitializerInThread():
            win, root = find_claude_root()
            poke_accessibility(win.NativeWindowHandle)
            time.sleep(2)
            pane = get_primary_pane_from_root(root)
            if args.dump_tree:
                def dump_tree(c, d=0, md=25):
                    ct = auto.ControlTypeNames.get(c.ControlType, c.ControlType)
                    print("  " * d + f"[{ct}] {(c.Name or '')[:100]!r}")
                    if d < md:
                        for k in c.GetChildren():
                            dump_tree(k, d + 1, md)
                dump_tree(get_chat_container(pane))
            else:
                bundle = extract_target_with_context(pane)
                if not bundle:
                    print("(no assistant message found)")
                    return
                print(build_prompt(bundle))
        return

    stop = threading.Event()
    try:
        run_webview_ui(stop, theme="light" if args.light else "dark")
    except ImportError:
        print("pywebview not installed (pip install pywebview); "
              "using plain-text fallback window.")
        run_tk_ui(stop)


if __name__ == "__main__":
    main()
