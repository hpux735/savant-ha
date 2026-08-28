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
3. performs the `session/devicePresent` handshake and subscribes to state keys,
4. receives `state/update` pushes and issues `service/request` commands.

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

1. Enter the host address; leave the port blank to auto-discover it.
2. Optional advanced fields (credentials, `hostToken`/`cloudToken`/`configurationID`,
   and a list of room names) are on the second step. Room names are install-specific and
   cannot be enumerated over the protocol, so list them (comma- or newline-separated) if
   you want room lights/temperature — otherwise only HVAC, audio zones, and global
   sensors appear.

## Entities

| Platform | Source state keys | Notes |
|---|---|---|
| Climate | `HVAC Controller.HVAC_controller.*` | mode (Off/Heat/Cool/Auto), heat/cool setpoints, current temp/humidity |
| Light | `<room>.RoomLightsAreOn` / `BrightnessLevel` | room-level on/off only (see limitations) |
| Media Player | `Music.Audio Zone N.SVC_AV_SAVANTMUSIC.*` | now-playing, power on, volume (when the active room is known) |
| Sensor | `global.CurrentTemperature`, `…ThermostatCurrentHumidity_1`, `<room>.RoomCurrentTemperature` | temperature / humidity |

## Limitations / open questions

These are inherited from the sibling protocol document — see `PROTOCOL.md` §7:

- **`hostToken` derivation** is not yet known. The integration accepts an explicit
  `hostToken`, or falls back to base64 of `username:password` when both are supplied.
- **Room-level light dimming/colour** needs per-load `Address*` values (`DimmerSet`),
  which aren't surfaced by the state bus yet — lights are on/off only.
- **Shades / fans / door-locks** have observed state keys but no captured set-verbs, so
  they aren't exposed yet.
- **Media transport** (play/pause/mute/power-off) verbs are not in the observed catalog;
  only `PowerOn`/`SetVolume` are wired.
- **Scenes** don't have a confirmed "run scene" verb yet.

Temperature is assumed Fahrenheit (matching the observed `SchedulerSettings` scale).

## Development

Provenance rules — this repo's `AGENTS.md` — are mandatory: every change must be paired
with the prompt that produced it (recorded in `savant_ha_prompts.csv`), and no
proprietary knowledge may be introduced.