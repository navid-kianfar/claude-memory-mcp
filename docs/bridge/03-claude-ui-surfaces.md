# Showing asoode inside Claude — what actually exists

> Captured 2026-09-03 by an Opus agent researching the official Claude Code docs.
> Question asked: *"can we have a window like claude preview that inside the claude i can load my
> project management app? (claude plugin?!)"*

## Bottom line

**Yes — the Browser pane.** It is a real, tabbed, user-interactive browser pane in the Claude Desktop
**Code tab**, it can attach to an already-running server, and it can open arbitrary hosted URLs.

**A plugin cannot do this.** There is no UI/webview/iframe extension point in the plugin system at all.

---

## 1. The Browser pane

A pane in the Claude Desktop **Code tab** (not the terminal CLI), in the same drag-and-drop pane system
as chat, diff, terminal, file editor, tasks, and the iOS Simulator.

- Toggle: **Cmd+Shift+B** (macOS), or the **Views** menu. **Cmd+Shift+S** selects an element.
  **Cmd+\\** closes the focused pane.
- **It is not agent-only.** The docs list as its first capability: *"Interact with your running app
  directly in the Browser pane."* It is *"a tabbed browser, so you can open documentation, issue
  trackers, or any other site next to your running app,"* and *"You can sign in to sites in the pane,
  including popup sign-in flows such as Google OAuth."* Claude's own verification (screenshots, DOM
  inspection, clicking, form filling) is a **separate, additional** capability on the same pane.
- It is a pane in the session layout, so it **persists across turns**.
- It also opens static HTML, PDFs, images and videos from the project (click a path in chat).

