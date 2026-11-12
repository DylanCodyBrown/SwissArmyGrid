#!/bin/sh
# Install SwissArmyGrid __VERSION__ (macOS and Linux): uv if it is missing, then the grid server as a uv tool,
# with its own Python (uv downloads it; no system Python needed).
#
#   sh install.sh                                          # download the wheel from the GitHub release
#   sh install.sh ./swissarmygrid-__VERSION__-py3-none-any.whl  # or install a wheel you downloaded
#
# The repository is private: downloading needs the GitHub CLI signed in (gh auth login), or download the
# wheel from the release page yourself and pass its path.
set -eu

VERSION="__VERSION__"
REPO="DylanCodyBrown/SwissArmyGrid"
WHEEL="swissarmygrid-${VERSION}-py3-none-any.whl"

say() { printf '%s\n' "$*"; }
fail() { printf 'SwissArmyGrid install: %s\n' "$*" >&2; exit 1; }

# 1. uv: the installer uses uv's own install script, which puts uv in ~/.local/bin.
if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    PATH="$HOME/.local/bin:$PATH"
  else
    say "Installing uv (the tool that installs SwissArmyGrid and its Python)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh || fail "uv could not be installed; see https://docs.astral.sh/uv/"
    PATH="$HOME/.local/bin:$PATH"
  fi
fi
command -v uv >/dev/null 2>&1 || fail "uv is installed but not on PATH; open a new terminal and run this again."

# 2. The wheel: a path given on the command line, or the release's wheel.
if [ "$#" -ge 1 ]; then
  [ -f "$1" ] || fail "no such file: $1"
  wheel_path="$1"
else
  dir=$(mktemp -d)
  trap 'rm -rf "$dir"' EXIT
  wheel_path="$dir/$WHEEL"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    say "Downloading $WHEEL from the v$VERSION release..."
    gh release download "v$VERSION" --repo "$REPO" --pattern "$WHEEL" --dir "$dir" \
      || fail "could not download the release with gh."
  else
    url="https://github.com/$REPO/releases/download/v$VERSION/$WHEEL"
    say "Downloading $url ..."
    curl -fLsS -o "$wheel_path" "$url" || fail "could not download the wheel (the repository is private).
  Sign in to the GitHub CLI (gh auth login) and run this again, or download $WHEEL from
  https://github.com/$REPO/releases/tag/v$VERSION and run: sh install.sh <path to the wheel>"
  fi
fi

# 3. The server, as a uv tool in its own environment.
say "Installing the SwissArmyGrid server $VERSION..."
uv tool install --force --python 3.12 "${wheel_path}[server]" || fail "uv could not install the server."
uv tool update-shell >/dev/null 2>&1 || true

say ""
say "SwissArmyGrid $VERSION is installed."
say "  Start it:              swissarmygrid start"
say "  The Python client:     uv pip install \"$WHEEL\"   (in any project's environment)"
say "  Then, in Python:       import swissarmygrid as sag; sag.push(df)"
say "(Open a new terminal first if the swissarmygrid command is not found.)"
