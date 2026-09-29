# FastFiles

FastFiles is a desktop app for Linux and Windows that shares files through a LAN
Locker or sends files and folders over SSH. Direct SSH
uses the tools administrators already trust: `rsync` for transfers and your normal
OpenSSH configuration for hosts, keys, jump hosts, ports, and agents.

**Free for personal, noncommercial use.** Business and organizational use requires
a separate paid license. See [Licensing](#licensing).

## Transfers

- Drag photos, files, and folders onto a locker folder or computer to copy them
- Drag between local and remote lockers, or select several items and Send/Receive
- Send one or more local files or a directory to a remote machine
- Receive a remote file or directory into a local folder
- Use `user@hostname`, a hostname, or an alias from `~/.ssh/config`
- Overall progress, transferred bytes, speed, ETA, status log, and cancellation
- Optional compression, archive metadata, partial-file resume, and dry run
- Safe argument handling: commands are launched without a shell
- A consistent `~/FastFiles Locker` shared folder on every machine
- Automatic peer discovery on the local network with mDNS
- Side-by-side locker browsing with automatic folder refresh while idle
- A Computers sidebar with memorable aliases and favorite folders per computer
- Pairing codes or separate computer keys with download/upload folder permissions
- Saved credentials in the desktop keyring; computer keys are hashed on the server
- Global allow/deny rules for controlling which locker paths are exposed
- Saved IPs/hostnames with automatic Locker availability checks every 15 seconds
- Explicit overwrite controls, cancellation, and empty-folder copies in Locker
- An Inbox shortcut, incoming-file notices, and persistent receive history
- Private, rotating diagnostic logs

Direct SSH uses encrypted OpenSSH connections. Locker uses **unencrypted HTTP**
with a pairing code or computer access key and should only be used on a trusted
LAN or through an encrypted VPN. Do not expose the Locker port directly to the internet.
Sharing rules restrict Locker only; Direct SSH uses the SSH account's filesystem
permissions.

## Run it

You need Python 3.10+. Locker transfers need FastFiles on both machines; they
do not require SSH or rsync. Direct SSH additionally needs local `ssh` and
`rsync`, plus an SSH server and `rsync` on the remote machine.

The easiest setup is:

```bash
./install.sh
./start.sh
```

The installer creates an isolated `.venv`, installs or updates FastFiles, checks
the dependencies, runs the test suite, and adds **FastFiles** to your application
launcher (desktop installs only). It is safe to run again after pulling
an update. Arguments passed to `start.sh` are forwarded to FastFiles, so an
always-on locker can also be started with `./start.sh --serve`.

### Updating from Git

Close FastFiles before updating. On Windows, double-click **`update.bat`**.
On Linux, run **`./update.sh`** (or choose **Run in Terminal** in a file manager
that supports launching shell scripts).

Both updaters pull the current branch's configured upstream with `git pull
--ff-only --no-rebase`, then rerun the installer and its checks. Git must be
installed and the folder must be a Git clone, not a downloaded ZIP. Local changes,
a missing upstream, or diverged history stop the update without running the
installer. No changes are automatically discarded or stashed. If installation
fails after pulling, the downloaded code stays in place; rerun the installer
after fixing the reported error. Restart FastFiles when the update finishes.

Headless installations should use `./update.sh --headless` or
`update.bat -Headless` to keep desktop dependencies optional.

### Windows

Install Python 3.10 or newer, then **double-click `start.bat`** in this folder.
It runs setup automatically if the Python environment is missing, then opens
FastFiles. No commands need to be typed. After setup, you can also open
**FastFiles** from the Start menu without a console window.

To download updates from Git and install them, double-click **`update.bat`**.
To repair the current installation without downloading updates, use **`install.bat`**.
If setup or startup fails, the window stays open so you can read the error.
Keep the whole project folder together; these launchers are not standalone apps.

For users who prefer PowerShell, the equivalent commands are:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
.\start.ps1
```

The installer creates `.venv`, installs the desktop dependencies, runs the tests,
and adds a **FastFiles** Start menu shortcut for your account. Keep the checkout
in place; rerun the installer after moving it or pulling updates. If PowerShell
blocks the start script, use `powershell -ExecutionPolicy Bypass -File .\start.ps1`.
No administrator access is required for the installer.

For a headless Locker:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Headless
.\start.ps1 --init-config
.\start.ps1 --serve
```

Allow Python through Windows Firewall on your **private** network when prompted.
Locker uses TCP port 47832 by default and mDNS discovery uses UDP 5353. You can
connect by IP address if discovery is unavailable. Windows and Linux Locker
instances use the same protocol.

Settings and profiles are stored in `%APPDATA%\FastFiles`, logs in
`%LOCALAPPDATA%\FastFiles`, and the default shared folder is
`%USERPROFILE%\FastFiles Locker`. Windows files inherit the containing folder's
access controls; POSIX permission bits apply only on Linux. Windows sharing
patterns ignore case. Names Windows cannot represent (including device names,
trailing dots/spaces, and alternate data streams) are rejected instead of renamed.
Symbolic links and directory junctions are excluded from sharing.

**Optional Direct SSH:** install Cygwin's `rsync` and `openssh` packages and put
its `bin` directory (usually `C:\cygwin64\bin`) first on PATH before starting
FastFiles. `rsync`, `ssh`, and `cygpath` must come from the same Cygwin installation.
FastFiles uses `cygpath` to translate local drive paths for rsync. Run
`.\start.ps1 --check` to check tool availability. Set up keys and accept the
remote host key using Cygwin SSH first; its home/configuration may differ from
Windows OpenSSH. The alias picker reads `%USERPROFILE%\.ssh\config`, so keep
aliases there consistent with Cygwin's SSH configuration or enter the host
explicitly. Native Windows OpenSSH alone and WSL rsync are not supported Direct
SSH backends. Locker needs neither Cygwin nor WSL.

### Linux desktop libraries

On Linux, the desktop installer detects missing XCB cursor, EGL, and OpenGL
libraries and installs the system packages using apt, dnf, or pacman. It may ask
for your sudo password (and package-manager confirmation). Other distributions
receive a list of missing libraries to install manually. Headless installs skip
these desktop packages.

The desktop installer checks Qt using your current display session as well as an
offscreen test. Without a desktop session it reports that GUI startup could not
be verified. Python packages alone do not supply every Linux display library.

If startup reports that the Qt `xcb` plugin could not load and mentions
`xcb-cursor0`, install the cursor library for your distribution:

```bash
# Ubuntu / Debian
sudo apt install libxcb-cursor0
# Fedora
sudo dnf install xcb-util-cursor
# Arch Linux
sudo pacman -S xcb-util-cursor
```

Then run `./start.sh` again. If it still fails, run
`QT_DEBUG_PLUGINS=1 ./start.sh` to identify other missing libraries or display
connection errors. See [Qt's Linux requirements](https://doc.qt.io/qt-6/linux-requirements.html).
The `--check` command checks only SSH tools; it does not verify GUI startup.

### Launcher and manual setup

To add or refresh the launcher for an existing installation without reinstalling,
run `.venv/bin/python scripts/install_launcher.py`. The entry is installed for
your user in `${XDG_DATA_HOME:-~/.local/share}/applications/fastfiles.desktop`.
Keep this checkout in place; rerun the installer if you move it.

Manual setup is equivalent to:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[desktop]'
fastfiles
```

### Choose a computer, then a folder

Use **Save as…** to give a Locker connection a memorable alias such as “Laptop”
or “Home server.” Saved and discovered computers appear in the **Computers**
sidebar. Select a computer, enter its pairing code or computer key, and click
**Connect**. Check **Remember access** when saving the computer to store its
credential in the desktop keyring. Connecting to an already saved computer also
updates its remembered credential when this box is checked.
Saved computers stay in the sidebar when offline and can be saved before their
first connection.

**My locker** shows this computer's shared files; **Computer locker** shows the
selected computer's permitted folders. Double-click a folder to browse it, use
**Up** or **Home** to navigate, and **New** to create a destination where
you have permission. **Save folder…** gives the remote folder an alias in that
computer's **Saved folders** menu, available while connected. Each saved computer
remembers its last folder.

In shorter windows, **⋯** opens the saved computer settings. Right-click a file
pane for folder actions such as **Home**, **Inbox**, and **New**; the panes still
accept drops when the separate drop areas are hidden.

- Drop photos, files, or folders from your file manager into **My locker** to add
  copies to the local shared folder.
- Drop them into **Computer locker** to send directly to the connected computer.
- Drag items between the two panes to send or receive copies. Ctrl/Shift-select
  several items, or use **Send selected** and **Receive selected**.
- Drop files or local locker items onto a computer in the sidebar to send to its
  remembered folder. If access is not remembered, enter the code/key and connect
  to finish the pending drop.

A highlighted folder is the drop destination. Dropping on empty space or a file
uses that pane's currently displayed folder. The Send/Receive buttons also use
the displayed destination; check the destination label before starting. Originals
stay in place. Transfers show progress, support cancellation, and replace files
only when **Replace existing files** is checked.

Adding photos to your locker makes them available for authorized computers to
browse and download while this computer is online and its FastFiles app or
service is running. Open locker folders refresh about every three seconds while
idle. Files copy to another computer only when you send or receive them; adding
a file does not automatically replicate it across your computers.

**Inbox** opens `Inbox` inside your local locker and creates it when permitted.
Use it as a convenient receiving folder by opening it before receiving a copy.
Incoming files still go to the destination the sender selected. **Transfer
history** shows recent sent, received, and imported files, including arrivals
while the headless service was running; double-click a local receipt to open
its folder. Incoming notices appear while the desktop app is open, and **Open folder** opens the
currently displayed local folder.

In **Direct SSH**, drop files and folders into the drop area to add them to the
send list, then click **Send files**. In Receive mode, drop one local folder to
choose the destination. Drop areas are disabled while their operation is running.

The GUI shares its locker while it is open. Computers can also be selected by
IP or hostname. **Check computers** refreshes availability immediately; periodic
checks run every 15 seconds in the background and never send credentials.

Online means that a compatible FastFiles service answered, not just that the
computer is powered on. Authentication is checked when you click Connect.
Offline saved machines stay selectable for edits and retries. **Update needed**
means the remote FastFiles version is incompatible. Version 0.2 uses Locker
protocol 2; update both computers with `git pull` followed by the installer.

### Receive without the desktop app

To keep a machine discoverable and receiving without a desktop or a GUI
dependency, install the headless package and initialize its configuration:

```bash
./install.sh --headless
./start.sh --init-config
./start.sh --check-config
./start.sh --serve
```

The equivalent pip installation is `pip install -e .`; GUI users must include
the `[desktop]` extra. A custom config, bind address, and port are supported:

```bash
fastfiles --serve --config /path/to/fastfiles.json --bind 100.90.80.70 --port 47832
```

Use `--no-discovery` in headless mode to disable mDNS advertising. There is also
a [systemd user-service example](examples/fastfiles.service). Run it as the user
who owns the locker folder, not as root.

`--serve` prints the receive location and the `Inbox` location when available.
The service creates `Inbox` if the locker is writable and sharing rules permit
it. Keep the service running to receive after closing the desktop app; incoming
receipts remain available the next time you open the desktop app. Sender-selected
destinations and the same folder permissions apply to headless transfers.

To inspect the most recent 100 local receipts without opening the desktop app:

```bash
fastfiles --activity
fastfiles --activity --config /path/to/fastfiles.json
```

Activity is printed newest first with its UTC time, source, destination, file
count, and bytes. Run it separately from `--serve` or permission-management
commands; it reads the existing configuration and history.

On the other computer, select the discovered peer and enter the six-digit code
shown by its GUI or service, or a computer key issued by its owner. Only files
below that machine's approved locker folder are visible; parent paths and the
rest of the filesystem are rejected. In the desktop app, open **Pair this
computer** to reveal this computer's pairing details.

### Permissions for each computer

New installations retain six-digit pairing-code access. To give computers
different permissions, open **Sharing permissions…** on the computer that owns
the files. Create a named computer key and choose its download and upload path
patterns. For example, allow a laptop to download `Photos/**` and upload
`Inbox/**`, or issue an upload-only key for `Inbox/**`. Enter one rule per line;
leave an operation's box empty to deny that operation.

The owner sees each new key once. Enter it in the other computer's code/key
field and use **Remember access** to store it in that computer's system keyring.
The serving computer stores only a hash of the key. Names identify permissions
for people; access follows possession of the issued key.

The first computer grant disables the shared pairing code, so it cannot bypass
the individual folder rules. Revoking the last computer key keeps pairing-code
access disabled. Existing computer grants can be revoked, and the global
allow/deny and read-only settings always apply in addition to each key's rules.
To change a grant in the dialog, select it and use **Replace key and permissions**;
the previous key stops working, so enter the replacement on that computer.

Headless owners can manage the same grants from the command line:

```bash
fastfiles --grant-computer Laptop --download 'Photos/**' --upload 'Inbox/**'
fastfiles --grant-computer Backup --download 'Photos/**' --download 'Documents/**'
fastfiles --list-computers
fastfiles --revoke-computer Laptop
```

Repeat `--download` and `--upload` for additional patterns; omitting either gives
that key no permission for that operation. At least one pattern is required.
To replace a key or its CLI permissions, revoke it and issue a new key. Add
`--config /path/to/fastfiles.json` when using a custom configuration. CLI changes
and manual configuration edits require restarting the service or desktop app.
Changes made in **Sharing permissions…** apply to new requests in the running
desktop's service; restart a separately running headless service to apply them.

### VPN connections

mDNS discovery often does not cross a VPN. You can type a VPN IP address or DNS
name directly into the peer box, optionally with a port:

```text
100.90.80.70
workstation.my-tailnet.ts.net
10.8.0.12:47832
[fd7a:115c:a1e0::12]:47832
```

By default the service listens on all IPv4 interfaces. Set `bind_address` to a
VPN address (or use `--bind`) to restrict it. An explicit IPv6 bind address is
also supported. Permit TCP port `47832` in the
host firewall for the VPN interface; UDP `5353` is only needed for automatic
discovery on networks that carry multicast.

### Headless sharing policy

The headless service reads `~/.config/fastfiles/config.json`. `locker_path` is
always the hard boundary; `allow_patterns` and `deny_patterns` further restrict
paths inside it. Deny rules win, including for direct downloads/uploads and
recursive folder transfers. Edit these fields in the generated config:

```json
{
  "locker_path": "/srv/fastfiles",
  "allow_patterns": ["incoming/**", "public/**"],
  "deny_patterns": ["**/.ssh/**", "**/*.key", "**/private/**"],
  "read_only": false,
  "bind_address": "0.0.0.0",
  "port": 47832,
  "max_upload_bytes": 53687091200
}
```

Keep the generated identity and access-code fields when editing this file. Run
`fastfiles --check-config`, then restart the service or GUI. A malformed config
stops startup and is never replaced with permissive defaults.

Patterns are relative paths using `/`, case-sensitive on Linux and case-insensitive on Windows. `*` matches within one
path segment; `**` matches zero or more segments. `public/**` includes the
`public` folder and descendants. `**/*.key` matches `.key` files at the root and
below it. Denying a directory denies its entire subtree. For file-specific allow
rules, also allow their parent folders so they can be browsed. `[]` for
`allow_patterns` shares nothing; an omitted allow list defaults to `["**"]`.
Root listing remains available even when it is empty. Symbolic links, special
files, and `.fastfiles-*` temporary files are never shared.

`read_only` blocks remote uploads and directory creation. The default upload
limit is 50 GiB per file and can be adjusted with `max_upload_bytes`. Incorrect
codes are rate-limited to ten attempts per minute per source IP. The pairing
code is stored in the private local config so it can be displayed; it is not an
encrypted transport or an internet-facing authentication system.

Saved machine aliases, favorite folders, and last-used folders live in a separate
mode-`0600` `connections.json`; remembered codes and computer keys go through the
OS keyring. If a keyring is unavailable, addresses and folders can still be saved
and the credential entered when connecting. Saving with **Remember access**
unchecked removes the saved credential for that endpoint. Profiles with the
same IP and port share a keyring entry. The GUI reports malformed profile JSON
instead of overwriting it.

### Copy behavior and logs

Locker copies only items visible under the source sharing policy. Existing files
are protected unless **Replace existing files** is enabled. Empty folders are
preserved. Each file is written to a private temporary file before completion;
uploads confirm byte count and SHA-256 with the receiver, and downloads reject
short responses. Cancel/failure keeps completed files but removes the unfinished
temporary download; folder copies are not an all-or-nothing transaction. Network
cancellation waits for the current request or its timeout. Locker does not yet
resume partial files or preserve source metadata; Direct SSH offers both.

The selected connection and destination are held fixed during a transfer. The
completion status includes the destination, file count, and size. Direct SSH
shows the selected SSH account/path and itemized rsync output under **Show
details**. `~/` refers to the SSH user's home, which may differ from the desktop
user's home. Send mode treats the remote path as a folder. Receive mode accepts
a remote file or folder. **Dry run** only previews and copies nothing.

Each locker keeps its most recent 200 file/folder transfer receipts in
`.fastfiles-activity.json` inside the locker. This private file is excluded from
sharing; the desktop shows the most recent 100 receipts in **Transfer history**.
Receipts include time, source, destination, file count, and bytes, and survive
service restarts. The desktop records sends, receives, and imports; the headless
service records incoming uploads and folders. A folder transfer can create
several receipts as its contents arrive.

Diagnostics are stored in `~/.local/state/fastfiles/fastfiles.log` (or under
`$XDG_STATE_HOME`), with mode `0600`, rotation at 2 MB, and three backups. The
**Open log** button opens the current log. Logs include endpoints, paths,
rsync output, and transfer results, but never pairing codes or saved secrets.
Paths and filenames can still be sensitive; review logs before sharing them.

For a quick environment check:

```bash
python -m fastfiles --check
```

Authentication is deliberately left to OpenSSH. Set up an SSH key and test the
connection in a terminal first, for example `ssh my-server`. Password prompts are
not embedded in the app; an `ssh-agent` or desktop keyring is recommended.

## SSH aliases

This standard configuration works directly in FastFiles:

```sshconfig
Host studio
    HostName 192.168.1.42
    User alex
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

Choose `studio` in the Host box, enter a remote path, and transfer.

## Development

Install development checks with `pip install -e '.[desktop,dev]'`, then run:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
ruff check src tests
```

The suite runs real Locker HTTP round trips and real rsync processes using an
isolated local transport, so it does not contact your saved machines. Qt checks
cover layout, connection state, online status, cancellation, and error recovery.
They are skipped in installations without desktop dependencies; rsync integration
tests use a POSIX harness and are skipped on Windows or if rsync is unavailable.
GitHub Actions runs the desktop suite on Linux and Windows with Python 3.10 and
3.14, plus a separate Linux headless installation check.

Windows CI coverage is configured; native Windows installation and Cygwin SSH
transfers still need a manual smoke test. macOS packaging and transfers over
real lossy networks also need platform testing. The locker directory must be
owned by a trusted local account; it is not a sandbox against another local
process changing the filesystem while a request is running.

## Licensing

FastFiles is **source available and free for personal, noncommercial use** under
the [FastFiles Personal Use License 1.0](LICENSE). You may inspect, modify, and
share it without charge under those terms.

Business, commercial, professional, and other organizational use requires a
**separate paid license**, including internal use that does not generate revenue.
Contact [magus-warrior, the repository owner](https://github.com/magus-warrior)
through [the repository](https://github.com/magus-warrior/fastfiles) to discuss
licensing before using FastFiles for those purposes.

This is not an OSI-approved open-source license: the personal-use restriction
excludes commercial use. See [licensing details](docs/licensing.md) for examples
and third-party dependency information.

Direct SSH currently assumes rsync is present at both ends. A future transport
adapter can add SFTP fallback for Windows machines that only expose OpenSSH.
