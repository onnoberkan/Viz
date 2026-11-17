#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h}"
delivery_dir="${project_dir:h}"
venv_dir="$project_dir/.venv"
build_dir="$project_dir/build"
app_path="$delivery_dir/Viz Test.app"

if [[ ! -x "$venv_dir/bin/python" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    builder_python="$(command -v python3.12)"
  elif [[ -x /opt/anaconda3/bin/python3.12 ]]; then
    builder_python=/opt/anaconda3/bin/python3.12
  else
    print -u2 'Python 3.12 is required to rebuild Viz Test.'
    exit 1
  fi
  "$builder_python" -m venv "$venv_dir"
  "$venv_dir/bin/python" -m pip install -r "$project_dir/requirements-build.txt"
  "$venv_dir/bin/python" -m pip install --no-deps finnpy==2.0.0
fi

mkdir -p "$build_dir"
export PYINSTALLER_CONFIG_DIR="$build_dir/pyinstaller-config"
"$venv_dir/bin/python" -m PyInstaller \
  --noconfirm --onedir --windowed --target-arch arm64 \
  --name 'Viz Test' \
  --osx-bundle-identifier 'local.intraop.recordings.test' \
  --distpath "$build_dir/dist" \
  --workpath "$build_dir/work" \
  --specpath "$build_dir" \
  --paths "$project_dir/Source" \
  "$project_dir/Source/viz_test_entry.py"

if [[ -e "$app_path" ]]; then
  rm -rf "$app_path"
fi
ditto "$build_dir/dist/Viz Test.app" "$app_path"
/usr/libexec/PlistBuddy -c 'Set :CFBundleShortVersionString 3.5.3' "$app_path/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleVersion string 71' "$app_path/Contents/Info.plist"
codesign --force --deep --sign - "$app_path"
rm -rf "$build_dir/dist"
print "Built $app_path"
