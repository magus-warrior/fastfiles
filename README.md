# FastFiles

FastFiles is a Linux-first desktop app for sending files and folders over SSH. It
uses the tools administrators already trust: `rsync` for transfers and your normal
OpenSSH configuration for hosts, keys, jump hosts, ports, and agents.

**Free for personal, noncommercial use.** Business and organizational use requires
a separate paid license. See [Licensing](#licensing).

## Transfers

- Send one or more local files or a directory to a remote machine
- Receive a remote file or directory into a local folder
- Use `user@hostname`, a hostname, or an alias from `~/.ssh/config`
- Overall progress, transferred bytes, speed, ETA, status log, and cancellation
- Optional compression, archive metadata, partial-file resume, and dry run
- Safe argument handling: commands are launched without a shell
- A consistent `~/FastFiles Locker` shared folder on every machine
- Automatic peer discovery on the local network with mDNS
- Side-by-side local and peer locker browsing, including file sizes
- Pairing-code protection and folder-safe uploads/downloads
- Named machine profiles with pairing codes stored in the desktop keyring
- Headless allow/deny rules for controlling which locker paths are exposed
- Saved IPs/hostnames with automatic Locker availability checks every 15 seconds
- Explicit overwrite controls, cancellation, and empty-folder copies in Locker
- Persistent transfer receipts and private, rotating diagnostic logs

Direct SSH uses encrypted OpenSSH connections. Locker uses **unencrypted HTTP**
with a six-digit pairing code and should only be used on a trusted LAN or through
an encrypted VPN. Do not expose the Locker port directly to the internet.
Sharing rules restrict Locker only; Direct SSH uses the SSH account's filesystem
permissions.

## Run it

You need Python 3.10+, `ssh`, and `rsync` on the local machine. The remote machine
also needs an SSH server and `rsync` installed.

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

The GUI shares its locker while it is open. Each saved Locker machine can be
selected by alias, IP, or hostname. Use **Save as…** to save the address; check
**Remember code in keyring** to save its code too. A machine can be saved before
it is online. **Check now** refreshes its status immediately; periodic checks
run in the background and never send the pairing code.

Online means that a compatible FastFiles service answered, not just that the
computer is powered on. Authentication is checked when you click Connect.
Offline saved machines stay selectable for edits and retries. **Update needed**
means the remote FastFiles version is incompatible. Version 0.2 uses Locker
protocol 2; update both computers with `git pull` followed by the installer.

To keep a machine discoverable without a desktop or a GUI dependency, install
the headless package and initialize its configuration:

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

On the other computer, select the discovered peer and enter the six-digit code
shown by its GUI or service. Only files below that machine's approved locker
folder are visible; parent paths and the rest of the filesystem are rejected.

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

Patterns are case-sensitive relative paths using `/`. `*` matches within one
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

Saved machine metadata lives in a separate
mode-`0600` `connections.json`; remembered peer codes go through the OS keyring.
If a keyring is unavailable, addresses can still be saved and the code entered
when connecting. An unchecked Remember code removes the saved code for that
endpoint. Profiles with the same IP and port share a keyring entry. The GUI
reports malformed profile JSON instead of overwriting it.

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
tests are skipped if rsync is unavailable. GitHub Actions runs the desktop suite
on Python 3.10 and 3.14 plus a separate headless installation check.

Linux is the tested target. Windows/macOS desktop packaging and transfers over
real lossy networks still need platform testing. The locker directory must be
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

FastFiles currently assumes rsync is present at both ends. A future transport
adapter can add SFTP fallback for Windows machines that only expose OpenSSH.
