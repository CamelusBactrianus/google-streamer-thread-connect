# Join a Google TV Streamer to your Home Assistant Thread network

`thread_join.py` moves a Thread 1.4 border router, such as a **Google TV Streamer (4K)**, off the Thread network it created for itself and onto an existing Thread network, such as the one run by Home Assistant's OpenThread Border Router (OTBR). It uses the Streamer's own **Share Thread network credentials** feature. You don't need to reset anything, change the Home Assistant network, or re-pair your existing Thread devices.

> **Unofficial and unsupported.** Google, Nabu Casa, and the OpenThread project don't provide or support this. It worked for the author, but it could stop working with any firmware update. Read the [warnings](#warnings) before using it.

## The problem this solves

When you set up a Google TV Streamer, it should join an existing Thread network whose credentials are stored on your phone and in your Google account. In practice, it often creates its own separate network (named something like `Google-XXXX`), even after you've done everything commonly recommended: syncing Thread credentials from the Home Assistant Companion app, checking the Google cloud backup, factory resetting, putting it on the same VLAN as HA, using a different Google Home home, clearing Play Services data, and so on. The OTBR logs show the Streamer never even tries to join the HA network. It decides to create a new one before doing anything on the radio.

A 2026 firmware update added Thread 1.4 **credential sharing** to the Streamer: Settings → Network & Internet → **Share Thread network credentials**. This shows a single-use code (an *ePSKc*) that gives temporary admin access to the Streamer's own Thread network. This script uses that access to send the Streamer a *pending dataset* containing **your** network's settings. After a short delay, the Streamer switches to them, which in effect joins it to your network.

## How it works

1. You paste your destination network's active dataset (a TLV hex string). It's kept in memory only.
2. You open the share screen on the Streamer and type the code it shows.
3. The script finds the Streamer using mDNS (`_meshcop-e._udp`) and connects to it with OpenThread's `commissioner-cli`, using the code as the key for the secure connection.
4. It reads the Streamer's current dataset. If the Streamer is already on your network, it stops there.
5. It builds a pending dataset with your network's channel, PAN ID, extended PAN ID, network name, network key, PSKc, mesh-local prefix, security policy, and channel mask, plus a newer active timestamp.
6. **Field check (1.2):** it compares the field *names* (never the values) of your dataset, the Streamer's dataset, and the pending dataset. It continues only if the pending dataset contains exactly your network's fields, so nothing from the Streamer's own dataset can be carried over and spread to your network.
7. It sends the pending dataset to the Streamer.
8. After the delay (the Streamer enforces a **5-minute minimum**), the Streamer switches over. The script then checks the Streamer's mDNS advertisement to confirm it's now on your network.

### Two-step mode: `--dry-run`, then `--from-template` (1.2, recommended)

