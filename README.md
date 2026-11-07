# even_g1_ota

Flash firmware onto Even Realities G1 glasses over BLE. One script, one argument.

```
python3 g1_ota.py application_update.zip        # both cores
python3 g1_ota.py app_update.bin                # app core only
python3 g1_ota.py net_core_app_update.bin       # net core only
```

The input is either the update zip (the `dfu_application.zip` a build produces,
or a vendor `even_g1_x.yz.zip`) or one of the two signed MCUboot images inside
it. Which core a `.bin` belongs to is read from the image itself, not from its
file name.

Every G1 arm in range is flashed, left then right. Each arm is its own nRF5340
with its own firmware; to flash only one, leave the other in the closed case.

## Read this first

- **There is no revert.** The G1 bootloader is overwrite-only: when the arm
  reboots, the running image is erased and replaced. If the new image does not
  bring up BLE, the only recovery is SWD after disassembly.
- The bootloader only installs images signed with the key it was built with
  (RSA-2048 PSS). A wrongly signed image is refused and the arm keeps running
  what it had.
- Keep the arms well charged and do not interrupt the reboot. Power loss while
  the bootloader is copying is the one way to brick an arm with a good image.
- Flashing only one core, or only one arm, can leave versions that do not work
  together. The net core carries the BLE stack.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python g1_ota.py <file>
```

Linux needs BlueZ running; no pairing is involved.

The arms must be advertising and willing to talk to this computer:

- Close the Even app or turn off the phone's Bluetooth, so the phone does not
  hold the connection.
- An arm drops any host other than its paired phone unless it is in bind mode,
  which is **in the charging case with the lid open**. (Firmware 1.66,
  `ancs_connected()` / `check_bind_status()`.)

## What it does, per arm

1. Connects and reads the image state.
2. Skips the app core image if the arm is already running it.
3. Uploads each image to its secondary slot (external flash). The running
   firmware is untouched up to here, and an interrupted run resumes where it
   stopped.
4. Reads the state back and checks that the staged hash matches the file.
5. Marks the staged images for install and resets the arm. **This is the point
   of no return.**
6. Waits for the arm to come back (the copy takes about half a minute),
   reconnects and checks the result.

The script stops at the first arm that fails, so the other one is left alone.
Running it again after a failure is safe: it picks up from whatever state the
arm is in.

The net core result is reported as installed when the bootloader has consumed
the staged image. The hash of the running net core cannot be read back over
this protocol.

## Origin

Derived from the `g1-fw` repository (`freedom_repos/g1-fw` in this workspace):

| Here | From |
|---|---|
| SMP framing, upload, state read/set | `companion/g1/mcumgr.py` |
| BLE transport, response reassembly | `companion/g1/transport_smp_real.py` |
| Stage → verify → arm → reboot → verify sequence, resume after a link drop | `companion/g1_glasses_ota/glasses_ota_task.py` |
| `#r` reboot command | `companion/g1_glasses_ota/dfu_control_task.py` |

Left behind: the Renode emulator transport, the nRF5340-DK rehearsal, the macOS
app wrapper, the per-phase CLI with arming flags, slot erase, DFU-mode entry,
and all the QSPI/memory probing tools.

Three things differ from the original, each based on the reconstructed 1.66
firmware source rather than on a test against real glasses:

- **Reset** uses the mcumgr OS reset command (what the Even app sends; the
  handler is in the firmware) and falls back to the `#r` text command.
- **Heartbeat.** The firmware disconnects a host that has sent nothing on the
  command channel for 30 s, which is what cut the original uploads every
  ~360 KB. The script sends the app's heartbeat packet every 10 s during an
  upload. The resume logic is still there if the link drops anyway.
- **Image hash** comes from the file's MCUboot trailer and is compared with what
  the arm reports before anything is armed.

## Test

```
.venv/bin/python test_g1_ota.py               # synthetic images
.venv/bin/python test_g1_ota.py update.zip    # the images of a real update zip
```

`test_g1_ota.py` runs the whole script against an in-process fake arm, with no
radio involved: upload, link drops and resume, heartbeat, reset fallback,
install, reruns, and the error paths. It takes about a minute. The fake is a
model of the firmware's behaviour, so a pass says the script's logic is
consistent with that model, not that the glasses agree with it.

## Status

Passes the test above, and parses every firmware zip in
`reference_material/firmware`. **Not yet run against real glasses.**
