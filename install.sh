#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_ROOT=${SCOUT_USAGE_INSTALL_ROOT:-"$HOME/.local/share/scout-usage-tracker"}
BIN_DIR=${SCOUT_USAGE_BIN_DIR:-"$HOME/.local/bin"}
CONFIG_DIR=${SCOUT_USAGE_CONFIG_DIR:-"$HOME/.config/scout-usage-tracker"}
CONFIG_PATH="$CONFIG_DIR/config.json"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABEL="local.scout-usage-tracker"
PLIST="$LAUNCH_DIR/$LABEL.plist"
SKILL_DIR="$HOME/.codex/skills/scout-usage"
OWNER_MARKER=".scout-usage-tracker-owned"
INSTALL_MARKER="$INSTALL_ROOT/$OWNER_MARKER"
CONFIG_MARKER="$CONFIG_DIR/$OWNER_MARKER"
BIN_MARKER="$BIN_DIR/.scout-usage-owned"
SKILL_MARKER="$SKILL_DIR/$OWNER_MARKER"
LAUNCH_MARKER="$INSTALL_ROOT/.launchagent-installed"

usage() {
  echo "Usage: $0 {install|update|status|open|uninstall} [--enable-auto-update] [--install-skill] [--purge-data]"
}

action=${1:-install}
if [ "$#" -gt 0 ]; then shift; fi
enable_auto=false
install_skill_flag=false
purge=false

case "$action" in
  install|update)
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --enable-auto-update) enable_auto=true ;;
        --install-skill) install_skill_flag=true ;;
        *) usage >&2; exit 2 ;;
      esac
      shift
    done
    ;;
  uninstall)
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --purge-data) purge=true ;;
        *) usage >&2; exit 2 ;;
      esac
      shift
    done
    ;;
  status|open)
    [ "$#" -eq 0 ] || { usage >&2; exit 2; }
    ;;
  *) usage >&2; exit 2 ;;
esac

if [ "$enable_auto" = true ] && [ "$(uname -s)" != "Darwin" ]; then
  echo "Automatic update is supported only on macOS." >&2
  exit 1
fi

check_requirements() {
  command -v python3 >/dev/null 2>&1 || { echo "Python 3.10 or newer is required." >&2; exit 1; }
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 or newer is required." >&2; exit 1; }
  python3 -c 'import sqlite3; assert sqlite3.sqlite_version' || { echo "Python SQLite support is required." >&2; exit 1; }
}

validate_paths() {
  python3 -c '
import pathlib, sys
home_raw = pathlib.Path(sys.argv[1])
if not home_raw.is_absolute() or home_raw == pathlib.Path("/"):
    raise SystemExit("Unsafe HOME: expected an absolute, non-root directory")
home = home_raw.resolve()
resolved = []
for raw in sys.argv[2:7]:
    candidate_raw = pathlib.Path(raw)
    if not candidate_raw.is_absolute():
        raise SystemExit("Unsafe install path: expected an absolute path strictly under HOME")
    candidate = candidate_raw.resolve()
    if candidate == home or home not in candidate.parents:
        raise SystemExit("Unsafe install path: all managed paths must resolve strictly under HOME")
    resolved.append(candidate)
if resolved[0].name != "scout-usage-tracker" or resolved[2].name != "scout-usage-tracker":
    raise SystemExit("Unsafe tracker root: install and config directories must be named scout-usage-tracker")
for index, left in enumerate(resolved):
    for right in resolved[index + 1:]:
        if left == right or left in right.parents or right in left.parents:
            raise SystemExit("Unsafe install path: managed paths must be pairwise disjoint")
project = pathlib.Path(sys.argv[7]).resolve()
for candidate in resolved:
    if candidate == project or candidate in project.parents or project in candidate.parents:
        raise SystemExit("Unsafe install path: managed paths must not overlap the source package")
' "$HOME" "$INSTALL_ROOT" "$BIN_DIR" "$CONFIG_DIR" "$LAUNCH_DIR" "$SKILL_DIR" "$PROJECT_DIR"
}

require_owned_or_absent() {
  managed_path=$1
  marker=$2
  if [ -e "$managed_path" ] || [ -L "$managed_path" ]; then
    [ -d "$managed_path" ] || { echo "Refusing non-directory managed root: $managed_path" >&2; exit 1; }
    [ -f "$marker" ] || { echo "Refusing pre-existing unowned tracker directory: $managed_path" >&2; exit 1; }
  fi
}

validate_ownership() {
  require_owned_or_absent "$INSTALL_ROOT" "$INSTALL_MARKER"
  require_owned_or_absent "$CONFIG_DIR" "$CONFIG_MARKER"
  if [ "$action" = install ] || [ "$action" = update ]; then
    if [ -e "$BIN_DIR/scout-usage" ] && [ ! -f "$BIN_MARKER" ]; then
      echo "Refusing to overwrite unowned launcher: $BIN_DIR/scout-usage" >&2
      exit 1
    fi
    if [ "$install_skill_flag" = true ]; then
      require_owned_or_absent "$SKILL_DIR" "$SKILL_MARKER"
    fi
    if [ "$enable_auto" = true ] && [ -e "$PLIST" ] && [ ! -f "$LAUNCH_MARKER" ]; then
      echo "Refusing to overwrite unowned LaunchAgent: $PLIST" >&2
      exit 1
    fi
  fi
}

