#!/usr/bin/env python3
"""
thread_join.py - move a Thread 1.4 border router (e.g. a Google TV Streamer)
onto an existing Thread network, such as the one run by Home Assistant's
OpenThread Border Router, using the device's ePSKc code
("Share Thread network credentials").

Unofficial and unsupported. Use at your own risk. See README.md.

Copyright (c) 2026 CamelusBactrianus
SPDX-License-Identifier: MIT
"""
__version__ = "1.1"

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
import time

try:
    import pexpect
    from zeroconf import Zeroconf, ServiceBrowser
except ImportError:
    sys.exit('Missing Python packages. Install them with:\n'
             '  pip install pexpect zeroconf')

DELAY_MS = 300000  # Google TV Streamer enforces a 5-minute minimum anyway
REQUIRED_TLVS = {0: 'channel', 1: 'PAN ID', 2: 'extended PAN ID', 3: 'network name',
                 4: 'PSKc', 5: 'network key', 7: 'mesh-local prefix', 12: 'security policy'}

KEY_RE = re.compile(r'("(?:NetworkMasterKey|NetworkKey|PSKc)"\s*:\s*")[0-9a-fA-F]+')
HEX32_RE = re.compile(r'\b[0-9a-fA-F]{32}\b')


def redact(s):
    return HEX32_RE.sub('<redacted>', KEY_RE.sub(r'\1<redacted>', s))


def die(msg):
    sys.exit('ERROR: ' + msg)


def u16(b):
    return int.from_bytes(b, 'big')


