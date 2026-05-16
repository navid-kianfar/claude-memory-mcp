# Homebrew distribution

You do **not** need a Homebrew account. Homebrew has no accounts — a formula is
just a Ruby file in a Git repository. There are two ways to ship one:

## Option A — a personal tap (recommended, do this now)

A "tap" is any GitHub repo whose name starts with `homebrew-`.

1. Create a public repo named **`homebrew-tap`** under the `navid-kianfar` account:
   `https://github.com/navid-kianfar/homebrew-tap`
2. Copy this formula into it as `Formula/claude-memory-mcp.rb`.
3. Users then install with:

   ```bash
   brew tap navid-kianfar/tap          # expands to github.com/navid-kianfar/homebrew-tap
   brew install claude-memory-mcp
   ```

   Before you cut a tagged release, the main branch is installable directly:

   ```bash
   brew install --HEAD navid-kianfar/tap/claude-memory-mcp
   ```

4. Run the daemon as a background service:

   ```bash
   brew services start claude-memory-mcp
   ```

When you publish a GitHub release, update `url` and `sha256` in the formula
(`curl -sL <tarball-url> | shasum -a 256`).

## Option B — homebrew-core (`brew install claude-memory-mcp`, no tap)

Getting into the official `homebrew-core` repo means anyone can install without
tapping. It still needs only a GitHub account, but the project must meet
Homebrew's **notability** bar (roughly: 75+ stars, or 30+ forks, or 30+
watchers) and you submit a pull request that maintainers review. Start with a
tap and migrate later if the project gains traction.

## Notes

- The formula builds the React UI with Node and installs the Python package
  (and its dependencies, including PyTorch) into an isolated virtualenv with
  `uv`. The first install downloads a few hundred MB — this is expected for an
  ML-backed tool.
- `MEMORY_MCP_UI_DIR` is baked into the wrapper scripts so the daemon finds the
  built UI regardless of where Homebrew places the package.
- Keep `packaging/homebrew/claude-memory-mcp.rb` in this repo as the canonical
  copy and sync changes into the tap repo.
