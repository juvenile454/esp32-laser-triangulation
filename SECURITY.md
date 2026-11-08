# Security and safety

## Reporting a vulnerability or unsafe behaviour

Please do **not** open a public issue for a security problem, a leaked
credential or hardware behaviour that could hurt someone.

Use GitHub's private reporting instead: open the repository's **Security** tab
and choose **Report a vulnerability**. The report is visible only to the
maintainer. Include what you observed, the firmware commit, and how to
reproduce it.

This is a hobby-scale research prototype maintained in spare time. Reports are
read, but there is no guaranteed response time.

## Supported versions

Only the latest commit on `main` is maintained. There are no long-term
release branches.

## Credentials and private material

Never commit:

- API tokens, SSH keys or environment files;
- Wi-Fi credentials or serial-number inventories;
- account or machine credentials;
- factory-flash backups or arbitrary device dumps;
- captures containing unintended private information.

If a credential is committed, rotate it first and then remove it from Git
history. Deleting it only from the latest commit is insufficient.

## Laser and hardware safety

> [!WARNING]
> The project switches a 5 mW, 650 nm line laser, labelled **Class IIIa** (US).
> That corresponds roughly to **Class 3R under IEC 60825-1**. The power is taken
> from the module label and has not been measured. Never look into the beam or
> its specular reflections, keep reflective tools and parts out of the beam
> path, and wear laser safety glasses rated for 650 nm when you work close to
> the beam.

- Firmware upload and hardware actuation are not CI operations. Before
  uploading, identify the intended board and serial port and put the manual
  laser switch in the off position.
- The firmware drives the verified `2N2222` low-side stage on GPIO14. Command
  `1` keeps the laser on until command `0`, a reset or power removal. **There is
  no firmware maximum-on-time watchdog yet**; draft PR #7 adds one and has not
  been tested on hardware.
- The live viewer limits the on-time on the host side (`--laser-max-on-s`,
  default 60 s) and switches the laser off when the browser page stops polling.
  This only works while the viewer process and the USB link are alive.
- A normal viewer shutdown switches the laser off. If it cannot confirm that,
  it prints `LASER STATE UNKNOWN - use the manual switch` and exits with code 1.
  An abrupt host, process or cable failure is not an interlock. The manual
  switch in the 5 V lead is the authoritative physical disconnect.
- Do not leave the enabled prototype unattended.
- Electrical and optical limits must be verified for your exact hardware before
  you copy or extend the circuit.

## Live viewer exposure

The viewer can switch the laser, so it is treated as a security boundary even
though it only listens on loopback:

- `--bind` accepts only `127.0.0.1`, `::1` or `localhost`;
- requests with a foreign `Host` header are rejected (421), which blocks DNS
  rebinding;
- every state-changing request must be a same-origin `POST` with
  `Content-Type: application/json` and the random per-start token from the
  served page; otherwise it is rejected (403 or 415);
- the status endpoint and the MJPEG stream also require the token;
- responses carry a strict Content-Security-Policy, `X-Frame-Options: DENY`
  and `X-Content-Type-Options: nosniff`.

Do not expose the viewer through a reverse proxy, tunnel or port forward.

## Machine-integration boundary

Nothing in this repository is authorised to command a robot, welding system or
production machine. Any later integration requires an independently reviewed
interface with bounded outputs, stale-data handling, an explicit enable state,
watchdog behaviour and machine-side validation.
