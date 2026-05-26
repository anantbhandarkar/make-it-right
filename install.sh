#!/usr/bin/env bash
# Make It Right — multi-tool installer.
# Installs the skills, reviewer agents, and AGENTS.md into the right location for your
# coding agent. Symlinks (not copies) so edits in this repo are live immediately.
#
# Usage:  ./install.sh [--tool=claude|cursor|codex|antigravity|all]   (default: claude)
#         CLAUDE_HOME / CODEX_HOME / GEMINI_HOME override the target dirs.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="claude"
for arg in "$@"; do
  case "$arg" in
    --tool=*) TOOL="${arg#*=}" ;;
    --tool)   shift; TOOL="${1:-claude}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

link() {  # link <source> <target>  (skips real files, refuses to clobber non-symlinks)
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    echo "  SKIP  $dst exists and is not a symlink — remove it first" >&2
    return
  fi
  ln -sfn "$src" "$dst"
  echo "  LINK  $dst -> $src"
}

install_skills_to() {  # install_skills_to <skills_dir>
  local dir="$1"
  for skill in "$REPO_DIR"/skills/*/; do
    link "${skill%/}" "$dir/$(basename "${skill%/}")"
  done
}

install_agents_to() {  # install_agents_to <agents_dir>
  local dir="$1"
  for agent in "$REPO_DIR"/agents/*.md; do
    link "$agent" "$dir/$(basename "$agent")"
  done
}

# ── Claude Code (and Cursor, which reads ~/.claude/{skills,agents}) ──────────────
install_claude() {
  local base="${CLAUDE_HOME:-$HOME/.claude}"
  echo "→ Claude Code  ($base)"
  install_skills_to "$base/skills"
  install_agents_to "$base/agents"
}

# ── Cursor: loads ~/.claude/{skills,agents} globally; AGENTS.md works per-repo ───
install_cursor() {
  echo "→ Cursor  (uses Claude's ~/.claude resources)"
  install_claude
  echo "  NOTE  Cursor auto-discovers the above from ~/.claude. For repo-scoped rules,"
  echo "        copy AGENTS.md into a project root (Cursor reads AGENTS.md natively)."
}

# ── Codex CLI: AGENTS.md is the always-on, documented surface ────────────────────
install_codex() {
  local base="${CODEX_HOME:-$HOME/.codex}"
  echo "→ Codex CLI  ($base)"
  link "$REPO_DIR/AGENTS.md" "$base/AGENTS.md"
  echo "  NOTE  AGENTS.md loads automatically. To wire the skills/sub-agents natively,"
  echo "        register them via Codex '/skills' and custom-agent config (see Codex docs)."
}

# ── Antigravity: SKILL.md skills + cross-tool AGENTS.md, both global ──────────────
install_antigravity() {
  local base="${GEMINI_HOME:-$HOME/.gemini}"
  echo "→ Antigravity  ($base)"
  install_skills_to "$base/antigravity/skills"
  link "$REPO_DIR/AGENTS.md" "$base/AGENTS.md"
  echo "  NOTE  Skills load on-demand by description. Reviewer agents run inline per the"
  echo "        AGENTS.md pipeline (their checklists ship inside the mir-backend skill)."
}

case "$TOOL" in
  claude)       install_claude ;;
  cursor)       install_cursor ;;
  codex)        install_codex ;;
  antigravity)  install_antigravity ;;
  all)          install_claude; echo; install_codex; echo; install_antigravity ;;
  *) echo "unknown --tool '$TOOL' (use: claude|cursor|codex|antigravity|all)" >&2; exit 2 ;;
esac

echo
echo "Done. Restart your agent to pick up the changes."
echo "Try:  /mir-backend <your backend task>   (or just describe a backend task — skills auto-trigger)"