Source: [desktop.md — Preview your app](https://code.claude.com/docs/en/desktop#preview-your-app),
[Browse external sites](https://code.claude.com/docs/en/desktop#browse-external-sites).

> The tool names `preview_start` / `navigate` are **not** in the public
> [tools reference](https://code.claude.com/docs/en/tools-reference). Treat them as internal;
> don't build anything on them.

### `.claude/launch.json` — attaching to an already-running server

This is the key fact for a docker-compose app. From
[Configure preview servers](https://code.claude.com/docs/en/desktop#configure-preview-servers):

> To preview a server you already run yourself, set `url` without a command. Claude attaches the
> preview to your running server instead of starting one.

**Constraints on `url`:**

| Constraint | Detail |
|---|---|
| Scheme | must be `http` or `https` |
| Credentials | must not contain a username or password |
| Localhost origins | `localhost`, any `*.localhost`, `127.0.0.1`, `::1` — open directly, no permission prompt |
| Localhost path/query | **Forbidden** — "a localhost `url` must be just your server's origin — no path or query" |
| Localhost port | **Must match the entry's `port` field**, which **defaults to 3000**. A mismatch is a configuration error naming the url and the fix |
| External https | Permission prompt on first open ("Always allow" persists); **paths are allowed** here |
| Org policy | `disableBrowserExternalNavigation` and `browserExternalPageTools` managed settings still apply |

⚠️ **Practical gotcha:** because `port` defaults to 3000 and a localhost `url`'s port must match it,
an app on 5174 needs **both** `"port": 5174` and `"url": "http://localhost:5174"`.

Other fields: `name`, `runtimeExecutable`, `runtimeArgs`, `cwd`, `env`, `autoPort`, `program`, `args`.
JSON-with-comments is supported. The file lives at the root of **the folder selected when the session
started** — a parent folder will not auto-detect a subfolder's servers. It is meant to be committed
("Don't put secrets here since this file is committed to your repo").

⚠️ **`autoVerify` defaults ON** — Claude re-verifies in the browser after every edit. For a
"just show me the board" pane that is noise; set `"autoVerify": false` or turn it off in the server dropdown.

### Auth inside the pane

1. **It is not your real Chrome profile.** *"The Browser pane uses a clean browser profile, separate
   from your personal browser, with none of your saved logins or history."*
2. **It has its own cookie jar; persistence is opt-in.** *"Persist cookies and local storage across
   server restarts by selecting **Persist sessions** in the dropdown, so you don't have to re-login
   during development."* Settings → Claude Code has a "clear saved session data" control, implying
   on-disk storage — but the docs only promise persistence *across server restarts*, not across app
   restarts. Assume the former; verify empirically.
3. **You can log in yourself**, including OAuth popups.

For asoode: first open = logged out. Sign in once, enable **Persist sessions**. If asoode ever gains
SSO bound to the user's real browser profile (passkeys, corporate IdP), that flow may not complete in
the clean profile — that is the case for Claude in Chrome instead.

---

## 2. Plugins — no UI extension point, at all

A plugin can contain: manifest (`.claude-plugin/plugin.json`), `skills/`, `commands/`, `agents/`,
`hooks/hooks.json`, `.mcp.json`, `.lsp.json`, `workflows/`, `output-styles/`,
`monitors/monitors.json` (experimental), `themes/` (experimental), `bin/` (on the Bash `PATH`),
`settings.json` (`agent`, `subagentStatusLine` only).

**There is no webview, panel, tab, iframe, HTML-render, or custom-view component.** The closest things
are deliberately not UI: **output styles** reformat Claude's *text* (Markdown); **themes** override CLI
color tokens; **channels** inject text messages via MCP.

⚠️ **`launch.json` is not a plugin component either** — it is project config in `.claude/`, so a plugin
cannot ship the Browser pane config. Commit `.claude/launch.json` to the asoode repo to share it.

---

## 3. Every other surface, and why it doesn't work

| Surface | Can it show a live external web app? | Hard limits |
|---|---|---|
| **Browser pane** (Desktop Code tab) | **Yes** — full, interactive, persistent, tabbed | Desktop only; clean profile; external sites prompt once; org settings can restrict |
| **Artifacts** (claude.ai pages) | **No** | Static single page, no backend. CSP allows external subresources only from Google Fonts, `cdnjs`, `cdn.jsdelivr.net`, `cdn.tailwindcss.com`, `code.jquery.com`; `fetch`/XHR/WebSocket reach **only the page's own origin** and the Fonts hosts. **Cannot reach localhost or a self-hosted API.** No documented third-party-iframe allowance. 16 MiB max |
| **Artifacts + MCP connectors** | Live *data*, not the app | Only **claude.ai account connectors** qualify. *"Local MCP servers you configure in Claude Code … can supply data while Claude builds the page, but the published page can't call them."* Viewers must approve; can never be shared publicly |
| **MCP elicitation** | No | Transient blocking modal: server-defined **form** fields, or **URL** mode that opens a browser URL for auth. Not arbitrary HTML, not persistent |
| **MCP resources** | No | `ListMcpResourcesTool`/`ReadMcpResourceTool` pull content into Claude's **context**. No rendering |
| **MCP Apps / `ui://`** | **Not in Claude Code** | The extension was ratified 2026-01-26; shipped hosts are **Claude web/mobile/desktop Chat**, **Cowork**, VS Code (Copilot), Goose, ChatGPT, Postman. Claude Code is named in neither the MCP announcement nor Anthropic's post, and `mcp.md` never mentions `ui://`, widgets, or HTML rendering |
| **VS Code / JetBrains extensions** | No Claude-provided browser pane | They give a chat panel, inline diffs, plan review, tabs. Browser work routes through Claude in Chrome. (VS Code's own Simple Browser can show localhost, but that's a VS Code feature) |
| **Claude in Chrome** | Yes — but a separate real Chrome window | *"Claude opens new tabs for browser tasks and shares your browser's login state."* Needs the extension ≥ 1.0.36 and a direct Anthropic plan; **force-disabled with API-key or `setup-token` auth**; unavailable on Bedrock/Foundry/WSL. Not a pane inside Claude Code |

---

## 4. Recommendation for asoode

**1. Browser pane attached to the local stack.** In the **asoode repo** (the folder the session opens),
`.claude/launch.json`:

```jsonc
{
  "version": "0.0.1",
  // Claude re-verifies after every edit by default; off for a "just show me the board" pane
  "autoVerify": false,
  "configurations": [
    {
      "name": "asoode-web",
      "port": 5174,                    // MUST equal the url's port for localhost
      "url": "http://localhost:5174"   // no command => attach to the running dev server
    },
    {
      "name": "asoode-api",
      "port": 3000,
      "url": "http://localhost:3000"   // Swagger lives at /docs — navigate after it opens
    }
  ]
}
```

For the docker-compose stack the frontend is on **:80**, so use `"port": 80` +
`"url": "http://localhost"`. Bring the stack up yourself, then **Cmd+Shift+B**, sign in once, enable
**Persist sessions**. Localhost needs no site approval, so Claude can also drive the board without prompts.

**2. Second tab for the hosted instance.** Either another `launch.json` entry with an external `url`
(paths allowed) or just navigate. One-time "Always allow" per domain.

**3. An iframe tab inside memory-mcp's own UI at `127.0.0.1:8765`** — possible (memory-mcp sets no CSP)
but asoode must send permissive `X-Frame-Options`/`frame-ancestors`, and there is no cross-origin cookie
story. **Not worth it while the Browser pane exists.** Prefer deep links from the memory-mcp Tasks tab
into asoode, opened in the pane.

**4. A plugin — for automation, never for UI.** Ship a skill + `.mcp.json` + hooks. Nothing a plugin
contains can draw a panel. If a plugin-provided pane is what's wanted, that is a legitimate feature
request via `/feedback`.

**Do not chase:** a plugin webview/iframe/custom tab; an MCP server rendering interactive HTML in a Code
session; an Artifact that embeds or fetches from a self-hosted app; a browser pane in the terminal CLI.
