#!/usr/bin/env bash

set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

assert_eq() {
    local expected="$1"
    local actual="$2"
    local message="$3"

    if [[ "${expected}" != "${actual}" ]]; then
        printf 'FAIL: %s\nexpected: %s\nactual:   %s\n' "${message}" "${expected}" "${actual}" >&2
        exit 1
    fi
}

tmpdir="$(mktemp -d)"
loader="$(mktemp)"
trap 'rm -rf "${tmpdir}"; rm -f "${loader}"' EXIT

awk '/^BACKUP_DIR=/{exit} {print}' "${repo_root}/install.sh" > "${loader}"
# shellcheck disable=SC1090
source "${loader}"

touch "${tmpdir}/LDO_Leviathan_v1.2.cfg"
touch "${tmpdir}/BTT_Manta_M8P_v1.1.cfg"
touch "${tmpdir}/MY-OWN-CUSTOM-TEMPLATE.cfg"

entries=()
while IFS= read -r entry; do
    entries+=("${entry}")
done < <(build_template_menu_entries "${tmpdir}")

assert_eq 3 "${#entries[@]}" "expected one menu entry per template file"
assert_eq "BTT Manta M8P v1.1	${tmpdir}/BTT_Manta_M8P_v1.1.cfg" "${entries[0]}" "entries should be sorted by human-readable label"
assert_eq "LDO Leviathan v1.2	${tmpdir}/LDO_Leviathan_v1.2.cfg" "${entries[1]}" "underscores should be rendered as spaces"
assert_eq "My Own Custom Template	${tmpdir}/MY-OWN-CUSTOM-TEMPLATE.cfg" "${entries[2]}" "special template should get title-cased output"

template_categories=()
while IFS= read -r category; do
    template_categories+=("${category}")
done < <(find "${repo_root}/user_templates/mcu_defaults" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
assert_eq "expander main mmu toolhead" "${template_categories[*]}" "expected installer coverage for every MCU template category"

for category in "${template_categories[@]}"; do
    if ! grep -q "mcu_defaults/${category}" "${repo_root}/install.sh"; then
        printf 'FAIL: installer does not reference mcu_defaults/%s\n' "${category}" >&2
        exit 1
    fi
done

if grep -q 'mcu_defaults/expand"' "${repo_root}/install.sh"; then
    printf 'FAIL: installer still references stale mcu_defaults/expand path\n' >&2
    exit 1
fi

# Strip comments first, so a mention in a comment cannot satisfy these checks.
installer_code="$(sed 's/#.*//' "${repo_root}/install.sh")"

if ! printf '%s\n' "${installer_code}" | grep -q 'if ! python3 .*sync_variables.py'; then
    printf 'FAIL: sync_variables.py call is missing or unguarded; a failure would abort the update\n' >&2
    exit 1
fi

# The sync must run before Klipper is restarted, or the restart cannot pick it up.
sync_line="$(printf '%s\n' "${installer_code}" | grep -n '^sync_variables$' | cut -d: -f1)"
restart_line="$(printf '%s\n' "${installer_code}" | grep -n '^restart_klipper$' | cut -d: -f1)"
if [[ -z "${sync_line}" || -z "${restart_line}" ]]; then
    printf 'FAIL: sync_variables or restart_klipper is not called from the run block\n' >&2
    exit 1
fi
if (( sync_line >= restart_line )); then
    printf 'FAIL: sync_variables must run before restart_klipper\n' >&2
    exit 1
fi

# A failing sync must not stop the update: run the guard with a stub that fails.
stub_dir="$(mktemp -d)"
printf '#!/usr/bin/env bash\nexit 1\n' > "${stub_dir}/python3"
chmod +x "${stub_dir}/python3"
if ! ( set -eu
       PATH="${stub_dir}:${PATH}"
       FRIX_CONFIG_PATH="${repo_root}" USER_CONFIG_PATH="${stub_dir}"
       eval "$(awk '/^function sync_variables/,/^}/' "${repo_root}/install.sh")"
       sync_variables ); then
    printf 'FAIL: a failing sync aborts install.sh under set -eu\n' >&2
    rm -rf "${stub_dir}"
    exit 1
fi
rm -rf "${stub_dir}"

# The success path is the one every user takes: a python3 that exits 0 must
# let sync_variables return 0 so the update continues normally.
stub_dir="$(mktemp -d)"
printf '#!/usr/bin/env bash\nexit 0\n' > "${stub_dir}/python3"
chmod +x "${stub_dir}/python3"
if ! ( set -eu
       PATH="${stub_dir}:${PATH}"
       FRIX_CONFIG_PATH="${repo_root}" USER_CONFIG_PATH="${stub_dir}"
       eval "$(awk '/^function sync_variables/,/^}/' "${repo_root}/install.sh")"
       sync_variables ); then
    printf 'FAIL: a successful sync should not abort install.sh under set -eu\n' >&2
    rm -rf "${stub_dir}"
    exit 1
fi
rm -rf "${stub_dir}"

printf 'ok\n'
