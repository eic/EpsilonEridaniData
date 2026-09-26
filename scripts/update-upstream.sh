#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_URL="https://github.com/TauCetiProject/TauCetiData.git"

# Add upstream remote if it doesn't exist
if ! git remote get-url upstream >/dev/null 2>&1; then
    git remote add upstream "$UPSTREAM_URL"
fi

echo "Fetching upstream..."
git fetch upstream main

echo "Checking out non-data files from upstream/main..."
# We explicitly EXCLUDE README.md to avoid overwriting EpsilonEridani customizations
git checkout upstream/main -- schema/ scripts/ docs/ eval/prompts/ .gitignore || true

echo "Successfully pulled latest code/schema from upstream."
echo "Review the changes with 'git status' and 'git diff', then commit."
