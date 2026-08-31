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
4. discovers the room list from the scene definitions and per-room state keys, then
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
3. **Choose the devices** to import and override each one's area. Devices are individual
   controllable things (a light load, a thermostat, a shade, a fan, an AV zone, …) — not
   rooms — and each maps to one or more Home Assistant entities. On accept the entities
   are created (you can move them freely afterwards).

Optional advanced settings (a cached `hostToken`, `cloudToken`/`configurationID`, or
extra room names) live behind the integration's **Configure** button.

## Entities (created only for the devices you import)

| Platform | Device source | Entities |
|---|---|---|
| Light | `LightEntities` (per-load `addresses` + `stateName`) | brightness / on-off |
| Climate | `HVACEntities` | mode (Off/Heat/Cool/Auto), heat/cool setpoints, current temp/humidity |
| Cover | `ShadeEntities` / `GarageEntities` | shade position (read-only for now) |
| Fan | `FanEntities` | on/off (read-only for now) |
| Media Player | `Zones` (AV zones) | now-playing, power on, volume |
| Sensor | `global.CurrentTemperature`, `<room>.RoomCurrentTemperature` | temperature |

## Limitations / open questions

These are inherited from the sibling protocol document — see `PROTOCOL.md` §7:

- **`secretKey`** is issued by the host on login but its purpose is unknown; the
  integration ignores it.
- **Light colour** needs the per-load `CurrentColor_N_<addr>` state + the `bleColor`
  `DimmerSet` args; only brightness/on-off are wired so far.
- **Shades / fans / door-locks** have observed state keys but no captured set-verbs, so
  their entities are read-only for now.
- **Media transport** (play/pause/mute/power-off) verbs are not in the observed catalog;
  only `PowerOn`/`SetVolume` are wired.
- **Scenes** don't have a confirmed "run scene" verb yet.

Temperature is assumed Fahrenheit (matching the observed `SchedulerSettings` scale).

## Development

Provenance rules — this repo's `AGENTS.md` — are mandatory: every change must be paired
with the prompt that produced it (recorded in `savant_ha_prompts.csv`), and no
proprietary knowledge may be introduced.