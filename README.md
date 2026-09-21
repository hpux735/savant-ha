# Savant HA

A [HACS](https://hacs.xyz)-compatible Home Assistant integration that controls a
**Savant host** on the local network.

> **Provenance.** This integration targets a protocol that was **reconstructed by
> black-box reverse engineering** — passive packet capture, mDNS enumeration, and a live
> MITM — with **no** vendor documentation, firmware, or source code. The reconstruction
> lives in the sibling project
> [`savant-app-re`](https://git.housedillon.com/wdillon/savant-app-re) (its
> `PROTOCOL.md` / `REVERSE_ENGINEERING.md`). This repo keeps a condensed copy in
> [`PROTOCOL.md`](PROTOCOL.md). Nothing here is drawn from a vendor SDK or spec.

## What it talks

The host exposes a WSS control channel (`wss://<host>:<dynamic-port>/`, WebSocket
subprotocol `rpm-protocol`, MessagePack payloads, gzip in the host→client direction,
UDP 9101/9103 discovery). The integration:

1. discovers the control port / `homeId` via a UDP broadcast (or uses one you provide),
2. connects over WSS (validating nothing, matching the app's own behavior),
3. logs in with a host-local account (or a cached `hostToken`) and subscribes to state,
4. uses the configuration archive as the device/room inventory, then
   receives `state/update` pushes and issues `service/request` commands.

## Installation

### HACS (recommended)

1. HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://git.housedillon.com/wdillon/savant-ha2` (category **Integration**)
3. **Download**, then restart Home Assistant.

### Manual

```bash
cd /path/to/ha/config/custom_components
git clone https://git.housedillon.com/wdillon/savant-ha2.git savant_ha
```

## Configuration

Settings → Devices & Services → **Add Integration** → **Savant**.

1. Enter the host address (leave the port blank to auto-discover it — recommended).
2. **Log in** with a host-local account (username/password). This is how the host
   authenticates a headless client; on success the integration authenticates, downloads
   the host's config archive (`uiconfig.tar.gz`), and enumerates the device inventory.
3. **Choose the devices** to import. Devices are individual
   controllable things (a light load, a thermostat, a shade, a fan, an AV zone, …) — not
   rooms — and each maps to one or more Home Assistant entities. On accept the entities
   are created (you can move them freely afterwards).

Optional advanced settings (a cached `hostToken`, `cloudToken`/`configurationID`, or
extra room names) live behind the integration's **Configure** button.

## Entities (created only for the devices you import)

| Platform | Device source | Entities |
|---|---|---|
| Light | capture-backed `LightEntities` dimmer/color loads | brightness, on/off, and RGBW where state supports it |
| Climate | `HVACEntities` | configured captured modes/setpoints and read-only CoolMaster state |
| Cover | `ShadeEntities` | capture-backed shade open/close and position; stop is not captured |
| Fan | `FanEntities` | exact per-device state, read-only |
| Media Player | `ServiceImplementationZonedService` (one selectable source or Apple TV endpoint per room) | music: browsing, global catalog search, playback, now-playing, power, volume, transport, and album art; Apple TV: archive-declared power, volume, and play/pause |
| Scene | Savant dashboard `scenesAndFoldersReduced` updates | standalone native `scene.turn_on` activation |

## Limitations / open questions

These are inherited from the sibling protocol document — see `PROTOCOL.md` §7:

- **`secretKey`** is issued by the host on login but its purpose is unknown; the
  integration ignores it.
- **Fans / door-locks / garages** have incomplete or state-only evidence and no captured
  general control shapes. Fans are read-only; locks and garages are not imported.
- Shade positioning is available only for capture-backed `ShadeSet` and `RFShadeSet` backends.
- Music mute/repeat remain one-way (`MuteOn`/`RepeatOn`) and are therefore not advertised
  as Home Assistant controls. Search filters and pagination beyond the captured defaults
  are not exposed.

New climate imports honor archive `isCelsius`, range, and mode-capability fields. Legacy
fallback devices retain the original Fahrenheit defaults.

## Development

Provenance rules — this repo's `AGENTS.md` — are mandatory: every change must be paired
with the prompt that produced it (recorded in `savant_ha_prompts.csv`), and no
proprietary knowledge may be introduced.