def to_int(v):
    """Parse 43981, '43981', '0xABCD' or 'abcd' as an int."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    s = str(v).strip()
    try:
        return int(s, 0)
    except ValueError:
        return int(s, 16)


# ---------------------------------------------------------------- dataset --

def parse_tlv(h):
    h = ''.join(h.split())
    if h[:2].lower() == '0x':
        h = h[2:]
    b = bytes.fromhex(h)
    out, i = {}, 0
    while i + 2 <= len(b):
        t, l = b[i], b[i + 1]
        out[t] = b[i + 2:i + 2 + l]
        i += 2 + l
    return out


def like(old, raw):
    """Format bytes the same way commissioner-cli formatted the old value."""
    h = raw.hex()
    if isinstance(old, int) and not isinstance(old, bool):
        return int(h, 16)
    if isinstance(old, str) and old.lower().startswith('0x'):
        return '0x' + h.upper()
    if isinstance(old, str) and any(c.isupper() for c in old):
        return h.upper()
    return h


def chan(d):
    return next(v for k, v in d['Channel'].items() if k != 'Page')


def build_pending(ha, st):
    """Pending dataset = destination network's values, in the target's JSON format."""
    keyname = next((n for n in ('NetworkMasterKey', 'NetworkKey') if n in st), None)
    if not keyname:
        die("the device didn't report its network key field; stopping to be safe")

    p = json.loads(json.dumps(st))
    numkey = next((k for k in st.get('Channel', {}) if k != 'Page'), 'Number')
    p['Channel'] = {numkey: u16(ha[0][1:3]), 'Page': ha[0][0]}
    p['PanId'] = like(st.get('PanId', '0x0'), ha[1])
    p['ExtendedPanId'] = like(st.get('ExtendedPanId', ''), ha[2])
    p['NetworkName'] = ha[3].decode()
    p['PSKc'] = like(st.get('PSKc', ''), ha[4])
    p[keyname] = like(st[keyname], ha[5])
    p['MeshLocalPrefix'] = ':'.join(ha[7][j:j + 2].hex() for j in range(0, 8, 2)) + '::/64'

    oldf = st.get('SecurityPolicy', {}).get('Flags', '')
    flags = ha[12][2:] if isinstance(oldf, str) or oldf > 255 else ha[12][2:3]
    p['SecurityPolicy'] = {'Flags': like(oldf, flags), 'RotationTime': u16(ha[12][:2])}

    if 53 in ha:  # channel mask
        b, i, masks = ha[53], 0, []
        while i + 2 <= len(b):
            page, l = b[i], b[i + 1]
            masks.append({'Masks': b[i + 2:i + 2 + l].hex(), 'Page': page})
            i += 2 + l
        p['ChannelMask'] = masks

    ha_sec = int.from_bytes(ha[14], 'big') >> 16 if 14 in ha else 0
    p['ActiveTimestamp'] = {'Seconds': max(ha_sec, st['ActiveTimestamp']['Seconds']) + 1,
                            'Ticks': 0, 'U': 0}
    p['PendingTimestamp'] = {'Seconds': int(time.time()), 'Ticks': 0, 'U': 0}
    p['DelayTimer'] = DELAY_MS
    return p, keyname


# ------------------------------------------------------------------- mDNS --

def props_of(info):
    return {k.decode(errors='ignore'): v for k, v in (info.properties or {}).items()}


def find_epskc(wait=8):
    """Devices currently showing a 'Share Thread network credentials' code."""
    found = {}

    class Listener:
        def add_service(self, zc, t, n):
            info = zc.get_service_info(t, n, 3000)
            if not info or not info.parsed_addresses():
                return
            addrs = info.parsed_addresses()
            v4 = [a for a in addrs if ':' not in a]
            nn = props_of(info).get('nn')
            found[n] = (v4[0] if v4 else addrs[0], info.port,
                        nn.decode(errors='ignore') if isinstance(nn, bytes) else '?')

        update_service = remove_service = lambda *a: None

    zc = Zeroconf()
    ServiceBrowser(zc, '_meshcop-e._udp.local.', Listener())
    time.sleep(wait)
    zc.close()
    return list(found.items())


def meshcop_xpan(instance):
    """Extended PAN ID a device advertises on its normal _meshcop service."""
    name = instance.split('._meshcop-e.')[0] + '._meshcop._udp.local.'
    try:
        zc = Zeroconf()
        try:
            info = zc.get_service_info('_meshcop._udp.local.', name, 8000)
        finally:
            zc.close()
    except Exception:
        return None
    if not info:
        return None
    xp = props_of(info).get('xp')
    return xp.hex() if isinstance(xp, bytes) else None


# ------------------------------------------------------------ commissioner --

class Commissioner:
    def __init__(self, cfg):
        self.closed = False
        self.c = pexpect.spawn('commissioner-cli', [cfg], encoding='utf-8', timeout=60)
        try:
            self.c.expect('> ')
        except (pexpect.EOF, pexpect.TIMEOUT):
            die('commissioner-cli did not start:\n' + (self.c.before or ''))

    def run(self, cmd, timeout=60):
        if self.closed:
            return False, 'commissioner already closed'
        self.c.sendline(cmd)
        try:
            i = self.c.expect([r'\[done\]', r'\[failed\]'], timeout=timeout)
        except pexpect.TIMEOUT:
            return False, (self.c.before or '') + '\n(timed out waiting for a response)'
        except pexpect.EOF:
            self.closed = True
            return False, (self.c.before or '') + '\n(commissioner-cli exited)'
        return i == 0, self.c.before

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.c.sendline('exit')
            self.c.expect(pexpect.EOF, timeout=10)
        except Exception:
            self.c.terminate(force=True)


def json_from(out):
    s, e = out.find('{'), out.rfind('}')
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(out[s:e + 1])
    except ValueError:
        return None


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(
        description='Move a Thread 1.4 border router onto your Thread network using its ePSKc code.')
    ap.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    ap.add_argument('--dry-run', action='store_true',
                    help='connect and show which network the device is on, but change nothing')
    ap.add_argument('--no-confirm', action='store_true',
                    help="don't wait to confirm the switch via mDNS afterwards")
    ap.add_argument('--debug', action='store_true',
                    help='verbose commissioner/DTLS logging, shown on failure. The debug log can '
                         'contain decrypted network data: do not share it publicly.')
    args = ap.parse_args()

    if not shutil.which('commissioner-cli'):
        die('commissioner-cli not found in PATH. Build and install ot-commissioner first (see README).')

    try:
        ha = parse_tlv(getpass.getpass('Paste your Thread network active dataset TLV (input hidden): '))
    except ValueError:
        die('that is not valid hex')
    missing = [v for k, v in REQUIRED_TLVS.items() if k not in ha]
    if missing:
        die('the TLV is missing: ' + ', '.join(missing))
    net, xpan = ha[3].decode(errors='ignore'), ha[2].hex()
    print(f"Destination network: {net} (channel {u16(ha[0][1:3])}, PAN 0x{ha[1].hex().upper()})")

    input('\nOn the device, open "Share Thread network credentials", then press Enter...')
    code = re.sub(r'[\s-]', '', input('Code shown on the device: '))
    if not code:
        die('no code entered')

    print('Searching for devices sharing Thread credentials...')
    svcs = find_epskc()
    if not svcs:
        die('no device is advertising an ePSKc service. Is the code screen still open, '
            'and is this computer on the same network segment as the device?')
    idx = 0
    if len(svcs) > 1:
        for i, (n, (ip, port, nn)) in enumerate(svcs, 1):
            print(f'  {i}) {n}  {ip}:{port}  current network: {nn}')
        while True:
            sel = input('Which one? ').strip()
            if sel.isdigit() and 1 <= int(sel) <= len(svcs):
                idx = int(sel) - 1
                break
    name, (ip, port, _) = svcs[idx]
    if ':' in ip:
        print('Note: only an IPv6 address was found; link-local addresses may not work.')
    print(f'Using {name} at {ip}:{port}')

    delay, verified = DELAY_MS // 1000, True
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        cfg = os.path.join(tmp, 'cfg.json')
        log = os.path.join(tmp, 'commissioner.log')
        stf = os.path.join(tmp, 'target.json')
        pf = os.path.join(tmp, 'pending.json')
        with open(cfg, 'w') as f:
            json.dump({'Id': 'OT-commissioner', 'EnableCcm': False,
                       'PSKc': code.encode().hex(), 'LogFile': log,
                       'LogLevel': 'debug' if args.debug else 'info',
                       'EnableDtlsDebugLogging': bool(args.debug)}, f)

        c = Commissioner(cfg)

        def fail(msg, out=''):
            if out.strip():
                print(redact(out))
            c.close()  # commissioner-cli flushes its log on exit
            if os.path.exists(log):
                with open(log, errors='ignore') as f:
                    lines = f.readlines()[-60:]
                if lines:
                    print('--- last log lines ---')
                    print(redact(''.join(lines)))
            die(msg)

        try:
            ok, out = c.run(f'start {ip} {port}')
            if not ok:
                fail('could not connect (wrong or expired code?)', out)
            ok, out = c.run('active')
            if not ok or 'true' not in out:
                fail('commissioner did not become active', out)
            print('Connected as commissioner.')

            ok, out = c.run(f'opdataset get active --export {stf}')
            if not ok or not os.path.exists(stf):
                fail('could not read the device dataset', out)
            with open(stf) as f:
                st = json.load(f)
            cur_name = st.get('NetworkName')

            # Safety: never push a pending dataset into the destination network itself.
            if str(st.get('ExtendedPanId', '')).lower() == xpan.lower():
                print(f"This device is already on '{net}'. Nothing to do.")
                return
            print(f"Device is on '{cur_name}' (channel {chan(st)}).")
            if args.dry_run:
                print('Dry run: no changes made.')
                return
            print(f"Moving it to '{net}'...")

            p, keyname = build_pending(ha, st)
            with open(pf, 'w') as f:
                json.dump(p, f)
            ok, out = c.run(f'opdataset set pending --import {pf}')
            uncertain = False
            if not ok:
                if 'REJECTED' in out or 'INVALID_ARGS' in out:
                    fail('the device rejected the pending dataset', out)
                # The session dropped before we got an answer: it may or may not have been applied.
                uncertain = True
                print(redact(out).strip())
                print('WARNING: the session closed before the device answered. The dataset may '
                      'or may not have been applied; checking via mDNS after the delay.')

            ok, out = c.run('opdataset get pending') if not uncertain else (False, '')
            got = json_from(out) if ok else None
            if not got and uncertain:
                verified = False
            elif not got:
                verified = False
                print('The device closed the session after accepting the dataset (normal for '
                      'Google TV Streamer); confirming via mDNS instead.')
            else:
                checks = {
                    'network name': got.get('NetworkName') == p['NetworkName'],
                    'extended PAN ID': str(got.get('ExtendedPanId', '')).lower() == str(p['ExtendedPanId']).lower(),
                    'PAN ID': to_int(got.get('PanId', -1)) == to_int(p['PanId']),
                    'channel': chan(got) == chan(p),
                    'network key': str(got.get(keyname, '')).lower() == str(p[keyname]).lower(),
                    'security flags': str(got['SecurityPolicy']['Flags']).lower()
                                      == str(p['SecurityPolicy']['Flags']).lower(),
                }
                for k, v in checks.items():
                    print(f"  {'OK ' if v else 'BAD'} {k}")
                if not all(checks.values()):
                    fail('the pending dataset on the device does not match; check before retrying')
                delay = int(got.get('DelayTimer', DELAY_MS)) // 1000
        finally:
            c.close()

    print(f'\nPending dataset {"accepted" if verified else "sent"}. '
          f'The device should switch in about {delay} s.')
    if args.no_confirm:
        return
    print('Waiting to confirm (Ctrl+C to skip)...')
    deadline = time.time() + delay + 180
    time.sleep(delay + 15)
    while time.time() < deadline:
        cur = meshcop_xpan(name)
        if cur and cur.lower() == xpan.lower():
            print(f"Confirmed: the device is now advertising '{net}'.")
            return
        print(f"  not yet (advertising {cur or 'nothing'}); checking again in 20 s...")
        time.sleep(20)
    print("Couldn't confirm via mDNS. Check your Thread network in Home Assistant and the OTBR logs.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nInterrupted.')
