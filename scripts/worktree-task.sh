#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/worktree-task.sh create <task-slug> [base-ref]
  scripts/worktree-task.sh list
  scripts/worktree-task.sh remove <task-slug>

Creates isolated task branches and worktrees next to this repository.
Runtime databases and shared services are deliberately not copied.
EOF
}

repo_root="$(git rev-parse --show-toplevel)"
repo_name="$(basename "$repo_root")"
worktree_root="$(dirname "$repo_root")/${repo_name}-worktrees"
action="${1:-}"

case "$action" in
  create)
    slug="${2:-}"
    base_ref="${3:-HEAD}"
    if [[ ! "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
      echo "task slug must match [a-z0-9][a-z0-9-]*" >&2
      exit 2
    fi
    branch="task/$slug"
    path="$worktree_root/$slug"
    mkdir -p "$worktree_root"
    if git show-ref --verify --quiet "refs/heads/$branch"; then
      git worktree add "$path" "$branch"
    else
      git worktree add -b "$branch" "$path" "$base_ref"
    fi
    printf 'branch=%s\nworktree=%s\n' "$branch" "$path"
    ;;
  list)
    git worktree list --porcelain
    ;;
  remove)
    slug="${2:-}"
    if [[ ! "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
      echo "task slug must match [a-z0-9][a-z0-9-]*" >&2
      exit 2
    fi
    path="$worktree_root/$slug"
    git worktree remove "$path"
    git worktree prune
    echo "removed=$path"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
