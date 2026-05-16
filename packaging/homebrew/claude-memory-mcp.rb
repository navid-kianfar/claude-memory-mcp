# Homebrew formula for Claude Memory MCP.
#
# This file is the canonical source. To publish it, copy it into a Homebrew
# tap repository (see packaging/homebrew/README.md) as:
#     Formula/claude-memory-mcp.rb
#
# Until a tagged release exists, install the latest main branch with:
#     brew install --HEAD navid-kianfar/tap/claude-memory-mcp
class ClaudeMemoryMcp < Formula
  desc "Persistent, searchable per-project memory for Claude Code"
  homepage "https://github.com/navid-kianfar/claude-memory-mcp"
  url "https://github.com/navid-kianfar/claude-memory-mcp/archive/refs/tags/v0.6.0.tar.gz"
  # Replace with the real tarball checksum when cutting a release:
  #   curl -sL <url> | shasum -a 256
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/navid-kianfar/claude-memory-mcp.git", branch: "main"

  depends_on "node" => :build
  depends_on "uv" => :build
  depends_on "python@3.12"

  def install
    # 1. Build the React management UI into frontend/dist.
    cd "frontend" do
      system "npm", "ci"
      system "npm", "run", "build"
    end

    # 2. Install the Python package + dependencies into an isolated venv.
    venv = libexec/"venv"
    python = Formula["python@3.12"].opt_bin/"python3.12"
    system "uv", "venv", venv, "--python", python
    system "uv", "pip", "install", "--python", "#{venv}/bin/python", "."

    # 3. Keep the built UI somewhere stable and tell the daemon where it is.
    (libexec/"ui").install Dir["frontend/dist/*"]

    # 4. Wrapper scripts: run the venv binaries with the right environment.
    %w[memory-mcp memory-mcp-serve memory-mcp-setup].each do |exe|
      (bin/exe).write_env_script "#{venv}/bin/#{exe}",
        PATH:               "#{venv}/bin:$PATH",
        MEMORY_MCP_UI_DIR:  "#{libexec}/ui"
    end
  end

  service do
    run [opt_bin/"memory-mcp", "serve"]
    keep_alive true
    log_path var/"log/claude-memory-mcp.log"
    error_log_path var/"log/claude-memory-mcp.log"
  end

  def caveats
    <<~EOS
      The memory daemon serves the MCP endpoint and the management UI on
      http://127.0.0.1:8765 . Start it in the background with:

        brew services start claude-memory-mcp

      Then register it with Claude Code:

        claude mcp add --transport http memory http://127.0.0.1:8765/mcp

      Management UI: http://127.0.0.1:8765/
    EOS
  end

  test do
    assert_match "memory-mcp", shell_output("#{bin}/memory-mcp --help")
  end
end
