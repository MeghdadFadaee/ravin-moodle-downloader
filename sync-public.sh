#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$SCRIPT_DIR"
readonly SOURCE_DIR="${PROJECT_ROOT}/public/"

destination="${SYNC_PUBLIC_DESTINATION:-}"
dry_run=false
delete_remote=false

usage() {
    cat <<'EOF'
Usage: ./sync-public.sh [OPTIONS]

Sync public/ to the server while excluding video files.

Options:
  -n, --dry-run              Show what would change without transferring files
      --delete               Delete remote non-video files missing locally
  -d, --destination TARGET   rsync SSH destination (USER@HOST:PATH)
  -h, --help                 Show this help

The destination is required. Pass --destination or set the
SYNC_PUBLIC_DESTINATION environment variable.
EOF
}

while (($# > 0)); do
    case "$1" in
        -n|--dry-run)
            dry_run=true
            ;;
        --delete)
            delete_remote=true
            ;;
        -d|--destination)
            if (($# < 2)); then
                printf 'Error: %s requires a destination.\n' "$1" >&2
                exit 2
            fi
            destination="$2"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Error: unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if ! command -v rsync >/dev/null 2>&1; then
    printf 'Error: rsync is not installed locally.\n' >&2
    exit 1
fi

if [[ -z "$destination" ]]; then
    printf 'Error: no destination provided. Use --destination USER@HOST:PATH.\n\n' >&2
    usage >&2
    exit 2
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
    printf 'Error: source directory does not exist: %s\n' "$SOURCE_DIR" >&2
    exit 1
fi

rsync_args=(
    --archive
    --compress
    --human-readable
    --partial
    --progress
    --itemize-changes
)

# Match lowercase and uppercase forms without relying on a platform-specific
# case-insensitive rsync option. Partial video downloads are excluded too.
video_patterns=(
    '*.[mM][pP]4' '*.[mM][pP]4.part'
    '*.[mM]4[vV]' '*.[mM]4[vV].part'
    '*.[mM][kK][vV]' '*.[mM][kK][vV].part'
    '*.[mM][oO][vV]' '*.[mM][oO][vV].part'
    '*.[aA][vV][iI]' '*.[aA][vV][iI].part'
    '*.[wW][eE][bB][mM]' '*.[wW][eE][bB][mM].part'
    '*.[fF][lL][vV]' '*.[fF][lL][vV].part'
    '*.[wW][mM][vV]' '*.[wW][mM][vV].part'
    '*.[mM][pP][eE][gG]' '*.[mM][pP][eE][gG].part'
    '*.[mM][pP][gG]' '*.[mM][pP][gG].part'
    '*.[3][gG][pP]' '*.[3][gG][pP].part'
    '*.[oO][gG][vV]' '*.[oO][gG][vV].part'
)

for pattern in "${video_patterns[@]}"; do
    rsync_args+=(--exclude="$pattern")
done

if [[ "$dry_run" == true ]]; then
    rsync_args+=(--dry-run)
fi

if [[ "$delete_remote" == true ]]; then
    rsync_args+=(--delete)
fi

printf 'Syncing %s to %s\n' "$SOURCE_DIR" "$destination"
[[ "$dry_run" == true ]] && printf 'Dry run: no files will be changed.\n'
[[ "$delete_remote" == true ]] && printf 'Mirror mode: stale remote non-video files will be deleted.\n'

rsync "${rsync_args[@]}" -- "$SOURCE_DIR" "$destination"
