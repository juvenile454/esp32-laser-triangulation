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
> The project switches a 5 mW, 650 nm **Class IIIa** line laser. Never look into
> the beam or its specular reflections, and keep reflective tools and parts out
> of the beam path.

- Firmware upload and hardware actuation are not CI operations. Before
  uploading, identify the intended board and serial port and put the manual
  laser switch in the off position.
- The firmware drives the verified `2N2222` low-side stage on GPIO14. Command
  `1` switches the laser on until command `0`, a reset, power removal or the
  firmware's **60 s maximum-on-time watchdog**, which switches it off and prints
  `LASER OFF pin=14 reason=timeout`. The watchdog is checked between serial
  commands, so it can overshoot by one command's duration (roughly 7 s for an
  RGB565 frame). **It has not yet been tested on the board**, and
  it is software on the same microcontroller: a firmware hang or crash defeats
  it. It is not an interlock.
- A normal viewer shutdown attempts to switch the laser off, but an abrupt host,
  process or cable failure is not an interlock. The manual switch in the 5 V
  lead is the authoritative physical disconnect.
- Do not leave the enabled prototype unattended.
- Electrical and optical limits must be verified for your exact hardware before
  you copy or extend the circuit.

## Machine-integration boundary

Nothing in this repository is authorised to command a robot, welding system or
production machine. Any later integration requires an independently reviewed
interface with bounded outputs, stale-data handling, an explicit enable state,
watchdog behaviour and machine-side validation.
