# FastFiles

FastFiles is a Linux-first desktop app for sending files and folders over SSH. It
uses the tools administrators already trust: `rsync` for transfers and your normal
OpenSSH configuration for hosts, keys, jump hosts, ports, and agents.

## Current MVP

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

## Run it

You need Python 3.10+, `ssh`, and `rsync` on the local machine. The remote machine
also needs an SSH server and `rsync` installed.

The easiest setup is:

```bash
./install.sh
./start.sh
```

The installer creates an isolated `.venv`, installs or updates FastFiles, checks
the dependencies, and runs the test suite. It is safe to run again after pulling
an update. Arguments passed to `start.sh` are forwarded to FastFiles, so an
always-on locker can also be started with `./start.sh --serve`.

Manual setup is equivalent to:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
fastfiles
```

The GUI shares its locker while it is open. To keep a machine discoverable
without leaving the GUI open, run its background service instead:

```bash
fastfiles --serve
```

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

The service listens on all network interfaces. Permit TCP port `47832` in the
host firewall for the VPN interface; UDP `5353` is only needed for automatic
discovery on networks that carry multicast.

### Headless sharing policy

The headless service reads `~/.config/fastfiles/config.json`. `locker_path` is
always the hard boundary; `allow_patterns` and `deny_patterns` further restrict
paths inside it. Deny rules win. Patterns use shell-style matching and forward
slashes:

```json
{
  "locker_path": "/srv/fastfiles",
  "allow_patterns": ["incoming", "incoming/**", "public", "public/**"],
  "deny_patterns": ["**/.ssh", "**/.ssh/**", "**/*.key", "**/private/**"]
}
```

Keep the generated identity and access-code fields when editing this file, then
restart `fastfiles --serve`. Saved machine metadata lives in a separate
mode-`0600` `connections.json`; remembered peer codes go through the OS keyring.

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

The transfer command builder and progress parser have no GUI dependency:

```bash
python -m unittest discover -s tests -v
```

FastFiles currently assumes rsync is present at both ends. A future transport
adapter can add SFTP fallback for Windows machines that only expose OpenSSH.
