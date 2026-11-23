# Codex task spec — One-step install for a dedicated Mac (Intel Mac mini, macOS Monterey)

Effort: high

## Goal
Someone with a freshly set-up Mac can download this project, run one installer, type the Kasa
login once, and from then on the dashboard starts by itself at login and opens full-screen.
Settings (cameras, plugs/switches, layout, weather, backup) can be carried over from another Mac
in one file.

## Target
- macOS 12 Monterey on an Intel Mac mini (Late 2014), also macOS 13+ and Apple Silicon.
- `/bin/bash` there is 3.2; `/usr/bin/python3` is Python 3.9 from Apple's Command Line Tools and
  **does not exist as a working interpreter until the tools are installed** (running the stub
  pops up Apple's install dialog). Standard library only.

## Scope — touch ONLY these
- `install.sh` (new, repo root), `Install.command` (new, repo root, double-click wrapper)
- `scripts/kiosk.sh` (new)
- `app/install.py` (new)
- `tests/test_install.py` (new; your own cases)
- `README.md` (a short "Install on a dedicated Mac" section pointing to the guide)
- `docs/INSTALL-MAC.md` (new; the step-by-step guide)
- `.gitignore` only if a new generated path needs ignoring

Do NOT change `start.sh`, `scripts/setup.sh`, any other `app/` module, `www/`, or existing tests.

## Acceptance tests (written by Claude — do NOT modify)
- `tests/test_install_acceptance.py` and every other `tests/*_acceptance.py`, `tests/test_security.py`.
  All must pass: `/usr/bin/python3 -m unittest discover -s tests -t .`
  If one looks wrong, stop and say why in your final message instead of changing it.

## What to do

### `install.sh` (bash 3.2 compatible; `set +x`, `set -euo pipefail`)
1. `cd` to its own directory. Refuse anything but macOS.
2. If `xcode-select -p` fails: print that Apple's Command Line Tools are needed, run
   `xcode-select --install` (opens Apple's dialog), tell the user to run the installer again
   when that finishes, and exit 2. Never call `/usr/bin/python3` before this check passes.
3. `exec /usr/bin/python3 -m app.install "$@"` (default command `install`).
`Install.command`: `cd` to its directory, run `./install.sh`, and keep the Terminal window
readable at the end ("Press Return to close"). Both files executable (mode 755).

### `app/install.py`
Pure helpers fixed by the acceptance tests:
- `SERVER_LABEL = "com.kasa-dashboard.server"`, `KIOSK_LABEL = "com.kasa-dashboard.kiosk"`,
  `InstallError` (message is a fixed, user-readable sentence; never contains a password).
- `render_env(email, password, port=None)` → text for `.env` that `app.envfile.env_get` reads
  back exactly (wrap values so one pair of outer quotes is what `env_get` strips). Refuse empty
  values, non-strings, and CR/LF/NUL. `port` is an `int` 1–65535 (not `bool`) or `None`.
- `write_env(path, text, replace=False)` → atomic write, mode 600, no leftover temp file;
  refuses to overwrite unless `replace`.
- `render_server_agent(repo)` / `render_kiosk_agent(repo)` → launchd property lists (bytes, via
  `plistlib`) exactly as the tests describe: the server runs `/bin/bash <repo>/start.sh` with
  `RunAtLoad`, `KeepAlive` true, `ThrottleInterval` 30, `WorkingDirectory`, and logs to
  `<repo>/run/dashboard.log`; the kiosk runs `/bin/bash <repo>/scripts/kiosk.sh` at load, no
  `KeepAlive`. No `EnvironmentVariables`, nothing from `.env`.
- `protected_location(repo, home)` → True when the project sits where macOS blocks background
  programs or which may disappear: `~/Desktop`, `~/Documents`, `~/Downloads`,
  `~/Library/Mobile Documents`, or anything under `/Volumes`.
- `export_settings(repo, destination)` → validates `<repo>/data/cameras.json` with the store's
  own validation and writes a copy to `destination` (mode 600). Never includes `.env`.
- `import_settings(repo, source)` → reads at most 1 MiB, validates with the store's validation
  (`validate_state` must accept it unchanged — reject anything it would reset or that has
  unknown keys), keeps `backup` only if its folder is an existing directory on this Mac
  (otherwise backup is turned off: `{"folder": "", "cameras": {}}` or key removed), copies any
  existing `cameras.json` to `cameras.json.before-import-<UTC timestamp>` first, then writes
  atomically (`data/` mode 700, file 600). Returns
  `{"cameras": n, "devices": n, "backup_kept": bool}`. All failures are `InstallError` and
  change nothing.

Command line (`python3 -m app.install <command>`; `--dry-run` on every command prints the
actions without changing anything outside the terminal; `--home DIR` and an injectable command
runner exist for tests):
- `install [--settings FILE] [--port N] [--no-autostart] [--no-kiosk]`
  1. Refuse a `protected_location` with a clear sentence telling the user to move the folder to
     their home folder (e.g. `~/kasa-dashboard`) and run again.
  2. Run `scripts/setup.sh` (downloads and verifies the pinned go2rtc).
  3. If `.env` is missing: ask for the Kasa email (`input`) and password (`getpass`, typed
     twice, never echoed, never placed in argv, environment, logs or any file but `.env`).
     If `.env` exists, keep it and say so.
  4. `--settings FILE` → `import_settings`. Without it, existing settings are kept.
  5. Unless `--no-autostart`: write the two agents to `~/Library/LaunchAgents/<label>.plist`
     (mode 644), then for each: `launchctl bootout gui/<uid>/<label>` (ignore failure) and
     `launchctl bootstrap gui/<uid> <plist>`. `--no-kiosk` skips the kiosk agent (and removes
     one left from an earlier install).
  6. Wait up to 60 s for `http://127.0.0.1:<port>/api/cameras` to answer, then print a short
     summary: the address, where the log is, and the remaining manual steps from the guide
     (automatic login, restart after power failure, the first-run permission prompts, and that
     **only one computer may run the dashboard for the same cameras — stop it on the old one**).
- `uninstall` → boot out and remove both agents; leaves the project folder, `.env` and
  settings untouched and says so.
- `export FILE`, `import FILE`, `status` (agents loaded? server answering? settings counts).

### `scripts/kiosk.sh` (bash 3.2)
- Read the port from `.env` using the project's own reader
  (`/usr/bin/python3 -c` with `app.envfile.env_get`), default 1984; never print `.env`.
- Wait up to 180 s for `http://127.0.0.1:<port>/api/cameras` (`curl --silent --max-time 3`).
- Open the dashboard full-screen: if `/Applications/Google Chrome.app` exists, start it with
  `open -na "Google Chrome" --args --kiosk --app=<url> --autoplay-policy=no-user-gesture-required
  --disable-session-crashed-bubble --noerrdialogs --no-first-run`; otherwise `open <url>` in the
  default browser (the guide explains pressing Ctrl-Cmd-F once in Safari).
- Then `exec /usr/bin/caffeinate -d -i -s` so the display and the Mac stay awake for as long as
  the user is logged in.

### `docs/INSTALL-MAC.md` (generic, public; no personal data)
Sections: what you need; **on the new Mac before installing** (finish macOS setup, wired
network recommended, optional Google Chrome for true kiosk mode); **get the project**
(download the ZIP from the repository page, unzip, move the folder to the home folder and why;
or `git clone`); **run the installer** (double-click `Install.command`; the first time macOS
blocks a downloaded script: right-click → Open → Open; or run `./install.sh` in Terminal);
**bring your settings from another Mac** (`./install.sh export ~/Desktop/kasa-dashboard-settings.json`
on the old Mac, copy the file, `./install.sh --settings <file>` or `./install.sh import <file>`
on the new one; the Kasa password is typed again, never copied); **stop the old copy** (one
computer per set of cameras); **make it a wall display** (System Preferences on Monterey:
Users & Groups → Login Options → Automatic login; Energy Saver → "Start up automatically after
a power failure" and prevent the computer from sleeping; Desktop & Screen Saver → screen saver
Never; Security & Privacy → turn off "Require password after sleep"; note FileVault must be off
for automatic login); **backups on an external drive** (the drive's name must stay the same;
the first write may show a "would like to access files on a removable volume" prompt — click
OK once); **permission prompts you may see**; **update**; **uninstall**; **troubleshooting**
(log location, `./install.sh status`).

## Constraints
- No `sudo`, nothing outside the user's home folder and the project folder. No new dependencies.
- Tests never call the real `launchctl`, never write to the real `~/Library`, never download:
  use the injectable runner, `--home`, and temp directories. Do not install anything on this
  machine while working, and do not run `install.sh` for real.
- A dashboard is running from the main checkout on port 1984: leave it alone.
- Public repository: placeholder data only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes completely.
- [ ] `/bin/bash -n install.sh Install.command scripts/kiosk.sh` reports no syntax errors, and the
      three files are mode 755 (`git diff` shows the mode; set with `chmod +x`).
- [ ] `/usr/bin/python3 -m app.install install --dry-run --home "$(mktemp -d)"` prints the plan
      and changes nothing (verify with `git status --short` before and after).
- [ ] `git diff --stat HEAD -- start.sh scripts/setup.sh www 'tests/*_acceptance.py' tests/test_security.py` is empty.
- [ ] Final message lists files changed and what you could not verify (no Intel Mac, no
      Monterey, no real launchd run).