Some Streamers end the ePSKc session as soon as the active dataset has been read, so the pending dataset can never be sent (see "Not connected to the border agent" under [Troubleshooting](#troubleshooting)). Version 1.2 avoids the read in the real run:

1. `--dry-run` reads the Streamer's dataset, runs the field check, and saves the dataset's **format** to `streamer_template.json` (file mode 600). The network key and PSKc are replaced with placeholders. The file still contains the Streamer's network name, extended PAN ID, and mesh-local prefix.
2. `--from-template` doesn't read the dataset. Before connecting, it checks the Streamer's `_meshcop._udp` mDNS advertisement: the extended PAN ID and active timestamp must match the template, and the Streamer must not already be on your network. Otherwise it stops without connecting. Then it builds the pending dataset from the template, runs the field check, connects, and sends the pending dataset as the **first** command.

## Tested with

- Home Assistant with a Home Assistant Connect ZBT-2 running the OpenThread Border Router add-on
- Two Google TV Streamers (4K) with firmware that includes Thread 1.4 credential sharing
- Windows 11 with WSL2 (Ubuntu 24.04) in mirrored networking mode
- `ot-commissioner` built from source (September 2026)

Version 1.2 (two-step mode) was also tested with:

- A Thread network **formed by a tado° Bridge X** (tado X thermostats; the tado border routers advertise Thread 1.3.0), with a Home Assistant SkyConnect / ZBT-1 (OpenThread RCP 2.7.2.0, OTBR add-on) joined to it
- One Google TV Streamer (4K), build **UTTK.260317.003** (Android 14)
- Arch-based Linux, natively (not WSL), with Python 3.15 in a conda environment
- `ot-commissioner` built from source at `main` commit `c55b8fa` (November 2026) with the conda-forge toolchain

On that setup the original one-step run failed: the Streamer closed the session right after the dataset was read. The two-step run worked on the first try. The whole destination network (both tado border routers and the HA OTBR) adopted the new active timestamp within seconds, and the network kept working.

Other Thread 1.4 border routers that support ePSKc sharing may work, but haven't been tested.

## Requirements

**Operating system**

- **Linux**, including Ubuntu 22.04 or 24.04 and Debian-based distributions. This is the primary target.
- **Windows 10/11** via **WSL2**, which needs *mirrored networking* (Windows 11 22H2 or later). WSL's default NAT mode blocks the mDNS and local traffic this needs.
- **macOS** may work if you can build `ot-commissioner` there, but it's untested.

**Network**

- The computer running the script must be on the **same network segment (VLAN/subnet)** as the device, because discovery uses mDNS.
- The device's border agent must be reachable over UDP. IPv4 is preferred.

**Software**

- Python 3.8 or later
- Python packages: `pexpect`, `zeroconf`
- [OpenThread `ot-commissioner`](https://github.com/openthread/ot-commissioner), built and installed so that `commissioner-cli` is on your `PATH`

**Other**

- The active dataset TLV of the network you want to join (see [Get your network's dataset](#get-your-networks-dataset))
- Physical access to the device's settings, to show the sharing code

## Installation

### 1. (Windows only) Set up WSL2 in mirrored mode

In PowerShell, create `%UserProfile%\.wslconfig`:

```powershell
Set-Content -Path "$env:USERPROFILE\.wslconfig" -Value "[wsl2]`nnetworkingMode=mirrored"
wsl --shutdown
```

Reopen WSL and run `ip addr`. You should see your PC's real LAN addresses, not `172.x`.

If device discovery finds nothing later, allow inbound traffic to WSL. Run this in an **admin** PowerShell:

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

In WSL, work in your Linux home directory (`cd ~`), not under `/mnt/c`.

### 2. Build and install ot-commissioner

```bash
cd ~
sudo apt update && sudo apt install -y git python3-pip
git clone https://github.com/openthread/ot-commissioner.git
cd ot-commissioner
./script/bootstrap.sh
mkdir build && cd build
cmake -GNinja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local ..
ninja && sudo ninja install
```

Check it installed with `which commissioner-cli`.

**Alternative: conda, no `sudo`.** This worked on an Arch-based system with a recent toolchain:

```bash
conda create -n thread -c conda-forge python pexpect zeroconf cmake ninja c-compiler cxx-compiler readline pkg-config
conda activate thread
git clone --recursive https://github.com/openthread/ot-commissioner.git
cd ot-commissioner
cmake -S . -B build -GNinja -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=$HOME/thread/local \
      -DOT_COMM_CCM=OFF -DOT_COMM_TEST=OFF \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
      -DCMAKE_PREFIX_PATH=$CONDA_PREFIX
cmake --build build
cmake --install build
export PATH=$HOME/thread/local/bin:$PATH
```

- `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` is needed with CMake 4.x, which refuses a bundled third-party library (nlohmann/json) that declares support for CMake older than 3.5.
- `cmake --install` ends with a permission error when it tries to copy `commissionerd.service` to `/etc/systemd/system`. That's harmless: `commissioner-cli` is already installed by then, and the service isn't needed.
- With conda, the Python packages are already installed, so you can skip step 3.

### 3. Install the Python packages

```bash
pip install pexpect zeroconf
```

On Ubuntu 23.04 and later, add `--break-system-packages`, or use a virtual environment.

The packages must be installed for the same Python that runs the script. If a virtual environment is active when you run `pip` (for example, the ESP-IDF environment under `~/.espressif/`), the packages go into that environment, and you'll need to activate it again before running the script. To install them for the system Python instead:

```bash
deactivate 2>/dev/null
/usr/bin/python3 -m pip install --user pexpect zeroconf --break-system-packages
```

### 4. Get the script

```bash
chmod +x thread_join.py
```

## Get your network's dataset

You need the **active operational dataset TLV** of the network you want the device to join. This is a long hex string.

- **Home Assistant UI:** Settings → Devices & services → Thread → open your preferred network's info and copy the active dataset TLV.
- **Command line** (Advanced SSH add-on with protection mode off):
  ```bash
  docker exec addon_core_openthread_border_router ot-ctl dataset active -x
  ```

**This string contains your network key.** Treat it like a password: don't paste it into chats, forums, or bug reports. Copy a fresh one each time you run the script, because the network's active timestamp can change.

## Usage

Recommended (1.2), with a **new code from the device for each run**:

```bash
./thread_join.py --dry-run          # checks everything, saves streamer_template.json, changes nothing
./thread_join.py --from-template    # the real run
```

The original one-step run still works where the device allows it:

```bash
./thread_join.py
```

Run it as your normal user, **not with `sudo`**. It doesn't need root, and `sudo` switches to root's Python, which usually doesn't have the required packages. You'll then get a "Missing Python packages" error even though `pip` said they were installed. If you installed the packages in a virtual environment, activate it first.

1. Paste the TLV when asked. It won't be shown as you paste.
2. On the device, open **Settings → Network & Internet → Share Thread network credentials**. Confirm your identity if asked, then press Enter in the script.
3. Type the code the device shows. Spaces and dashes are ignored.
4. If more than one device is advertising, pick the right one. Its current network name is shown.
5. Leave the script running. It confirms the switch once the delay (about 5 minutes) has passed.

Example:

```
Destination network: ha-thread-abcd (channel 15, PAN 0xABCD)

On the device, open "Share Thread network credentials", then press Enter...
Code shown on the device: 123456789
Searching for devices sharing Thread credentials...
Using Google Inc. Google TV Streamer #1A2B._meshcop-e._udp.local. at 192.168.1.50:49157
Connected as commissioner.
Device is on 'Google-XXXX' (channel 13).
Moving it to 'ha-thread-abcd'...
The device closed the session after accepting the dataset (normal for Google TV Streamer); confirming via mDNS instead.

Pending dataset sent. The device should switch in about 300 s.
Waiting to confirm (Ctrl+C to skip)...
Confirmed: the device is now advertising 'ha-thread-abcd'.
```

Example of the two-step mode (1.2). The first part is `--dry-run`:

```
Device is on 'Google-XXXX' (channel 13).
Destination TLV types: 0, 1, 2, 3, 4, 5, 7, 12, 14, 53
Device's active dataset fields: ActiveTimestamp, Channel, ChannelMask, ExtendedPanId, MeshLocalPrefix, NetworkMasterKey, NetworkName, PSKc, PanId, SecurityPolicy
Pending dataset fields:         ActiveTimestamp, Channel, ChannelMask, DelayTimer, ExtendedPanId, MeshLocalPrefix, NetworkMasterKey, NetworkName, PSKc, PanId, PendingTimestamp, SecurityPolicy
Active timestamps (seconds): destination=2, device=1770000000, pending=1770000001
Field check: OK, pending dataset matches the destination
Saved dataset format template (key and PSKc zeroed) to streamer_template.json
Dry run: no changes made.
```

Then `--from-template`, with a new code:

```
Device advertises xp=0123456789abcdef, active timestamp=1770000000; template has xp=0123456789abcdef, active timestamp=1770000000
...
Field check: OK, pending dataset matches the destination
Connected as commissioner.
Moving it to 'ha-thread-abcd'...
  OK  network name
  OK  extended PAN ID
  OK  PAN ID
  OK  channel
  OK  network key
  OK  security flags

Pending dataset accepted. The device should switch in about 299 s.
Waiting to confirm (Ctrl+C to skip)...
Confirmed: the device is now advertising 'ha-thread-abcd'.
```

### Options

| Option | What it does |
|---|---|
| `--dry-run` | Connect, show which network the device is on, run the field check, and save the device's dataset format to the template file. Changes nothing on the device. |
| `--from-template` | Don't read the device's dataset. Use the template from `--dry-run`, check the device via mDNS first, and send the pending dataset as the first command after connecting. Can't be combined with `--dry-run`. |
| `--template PATH` | Where `--dry-run` saves the template and `--from-template` reads it. Default: `streamer_template.json` in the current directory. |
| `--no-confirm` | Exit right after sending the dataset, without waiting to confirm. |
| `--version` | Print the script version. |
| `--debug` | Verbose commissioner and DTLS logging, shown if something fails. **The debug log can contain decrypted network data. Don't share it publicly.** |

### Afterwards

- In Home Assistant, go to Settings → Thread. The device should now appear as a border router on your network. The old `Google-XXXX` network may stay listed for a while with no border routers.
- Restart the device and check that it comes back on your network.
- Your HA border router may adopt the new active timestamp from the device. That's expected: the dataset contents are identical. In practice, the **whole destination network** adopts it, because a device that attaches with a newer dataset registers it with the leader. That's why the field check matters: whatever the pending dataset contains ends up on every device of your network.
- Some border routers encode the `at` (active timestamp) mDNS TXT record differently. The tado Bridge X routers advertise the seconds as a plain 64-bit number, while OpenThread/OTBR shifts them left by 16 bits (ticks in the low bits). The same timestamp can therefore look different in an mDNS browser.
- Delete `streamer_template.json` when you're done. It contains no keys, but it does identify the device's old network.

## Warnings

- **Unofficial.** This relies on current Google firmware behavior. A future update could block it or change how it behaves.
- **The sharing code is effectively an admin password** for the device's Thread network while it's valid. Only use it on a computer and network you trust.
- **The dataset TLV contains your network key.** The script keeps it in memory and in a private temporary folder that's deleted when it exits. Don't save it anywhere shared, and don't post it.
- **The whole Google Thread network moves, not just the Streamer.** A pending dataset applies to every device on the device's current network. If other Google border routers (Nest Hubs, Nest Wifi Pro) or Thread devices are on that `Google-XXXX` network, they'll be moved to your network too. The author only tested this with Streamers that had no other devices on their network. Thread has no way for a commissioner to move just one device: the dataset belongs to the whole network. An **untested** workaround is to isolate the Streamer first. Before generating the code, unplug the network's other border routers and powered devices, wait a few minutes so the Streamer takes over as leader of its own partition, run the script, and plug the other devices back in only after the switch is confirmed. They should then form the old Google network again without the Streamer. Battery-powered devices attached through the Streamer may drop off until they reattach, and Google Home may not handle this cleanly.
- **Never point this at a device that's already on your network.** The script checks this (by comparing extended PAN IDs) and stops. If you edit the script, keep that check. Without it, you could send a pending dataset to your entire HA network.
- **Factory resets and re-setup will undo it.** Setting the device up again in Google Home will most likely create a new Google network. A Google account sync or firmware update might as well. Run the script again if that happens.
- **Codes are single use and expire.** After 10 failed connection attempts with the same code, the device stops accepting it. Generate a new code for each attempt.
- **Timing:** the Streamer increases the switch delay to 5 minutes and usually closes the secure session straight after accepting the dataset. That's normal. The script then confirms using mDNS instead. Some Streamers close the session as soon as the dataset has been *read*, before anything can be sent. Use `--dry-run` and then `--from-template` in that case.
- **Expect the destination network's active timestamp to change** to the Streamer's timestamp plus one, usually a Unix time. A Thread network always follows the newest timestamp, so **restoring a Home Assistant backup does not roll this back**. To correct a wrong dataset later, push a pending dataset with an even newer timestamp through your own border router, for example with `ot-ctl` in the OTBR add-on.
- **No warranty.** You use this at your own risk. If something goes wrong, factory resetting the device restores its default behavior. Your HA network isn't changed except, possibly, by adopting the identical dataset with a newer timestamp.

## Troubleshooting

**"Missing Python packages" even though pip installed them**
You're running the script with a different Python than the one `pip` installed into, usually because of `sudo` or an inactive virtual environment. Run it without `sudo`, and activate the environment you installed into (see [Installation step 3](#3-install-the-python-packages)).

**"No device is advertising an ePSKc service"**
The share screen was closed or timed out, the computer is on a different VLAN/subnet from the device, or (in WSL) mirrored networking or the Hyper-V firewall isn't set up. Keep the code visible on the device until the script connects.

**"Could not connect (wrong or expired code?)"**
Generate a new code and try again. Each code works for one session only.

**"Commissioner did not become active"**
The device refused to grant full commissioner rights. Try again with a new code and `--debug`.

**"Not connected to the border agent" right after reading the dataset**
The output shows the device's network and the field check, then:

```
> opdataset set pending --import /tmp/.../pending.json
INVALID_STATE: sending UDP_TX.ntf message failed, not connected to the border agent
WARNING: the session closed before the device answered. ...
```

The device ended the ePSKc session right after it handed out its active dataset. The likeliest explanation is that the sharing feature treats a dataset read as "credentials shared". **Nothing was sent:** `commissioner-cli` checks the connection before sending, so this error means the request never left your computer. You can check with `--dry-run`: the device is still on its old network with the same active timestamp. Fix: run `--dry-run` and then `--from-template`, each with a new code. In the two-step run, the device accepted the pending dataset and even answered the read-back afterwards.

**"The device is no longer on the network the template was saved from"**
The device's extended PAN ID or active timestamp has changed since the dry run. Run `--dry-run` again to refresh the template.

**"Field check: FAILED"**
The script names the problem fields:
- *Copied from the device:* the device's dataset has a field that your dataset doesn't, for example a channel mask or a newer Thread 1.4 field.
- *Missing from the pending dataset:* your dataset has a field the device's doesn't.
- *TLV types this script cannot carry over:* your dataset has a TLV type that 1.2 doesn't map yet.

In every case, the real run stops before sending anything. Open an issue with the printed field names and TLV type numbers. Don't include any values.

**"Could not read the device dataset" (`INVALID_STATE: the DTLS session is not connected`)**
The device closed the session a few seconds after connecting, before answering. The log shows `petition succeed`, then a retransmitted `/c/ag` request that fails. This happens intermittently on the Google TV Streamer, and running again with a new code usually works. **Nothing is changed when this happens:** the script stops before it sends anything. It seems to happen more often with a device that has already joined a larger network. A likely explanation, though unconfirmed, is that the device is no longer that network's leader, so it has to relay the request over Thread instead of answering it directly.

**JSON errors like `key 'Number' not found`**
Your `ot-commissioner` version formats datasets differently. Run `--dry-run` and compare the field names, or open an issue including the error text. Remove any keys first.

**"The session closed before the device answered"**
The connection dropped while the device was handling the new dataset, so the script can't tell whether it was applied. It waits and checks mDNS anyway. If the switch isn't confirmed, wait until the delay has definitely passed, then run `--dry-run` with a new code to see which network the device is on before trying again.

**The switch is never confirmed**
Check HA's Thread page and the OTBR logs anyway. The confirmation step assumes the device's `_meshcop._udp` service has the same instance name as its `_meshcop-e._udp` service, which is how OpenThread does it, but other firmware may differ. Before 1.2, the lookup could also fail on systems running `avahi-daemon`: the Streamer didn't answer a cold, direct SRV/TXT query from python-zeroconf. Since 1.2, the script browses `_meshcop._udp` first, which makes the lookup work.

**The device goes back to a Google network later**
Note what triggered it (a reboot, an update, a Google Home change), then run the script again.

## Changelog

**1.2**
- Field check: the pending dataset must contain exactly the destination network's fields. Only field names are printed.
- Two-step mode: `--dry-run` saves the device's dataset format (secrets zeroed), and `--from-template` sends the pending dataset as the first command. This works around Streamers that close the session after the dataset is read.
- New options `--from-template` and `--template`.
- mDNS confirmation browses `_meshcop._udp` before resolving. This fixes "couldn't confirm" on systems with `avahi-daemon`.
- Docs: conda build without `sudo`, CMake 4 note, test report for a tado Bridge X network, timestamp and recovery notes.

**1.1**
- Initial public version.

## Background and references

- OpenThread Border Agent / ephemeral key (ePSKc) documentation: https://openthread.io/reference/group/api-border-agent
- OT Commissioner CLI guide: https://openthread.io/guides/border-router/external-commissioning/cli
- Home Assistant community discussions about the Google TV Streamer creating its own Thread network

## License

Released under the [MIT License](LICENSE). You're free to use, modify, and share the script, including in other projects, as long as the copyright and license notice are kept. It's provided as-is, with no warranty of any kind. See [Warnings](#warnings).
