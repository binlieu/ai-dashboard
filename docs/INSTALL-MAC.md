# Install on a dedicated Mac

## What you need

This guide covers macOS 12 Monterey on an Intel Mac mini (Late 2014). Newer
macOS versions should work, though the settings screens have different names.
You’ll need your Kasa account email and password, a display, a keyboard and
mouse for setup, and a Mac on the same local network as your Kasa devices.
Internet access is needed to download the project, Apple’s Command Line Tools,
and the video bridge. The installer uses go2rtc v1.9.14 on macOS 12 and v1.9.15
on macOS 13 and newer. Both releases are pinned by SHA-256 checksum.

## On the new Mac before installing

Finish macOS setup and sign in to the account you’ll use for the wall display.
Connect the Mac to your network. A wired Ethernet connection is recommended.

Google Chrome is optional. The installer uses kiosk mode if a compatible copy
of Chrome is already in `/Applications`. Chrome 150 was the last release for
Monterey. On a fresh Monterey Mac, use the built-in Safari if you don’t have a
compatible copy of Chrome. [Chrome support history](https://support.google.com/chrome/a/answer/10314655?hl=en)

## Get the project

Open **Terminal** (in **Applications → Utilities**) and run this command.

```sh
xcode-select --install
```

Follow Apple’s dialog and wait for it to finish. If Terminal says the tools are
already installed, go ahead and clone the project.

```sh
git clone https://github.com/binlieu/ai-dashboard.git ~/kasa-dashboard
```

Cloning avoids macOS’s downloaded-script prompt. You can later update this copy
with `git pull` and `./install.sh`.

Alternatively, use **Code → Download ZIP** on the repository page and unzip it.
Move the unzipped folder directly into your home folder (Finder’s **Go → Home**)
and rename it `kasa-dashboard`. macOS restricts background access to Desktop,
Documents, Downloads and iCloud Drive. An external drive may also be unavailable
when you log in, so keep the project on the Mac itself.

## Run the installer

If you’re moving an existing dashboard, export its settings and stop the old
copy before starting the installer on the new Mac. See the next two sections.

In Finder, open `~/kasa-dashboard` and double-click `Install.command`. If Monterey
blocks the downloaded script, right-click `Install.command`, choose **Open**, and
confirm **Open** in the dialog. You can also run it from Terminal.

```sh
cd ~/kasa-dashboard
./install.sh
```

If Apple’s Command Line Tools dialog appears, finish installing the tools and
run the dashboard installer again. Enter your Kasa email, then type your
password twice. Terminal shows no characters while you type the password.
The installer saves the login in a private `.env` file and keeps it on future
runs.

The installer starts the server and sets it up to start at each login. The
dashboard is at `http://127.0.0.1:1984/` by default. With a compatible Chrome
installed, the display opens in kiosk mode. Otherwise the installer opens the
default browser. In Safari, press **Control–Command–F** once the page opens to go
full-screen. If Safari opens a regular window at a later login, use the shortcut
again. [Apple’s Mac keyboard shortcuts](https://support.apple.com/en-ie/102650)

## Bring your settings from another Mac

1. On the old Mac, open Terminal in the dashboard folder and export its settings.

   ```sh
   ./install.sh export ~/Desktop/kasa-dashboard-settings.json
   ```

2. Copy that JSON file to the new Mac. It carries your saved cameras, plugs and
   switches, layout, weather, and backup settings. Type your Kasa password again
   during installation. The export contains only dashboard settings.
3. Stop the old copy as described below. On the new Mac, run one of these from
   `~/kasa-dashboard`, substituting the location where you copied the file.

   ```sh
   ./install.sh --settings ~/Desktop/kasa-dashboard-settings.json
   ```

   If you already installed the dashboard, import the file directly.

   ```sh
   ./install.sh import ~/Desktop/kasa-dashboard-settings.json
   ```

   An import saves the previous settings as `data/cameras.json.before-import-*`
   and restarts the server if it’s running as a login agent. For a server started
   with `./start.sh`, press **Control–C** in its Terminal window before importing,
   then start it again afterward.

## Stop the old copy

**Only one computer may run the dashboard for the same cameras.** On the old
Mac, run `./install.sh uninstall` from its dashboard folder to stop and remove
the login agents. If you started the dashboard with `./start.sh`, press
**Control–C** in that Terminal window. Closing the browser leaves the server
running.

## Make it a wall display

On the dedicated Mac, open **Apple menu → System Preferences** and make these
changes for unattended startup.

1. In **Users & Groups → Login Options**, unlock the pane if needed and set
   **Automatic login** to the display account. FileVault must be off for
   automatic login. With automatic login enabled, someone with physical access
   can restart the Mac and use that account. [Apple’s automatic login guide](https://support.apple.com/en-gb/102316)
2. In **Energy Saver**, enable **Start up automatically after a power failure**
   and prevent the computer from sleeping. Set **Turn display off after** to
   **Never** for an always-on display.
3. In **Desktop & Screen Saver → Screen Saver**, set the screen saver start time
   to **Never**.
4. In **Security & Privacy → General**, turn off **Require password** after
   sleep or screen saver begins.

Restart the Mac with the display connected and confirm it signs in and opens
the dashboard.

## Backups on an external drive

Connect the drive, create a recordings folder on it, and select it under
**Setup → Backup** in the dashboard. The folder path uses the drive’s name
(for example `/Volumes/Recordings/Kasa`). Keep the drive’s name the same so
macOS mounts it at the saved path.

When importing settings, backup stays enabled only if the saved folder exists
on the new Mac. Connect the drive before importing or choose the folder and
recording cameras again in **Setup → Backup**. The transfer copies settings.
Your recordings stay on the drive.

## Permission prompts you may see

- If macOS asks to install Apple’s Command Line Tools, complete the installation
  and run `Install.command` again.
- If Terminal asks for access to Desktop while exporting or importing a file
  there, allow access to complete the transfer.
- The first recording write may trigger a prompt saying the dashboard or Python
  “would like to access files on a removable volume.” Click **OK** once.

## Update

Update the cloned folder from Terminal.

```sh
cd ~/kasa-dashboard
git pull
./install.sh
```

The installer reloads the agents and keeps your login and saved settings. If
you used the ZIP, download and unzip the new version and copy the updated
project files into your existing dashboard folder before rerunning the
installer. Keep `.env` and `data/` in that folder.

## Uninstall

```sh
cd ~/kasa-dashboard
./install.sh uninstall
```

This stops and removes the login agents. Your project folder, login and saved
settings remain available if you install again. Close the dashboard browser
window when you’re done.

## Troubleshooting

- **Is it running?** From the dashboard folder, run `./install.sh status` to
  see whether the agents are loaded, whether the server responds, and the
  number of saved cameras and devices. Check `run/dashboard.log` for server
  startup errors when using the login agent.
- **Password changed?** Run `./install.sh login` to enter the new login and
  restart the server agent if it’s loaded.
- **Browser didn’t open?** Check the server with `./install.sh status`, then
  reopen the printed dashboard address. If Chrome opens a normal window while
  it’s already running, quit Chrome and rerun `./install.sh` to relaunch the
  kiosk. To run the server without opening a browser, use `./install.sh --no-kiosk`.
- **Want to start the server yourself?** Use `./install.sh --no-autostart` and
  then `./start.sh`. If you already enabled login agents, use
  `./install.sh uninstall` to remove them first.
- **Port already in use?** Stop the app using that port, or use
  `./install.sh --port 1985` on a new installation. For an existing installation,
  edit `DASHBOARD_PORT` in `.env` and rerun `./install.sh`.
