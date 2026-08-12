# plainclaude


A small Windows tool that watches [Claude Desktop](https://claude.ai/download), and every time Claude finishes a response, rewrites it in plainer language with a **local** LLM (via [Ollama](https://ollama.com)) and shows the rewrite in a separate always-on-top window — with markdown and LaTeX rendered properly.

Claude's answers are often correct but dense. This serves as a reading aid, giving you a live plain-language side-channel without changing anything about Claude itself. 

<img width="1280" height="800" alt="Screenshot_1" src="https://github.com/user-attachments/assets/b3227075-9ade-4264-8409-02a950cc3c55" />
<img width="1280" height="799" alt="Screenshot_2" src="https://github.com/user-attachments/assets/63f324a3-b85e-481d-8889-dd2113eeaabe" />

## How it works

- The script reads Claude Desktop's **Windows UI Automation (accessibility) tree** — the same OS interface screen readers like Narrator use. Chromium builds this tree lazily, so the script sends the standard MSAA probe (`AccessibleObjectFromWindow`) at attach to switch it on.
- Claude Desktop's tree labels each message (`Claude responded:` / `You said:`) and exposes a status bar that flips to *"Claude finished the response"* — so the tool extracts exactly the last completed assistant message, plus the user question and previous answer as context.
- Tool-use cards ("Ran a command", thinking-block headers) are captured as activity markers so the rewrite can say *"Claude ran a benchmark and found..."* instead of silently converting measurements into bare claims.
- The bundle goes to your local Ollama model with a prompt tuned for **faithful unpacking**: preserve every claim, number, and caveat; resolve symbols in words; full sentences instead of telegraphic compression.
- The rewrite renders in a pywebview window (marked + KaTeX).

**This tool sends nothing to Claude.** It is a passive, read-only observer of your own conversation, on your own machine. See [Privacy & Terms](#privacy--terms).

## Requirements

- Windows 10/11
- Claude Desktop (see [Compatibility](#compatibility))
- Python 3.10+
- [Ollama for Windows](https://ollama.com/download) with a pulled model (default: `qwen3:14b`; `qwen3:8b` works on smaller GPUs)
- Internet access at window startup (KaTeX/marked load from cdnjs; the LLM itself is fully local)

## Install & run

```powershell
pip install -r requirements.txt
ollama pull qwen3:14b
# Launch Claude Desktop normally, then:
python plainclaude.py
```

Debugging helpers:

```powershell
python plainclaude.py --dump        # print the exact prompt that would be sent to the model
python plainclaude.py --dump-tree   # print the accessibility subtree of the chat
python plainclaude.py --light       # light theme (default is dark)
```

If the tool attaches but extracts nothing, toggle Windows Narrator once (Win+Ctrl+Enter, wait 3 s, again to close) and retry — Chromium keeps accessibility enabled for the app's lifetime after any screen reader is detected.

## Configuration

Constants at the top of `plainclaude.py`:

| Constant | Meaning |
|---|---|
| `OLLAMA_MODEL` | Any Ollama chat model. Larger models drop fewer details. |
| `OLLAMA_URL` | Ollama endpoint (default `localhost:11434`). |
| `REWRITE_INSTRUCTIONS` | The rewrite prompt. Tune to taste. |
| `NOISE_BUTTONS` | Button labels treated as UI chrome rather than activity. |
| UI strings (`Primary pane`, `Chat messages`, `Claude responded:`, ...) | **Locale- and version-dependent** — see below. |

## Compatibility

Verified against **Claude Desktop 1.28929.0 (MSIX), English UI, Windows 11**. The extraction depends on accessibility labels Anthropic ships in the app (`'Message N of M'`, `'Claude responded:'`, `'finished the response'`, ...). A UI update or a non-English locale can break matching — if that happens, run `--dump-tree`, find the new labels, and update the constants. Contributions of label sets for other locales are welcome.

Works with both regular chats and Cowork sessions, provided the transcript uses the labeled message structure.

## Limitations

- Windows only (UIA, `oleacc`).
- Only the **rendered** conversation is visible: very long chats virtualize older messages out of the tree, so context extraction is best-effort near the top of a conversation.
- Rewrites are produced by a small local model. They can still omit or distort details — treat the original as authoritative. The `--dump` command shows exactly what the model was given.

## Privacy & Terms

Design properties, all verifiable in ~600 lines of code:

- **Read-only.** The tool never sends messages, clicks, keystrokes, or any input to Claude. Every interaction with Claude is performed by you, by hand.
- **No credentials, no network interception.** It does not touch session tokens, cookies, or API keys, and does not proxy or inspect traffic. It reads the same accessibility interface Windows Narrator reads.
- **Fully local processing.** Conversation text goes only to your own Ollama instance on `localhost`. No telemetry, no logging, no storage — rewrites exist only in the display window and are discarded when it closes.
- **No dataset building, no training.** Outputs are used for one-shot inference and thrown away. This tool does not collect Claude outputs for any purpose.

This project is not affiliated with or endorsed by Anthropic. You are responsible for ensuring your own use complies with [Anthropic's Consumer Terms of Service](https://www.anthropic.com/legal/consumer-terms). The authors' understanding is that a passive, local, read-only accessibility aid that automates no access to the service is consistent with those terms, but that is not legal advice.

## Testing

See [REGRESSION.md](REGRESSION.md) for the standard regression exchange and pass criteria — run it after prompt changes or after re-mapping UI labels following a Claude Desktop update.

## License

MIT — see [LICENSE](LICENSE).
