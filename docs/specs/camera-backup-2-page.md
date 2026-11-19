# Codex task spec — Camera backup 2/2: Backup section in Setup

Effort: high

## Goal
Setup gets a "Backup" section where the user types the storage folder and picks, per camera,
Off / 1 day / 7 days / 30 days, and can see that recording is working.

## Scope — touch ONLY these
- `www/index.html`, `www/dashboard.css`, `www/dashboard.js`
- `README.md` ("Using the dashboard": how to set up backup)

No Python, no tests, nothing under `www/vendor/`.

## What to do
1. **Section "Backup"** in Setup (after "Plugs and switches"):
   - `<label for="backup-folder">Storage folder</label>` text input (maxlength 1024,
     placeholder `/Volumes/My Drive/Camera backups`), help text: "Type the full path of an
     existing folder, for example on an external drive. Video is saved as 3-minute MP4 files;
     older files are removed automatically. Off stops recording and leaves existing files."
   - One row per camera in configured order: camera name and a `<select>` with `Off`, `Keep 1
     day`, `Keep 7 days`, `Keep 30 days` (values 0/1/7/30), plus a state word from the server
     (`Recording`, `Waiting for video`, `No folder`, `Drive almost full`, `Off`) and, when
     known, "Last file HH:MM" in the browser's locale.
   - A size hint under the rows that updates as selects change: sum of `days × 12 GB` for the
     selected cameras, shown as "Needs roughly N GB" (round to 2 significant figures; TB above
     1000 GB), next to "Free: N GB of N GB" and "Used by backups: N GB" from the server when
     available. If the estimate exceeds the free space, add the sentence "This is more than the
     free space; older files will be removed early."
   - Button "Save backup settings"; busy state; server error `message` shown inline
     (`role="alert"`); success announced in `#setup-announcement` ("Backup settings saved.").
   - **Shortening deletes footage.** If Save would shorten a camera's period (30→7, 30→1, 7→1),
     show an inline confirmation first, naming each such camera and its new period, e.g.
     "Yard will keep only 1 day. Older recordings are deleted now." with buttons "Save and
     delete older recordings" and "Cancel". Only the confirm button sends the request.
     Switching to Off or lengthening a period needs no confirmation.
   - Selecting any period with an empty folder shows the inline error "Enter a storage folder
     first." without calling the server.
2. **Data**: `GET api/backup` when Setup opens and every 15 s while Setup is open (from the
   existing one-second tick; no new timers), and after camera add/remove/rename. Never
   overwrite a field the user is editing: do not refresh the folder input or selects from polls
   while the section has unsaved changes. `PUT api/backup` body
   `{"folder": "...", "cameras": {"cam1": 7, "cam4": 0}}` (send every camera).
   A server without `/api/backup` (404) hides the section.
3. **Status bar**: one small text item next to the health counts, only when at least one
   camera has backup on: "Backup on" normally; "Backup: drive missing" or "Backup: drive almost
   full" in the warning colour when the server reports `warning`. Poll `api/backup` every 60 s
   for this (same single tick). Not colour-only (the words carry the meaning).
4. Style in all four themes with existing custom properties; rows must fit a 700 px wide Setup
   sheet and portrait screens; `textContent` only; the folder path is shown only as the input's
   value.

## Constraints
- Everything in `docs/specs/ui-redesign.md` section 7 still applies (strict CSP, single
  `setInterval`, `dailyReload`, rotation, weather and device logic unchanged).
- API shape (task 1): `GET/PUT /api/backup` → `{"folder", "cameras":[{"id","name","days","state","last"}],
  "free_bytes","total_bytes","used_bytes","warning"}`; errors `{error, message}` with status 400.
- A dashboard is running on port 1984: do not stop it or send it write requests. Public
  repository: placeholder data only. Do not commit.

## Done when
- [ ] `/usr/bin/python3 -m unittest discover -s tests -t .` passes (no Python changed).
- [ ] `git diff --stat HEAD -- app tests www/vendor` is empty.
- [ ] `grep -n "innerHTML\|insertAdjacentHTML\|document.write\|eval(" www/index.html www/dashboard.js` finds nothing; `grep -c setInterval www/dashboard.js` is 1.
- [ ] `node --check www/dashboard.js` passes if `node` is available.
- [ ] Final message lists files changed and what you could not verify.
