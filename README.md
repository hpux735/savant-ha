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

## Shared media-server routing

Several room media-player entities can be endpoint projections of one physical Savant
media server. For example, room-specific `Music` entities share the server's content and
transport while each entity controls whether its own Savant zone receives that server.
The integration derives this relationship from the configuration archive's physical
component and room identifiers, never from entity IDs, room names, or display names.

Every projected media-player entity exposes these stable state attributes:

- `savant_media_server_id`: opaque physical media-server identifier.
- `savant_zone_id`: opaque identifier for this endpoint's Savant zone.
- `savant_selected_zone_entity_ids`: sorted exact HA entity IDs currently selected for
  the server. Every alias of one server reports the same list.
- `savant_shared_media_server`: `true` when multiple imported endpoints project the server.

Use `savant_ha.set_media_server_zones` before `media_player.play_media`. The
`zone_entity_ids` list is the complete desired replacement state, not an incremental
addition. The service removes extra selected endpoints first, adds missing endpoints,
waits for authoritative Savant state pushes, and returns the verified final topology when
the caller requests response data. Repeating an already-correct request sends no controls.

Music browse IDs are opaque, deterministic hashes of the captured node. They remain
stable when the same catalog node is returned again, while the Savant routing metadata
remains private to the integration.

Isolate playback to one room:

```yaml
action: savant_ha.set_media_server_zones
data:
  entity_id: media_player.master_bath_music
  zone_entity_ids:
    - media_player.master_bath_music
response_variable: savant_route
```

Select multiple rooms:

```yaml
action: savant_ha.set_media_server_zones
data:
  entity_id: media_player.master_bath_music
  zone_entity_ids:
    - media_player.master_bath_music
    - media_player.dining_room_music
```

Deselect every imported endpoint for the server:

```yaml
action: savant_ha.set_media_server_zones
data:
  entity_id: media_player.master_bath_music
  zone_entity_ids: []
```

An empty route works when every currently selected endpoint supports Savant `PowerOff`;
otherwise the service fails explicitly and reports the actual selected set. Only imported
endpoint projections have HA entity IDs and can be requested or reported, but the complete
archive topology is retained so exact replacement also deselects active, unimported
endpoints. Existing entries must be reconfigured once to acquire that complete topology;
until then exact routing and grouping fail explicitly while existing media controls continue
to work.

Standard Home Assistant grouping is also supported for shared servers. `group_members`
reports the same exact selected endpoint IDs, `media_player.join` adds same-server
endpoints, and `media_player.unjoin` removes the targeted endpoint. Cross-server joins are
rejected. Grouping and exact replacement use the same per-server lock and verification
path. Playback itself remains backward compatible and does not implicitly isolate a room.

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