write_launcher() {
  mkdir -p "$BIN_DIR"
  chmod 700 "$BIN_DIR"
  temp_launcher="$BIN_DIR/.scout-usage.tmp.$$"
  {
    echo '#!/bin/sh'
    printf '%s\n' "export PYTHONPATH='$INSTALL_ROOT/src'"
    printf '%s\n' "exec python3 -m scout_usage_tracker --config '$CONFIG_PATH' \"\$@\""
  } > "$temp_launcher"
  chmod 700 "$temp_launcher"
  mv "$temp_launcher" "$BIN_DIR/scout-usage"
  : > "$BIN_MARKER"
  chmod 600 "$BIN_MARKER"
}

copy_program() {
  mkdir -p "$INSTALL_ROOT" "$CONFIG_DIR"
  chmod 700 "$INSTALL_ROOT" "$CONFIG_DIR"
  : > "$INSTALL_MARKER"
  : > "$CONFIG_MARKER"
  chmod 600 "$INSTALL_MARKER" "$CONFIG_MARKER"
  rm -rf "$INSTALL_ROOT/src" "$INSTALL_ROOT/templates" "$INSTALL_ROOT/skills"
  cp -R "$PROJECT_DIR/src" "$PROJECT_DIR/templates" "$PROJECT_DIR/skills" "$INSTALL_ROOT/"
  cp "$PROJECT_DIR/config.example.json" "$INSTALL_ROOT/config.example.json"
  if [ ! -e "$CONFIG_PATH" ]; then
    cp "$PROJECT_DIR/config.example.json" "$CONFIG_PATH"
  fi
  chmod 600 "$CONFIG_PATH" "$INSTALL_ROOT/config.example.json"
  write_launcher
}

check_scout_db() {
  source_db=${SCOUT_USAGE_SOURCE_DB:-"$HOME/.scout/copilot/session-store.db"}
  if [ -r "$source_db" ]; then
    echo "Scout database readable: $source_db"
  else
    echo "Scout database not found at configured default; edit $CONFIG_PATH before update."
  fi
}

enable_auto_update() {
  command -v launchctl >/dev/null 2>&1 || { echo "launchctl is required." >&2; exit 1; }
  mkdir -p "$LAUNCH_DIR"
  temp_plist="$LAUNCH_DIR/.$LABEL.tmp.$$"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo '<key>Label</key><string>local.scout-usage-tracker</string>'
    printf '<key>ProgramArguments</key><array><string>%s/scout-usage</string><string>update</string></array>\n' "$BIN_DIR"
    echo '<key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>15</integer></dict>'
    printf '<key>StandardOutPath</key><string>%s/refresh.log</string>\n' "$INSTALL_ROOT"
    printf '<key>StandardErrorPath</key><string>%s/refresh.log</string>\n' "$INSTALL_ROOT"
    echo '</dict></plist>'
  } > "$temp_plist"
  chmod 600 "$temp_plist"
  mv "$temp_plist" "$PLIST"
  : > "$LAUNCH_MARKER"
  : >> "$INSTALL_ROOT/refresh.log"
  chmod 600 "$LAUNCH_MARKER" "$INSTALL_ROOT/refresh.log"
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null
  echo "Automatic update enabled and verified."
}

install_skill() {
  mkdir -p "$(dirname "$SKILL_DIR")"
  if [ -d "$SKILL_DIR" ]; then
    rm -rf "$SKILL_DIR"
  fi
  cp -R "$PROJECT_DIR/skills/scout-usage" "$SKILL_DIR"
  : > "$SKILL_MARKER"
  chmod 600 "$SKILL_MARKER"
  echo "Installed Codex skill at $SKILL_DIR"
}

do_uninstall() {
  if [ -f "$LAUNCH_MARKER" ]; then
    if command -v launchctl >/dev/null 2>&1 && [ -e "$PLIST" ]; then
      launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    fi
    rm -f "$PLIST" "$LAUNCH_MARKER"
  fi
  if [ -f "$BIN_MARKER" ]; then
    rm -f "$BIN_DIR/scout-usage" "$BIN_MARKER"
  fi
  if [ -f "$SKILL_MARKER" ]; then
    rm -rf "$SKILL_DIR"
  fi
  if [ -f "$INSTALL_MARKER" ]; then
    rm -rf "$INSTALL_ROOT/src" "$INSTALL_ROOT/templates" "$INSTALL_ROOT/skills"
    rm -f "$INSTALL_ROOT/config.example.json"
  fi
  if [ "$purge" = true ]; then
    rm -f "$INSTALL_ROOT/history.sqlite3" "$INSTALL_ROOT/history.sqlite3-wal" "$INSTALL_ROOT/history.sqlite3-shm"
    rm -f "$INSTALL_ROOT/dashboard.html" "$INSTALL_ROOT/hmac-secret" "$INSTALL_ROOT/refresh.log"
    rm -f "$CONFIG_PATH"
    rm -f "$INSTALL_MARKER" "$CONFIG_MARKER"
    rmdir "$INSTALL_ROOT" 2>/dev/null || true
    rmdir "$CONFIG_DIR" 2>/dev/null || true
    echo "Uninstalled program and removed enumerated tracker-owned data; unrelated files were preserved."
  else
    echo "Uninstalled program; preserved config, history, secret, dashboard, logs, and ownership markers."
  fi
}

case "$action" in
  install|update|uninstall)
    check_requirements
    validate_paths
    validate_ownership
    ;;
esac

case "$action" in
  uninstall) do_uninstall ;;
  install|update)
    copy_program
    check_scout_db
    [ "$enable_auto" = false ] || enable_auto_update
    [ "$install_skill_flag" = false ] || install_skill
    echo "Installed Scout Usage Tracker. Run: $BIN_DIR/scout-usage update"
    ;;
  status|open)
    [ -x "$BIN_DIR/scout-usage" ] || { echo "Scout Usage Tracker is not installed." >&2; exit 1; }
    exec "$BIN_DIR/scout-usage" "$action"
    ;;
esac
