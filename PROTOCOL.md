# Savant RPM Protocol — local condensed copy

> **Provenance.** This file is a *condensed* copy of the protocol knowledge distilled
> in the sibling project
> [`savant-app-re/PROTOCOL.md`](https://git.housedillon.com/wdillon/savant-app-re).
> That project reconstructed the Savant **app ↔ host** protocol by **black-box
> reverse-engineering** (passive packet capture, mDNS enumeration, and a live MITM) with
> **no** vendor documentation, firmware, or source code. Nothing here is drawn from a
> vendor SDK or spec. The sibling `PROTOCOL.md` and
> `REVERSE_ENGINEERING.md` are the canonical sources; this copy exists so a fresh agent
> in *this* repo can resume without re-reading the whole sibling history.

"RPM" = RacePoint Media protocol. Ground truth: the live host (`<HOST_IP>`) + Savant Pro
iOS app 11.2.4, as observed by the sibling project.

---

## 1. Transport & framing

| Layer | Detail |
|---|---|
| TLS | 1.2 / 1.3, **any certificate accepted** (no pinning/validation) — private, not authenticating |
| WebSocket | subprotocol **`rpm-protocol`**, binary frames, path `/`, `wss://<host>:<dynamic-port>` |
| Payload | Usually a MessagePack map per complete WebSocket message; files/images use §5b framing |

- **client → host:** plaintext msgpack.
- **host → client:** **gzip-compressed** msgpack (`0x1f 0x8b`).
- Keepalive: WS `ping`/`pong`, byte `0x45` (`E`), every ~2s.
- **String encoding (live-verified):** the host's msgpack parser does **not** accept the
  new-spec `str8` (`0xd9`) format. Any envelope containing a string longer than 31
  chars — e.g. every long dotted state key such as
  `Savant.Lighting.CurrentDimmerLevel_1_001` — is silently dropped, including the whole
  frame. Strings ≤31 chars use `fixstr`, which is identical in both specs, which is why
  auth, controls, and short-key registrations all worked while `state/register` never
  did. Clients must encode long strings with the legacy raw formats (`raw16` = `0xda`);
  in msgpack-python that means `packb(..., use_bin_type=False)` (verified end-to-end
  against the target host: full initial snapshot + post-command pushes).
- Reassemble fragmented WebSocket messages before gzip/MessagePack decoding. Ping/pong/
  close frames are separate. Classify `01 01`/`01 81` binary transfers before decoding
  MessagePack; one envelope may contain multiple message maps.

### 1.1 Host discovery (UDP)

Before dialing WSS, the app broadcasts a msgpack request to two fixed UDP ports and
reads the host's reply:

```
{"service":"_presence_.ws"}  ->  <HOST_LAN_BROADCAST>:9103   (presence record)
{"service":"_control_.ws"}   ->  <HOST_LAN_BROADCAST>:9101   (control record)
```

Reply keys: `name`, `port`, `homeId`, `hostModel`, `buildVersion`, `UID`, `onboardKey`,
`scheme:"wss"`, `featureLevel`, `configStatus`, `cloudStatus`. The **9101 control record's
`port`** is the terminal port dialed for the RPM WSS connection.

The host also advertises `_soapi_sdo._tcp` through mDNS. This gives Home Assistant an
initial endpoint for configuration, but the UDP control record remains authoritative for
the stable `UID` and current WSS port.

## 2. Message envelope

The full native-client envelope is a msgpack map:

```json
{ "messages": [ ...1..N payload maps... ], "URI": "<path>", "uid": "<deviceUUID>", "user": "<name>" }
```

`messages` = payload(s); `URI` = endpoint; `uid` = stable client identifier; `user` =
display name. Every audited host map and several client endpoint families contain only
`URI` and `messages`; preserve the captured variant for each implemented request.

## 3. Session lifecycle

```
client                            host
  devicePresent ------------------>   {protocolVersion:"4", homeId, device{UID,make,
                                      app,model,OS,type,name}, messageFormat:2}
                                      (cloud path adds cloudToken + configurationID;
                                      a local-only session omits both)
  <-- deviceRecognized -----------   host identity + feature catalog + the flag
                                      authentication:true/false (§3.0)
  authenticationRequest ---------->   one of two forms:
      {user, password}                  LOCAL LOGIN — host account (§3.1)
      {hostToken}                       cached credential (re-auth)
  <-- authenticationResponse -------   success: {authorized:true, hostToken, secretKey,
                                      startZone?}
                                       failure: {authorized:false, errorReason, errorCode}
  session/fileDownload ----------->   native client requests uiconfig.tar.gz first
  dis/dashboard/register --------->   {state:"RecentServices"}
  dis/userData/register ---------->   local/user/global setting update channels
  state/register [keys...] -------->   subscribe
  dcm/request {getMode} ----------->
  dis/userData/register ---------->   global/user image update channels
  <-- state/update / dis/<svc>/update  async pushes
  service/request -------------->   device control (verbs)
  ping "E" <-> pong "E" (2s) -      keepalive
```

### 3.0 `session/deviceRecognized` (host → client)

The host's answer to `devicePresent`, sent **before** any authentication. Its
`authentication` field (bool) tells the client whether it must send
`authenticationRequest`: `true` means a login is mandatory before any state
subscription or `service/request` works. Other fields: `users` (list of local accounts),
`homeId`/`hostUID`/`hostName`, `homeInfo`, `hostSecret`, `hostTime`/`hostTimeZone`,
`featureLevel`/`buildNumber`/`buildVersion`/`channel`/`cloudEnvironment`/
`configurationStatus`/`protocolVersion`/`remote`/`update`, and `featureSummary`
(a feature/license catalog).

### 3.1 Local login (`{user, password}`) — cloud-free auth

The `hostToken` is **not** obtained out-of-band: a host-local account authenticates
directly, and the host issues the token (PROTOCOL.md §4.0/§4.1 of the sibling repo).
Live-verified:

```
-> session/devicePresent          {protocolVersion:"4", homeId:<any>, device:{UID:<any>,…},
                                  messageFormat:2}   // homeId/UID not validated; no cloud
<- session/deviceRecognized       {authentication:true, authorized:false, users:[…], …}

-> session/authenticationRequest  {user:"<LOCAL_USER>", password:"<PASSWORD>"}
<- session/authenticationResponse {authorized:true,
                                   hostToken:"<b64 UUID>",   // issued; cache for re-auth
                                   secretKey:"<b64 UUID>",   // purpose unknown
                                   startZone:"<room>"}       // OPTIONAL
```

Failure (bad password):

```
<- session/authenticationResponse {authorized:false, errorReason:"Invalid password",
                                  errorCode:1}
```

Notes for implementers:
- `devicePresent` **must** be sent first; the host answers `deviceRecognized` and only
  then accepts `authenticationRequest`.
- The password is the literal local-account password (no client-side hashing).
- `hostToken`/`secretKey` are opaque strings. Replay `hostToken` unchanged; do not
  decode, normalize, pad, or derive it.
- `permissions`, configuration metadata, and `startZone` are optional and also occur
  on successful local username/password authentication.
- All audited `deviceRecognized` responses require authentication. An
  `authentication:false` response is not captured and must not be assumed authorized
  unless it explicitly carries `authorized:true`.

## 4. Endpoints used by this integration

| URI | Direction | Purpose |
|---|---|---|
| `session/devicePresent` | client → host | register device |
| `session/deviceRecognized` | host → client | host identity + `authentication` flag |
| `session/authenticationRequest` | client → host | present `{user, password}` **or** `{hostToken}` |
| `session/authenticationResponse` | host → client | auth result (`authorized` + `errorReason`/`errorCode` on failure) |
| `state/register` / `state/unregister` | client → host | subscribe/unsubscribe state keys |
| `state/update` | host → client | `{state, value}` pushes |
| `service/request` | client → host | **device control** (§6) |
| `dcm/request` | client → host | device-control-manager RPC (e.g. `getMode`) |
| `dis/dashboard/register` | client → host | subscribe to dashboard/scene pushes |
| `dis/dashboard/request` | both | correlated scene RPCs such as `ApplyScene` |
| `dis/dashboard/update` | host → client | scene-list pushes (`scenesAndFoldersReduced`) |

## 5. State model (dotted keys)

### 5.1 HVAC — `HVAC Controller.HVAC_controller.*` (unit index `_1`)
- Points: `ThermostatCurrentTemperature`, `...SetPoint`, `...HeatPoint`, `...CoolPoint`,
  `...Humidity`, `...HumiditySetPoint`.
- Mode: `ThermostatMode`, `ThermostatHVACState`, `ThermostatFanMode`,
  `ThermostatCurrentFanSpeed`.
- Boolean mode/speed flags: `IsCurrentHVACMode{Off,Cool,Heat,Auto,...}`, `IsCurrentFanSpeed*`,
  `IsThermostatCurrentFanMode{Auto,On,Off}`, `IsThermostatHolding`, `ThermostatAwayState`.

Some CoolMaster Net controllers use two archive-derived state-key address suffixes, e.g.
`ThermostatCurrentTemperature_<address1>_<address2>`; retain the second token and its
zero padding. Captured values can include a unit suffix such as `"67F"`. No CoolMaster
control request has been captured, so its setter payload must not be inferred.
Later capture evidence adds per-unit `ThermostatCurrentSetPoint`, `ThermostatMode`,
`ThermostatFanMode`, and HVAC/fan mode flags with the same suffix. These are read-only
state observations, not evidence for setter payloads.

### 5.2 Rooms — `<Room>.*` (+ how to derive the room list)

Per-room attributes: `ActiveService`, `ActiveServices`, `LastActiveService`,
`CurrentVolume(int or numeric string)`, `IsMuted(bool)`, `RelativeVolumeOnly(bool)`, `RoomLightsAreOn(bool)`,
`BrightnessLevel(int 0-100)`, `RoomFansAreOn(bool)`, `RoomShadesAreOpen(bool)`,
`RoomCurrentTemperature(string, e.g. "72")`, `SleepTimerActive(bool)`,
`SleepTimerRemainingTime` is a string (only `""` was captured). `RoomNumberOfLightsOn` is an int.
`ActiveServices` may be a comma-separated pair of exact configured service identifiers.

**Value formats (live-verified against the app capture):** `RoomLightsAreOn` is a bool;
`BrightnessLevel` is an int 0–100; `RoomCurrentTemperature` is a **string** (not a
number). Per-load lighting keys (from the archive `stateName`) are
`<component>.<logical>.CurrentDimmerLevel_N_<addr>` (int 0–100) and
`<component>.<logical>.CurrentColor_N_<addr>` / `CurrentBleColor_N_<addr>` — a string of
the form `"R,G,B,W,<level>,<level>|kelvin,<level>,<level>|<curve>"` (e.g.
`"083,079,245,000,096,096|6000,096,096|Custom 1"`).

For Home Assistant, the first four values map directly to RGBW channels (0–255) and the
first `<level>` maps to brightness (0–100). Color-capable loads accept `DimmerSet` with
the observed nested `bleColor:{red,green,blue,white,kelvin}` map; omitting that map when
only dimming preserves the host's existing color. Standard dimmers omit color-only fields
and include `IsTrueImage:false`. Color-capable loads require their current RGBW payload
with level zero to turn off. They retain RGBW channels at level zero but do not restore
the prior brightness through `useLastDimmerValue`; the integration restores the last
host-reported active RGBW/brightness state (including state changed outside HA). Standard
dimmers retain the observed `useLastDimmerValue:true` power-on behavior.

**State push behaviour.** `state/register` takes a list of single-key maps
(`messages:[{"state":k}, …]`). Initial values and later updates arrive in variably
batched `state/update` maps, but not every registered key returns a value and there is no
universal snapshot-complete boundary. Empty string is a legitimate value for some fields,
not a universal missing-key response. Apply repeated values idempotently. A `DimmerSet` is confirmed by pushes of the load's
`CurrentDimmerLevel_N_<addr>` / `CurrentColor_N_<addr>` and the room's
`BrightnessLevel` / `RoomLightsAreOn`. An empty `state/update {messages:[]}` alone is
not a registration error (the native app receives one pre-auth). Earlier observations
that this host "pushes nothing" were caused by the client's msgpack `str8` encoding —
see §1; with legacy encoding the target host delivers the full snapshot. Lighting
entities still apply an optimistic local state on command so the UI reflects changes
instantly, but the pushed state is authoritative once it arrives.

**Deriving the room list (no dedicated "get rooms" endpoint).** Room names are arbitrary
host-defined strings; the full set is inferred from (PROTOCOL.md §6.1 of the sibling):

1. **State-key namespace** — strip a known `.<room-attribute>` suffix from the right;
   room names can contain punctuation. No wildcard registration
   exists: subscribe to each `<room>.<attr>` key explicitly.
2. **Scene definitions** — `dis/dashboard` scene objects embed room names, but no single
   scene is guaranteed to contain the complete room set.
3. **`startZone`** on `session/authenticationResponse` names the room the session opens
   in (one room, not the full list).

This integration requests `uiconfig.tar.gz`, registers dashboard state `RecentServices`,
then registers state keys and sends `dcm/request {getMode}`, matching the observed native
startup order. It takes `startZone` from the auth response and derives rooms from any
`<room>.<attr>` state key it sees; each newly discovered room's per-room keys are then
registered.

### 5.3 Audio zones
Ordinary media keys use `<component>.<logical>.<attribute>` in the audited media corpus.
The service-qualified shape is reserved for states such as
`<component>.<logical>.<variant>.SVC_AV_<service>.ZonesActiveIn`. Attributes include `CurrentSongName`, `CurrentArtistName`,
`CurrentAlbumName`, `CurrentArtworkPath`, `CurrentStreamingService`,
`CurrentPauseStatus`, `CurrentElapsedTime`, `CurrentRemainingTime`, `CurrentProgress`,
`CurrentTransportActions`, `SeekDisabled`, `CurrentVolume`, and `IsMuted`.

On the current host, elapsed and remaining time are `MM:SS` strings, progress is an int
0-100, and pause status is a bool. `CurrentArtworkPath` is an opaque artwork key: fetch
it through `session/fileDownload` with `{URI:"avc/<component>/<logical>",
payload:{key:<artwork-key>,type:"nowPlayingArtwork"}}`. The raw binary reply has a
shared transfer header on every message (§5b). Correlate the raw label to the requested
key and detect JPEG or PNG from the complete body. Trace-backed music controls are `PowerOn`, `PowerOff`, `SetVolume` (raw
0-50, mapped to Home Assistant's 0-100 display scale), `Play`,
`Pause`, `SkipUp`, `SkipDown`, and `Seek {ProgressValue:<0-100 percent>}`.
After `PowerOff`, the host can retain all metadata, pause, and progress values; use the
room's exact `ActiveService`/comma-separated `ActiveServices` membership instead.

For Home Assistant topology, canonical zoned-service rows are endpoint projections. The
physical server identity comes from the row's exact `component` relationship to
`ZoneConfigComponents` (preferring its stable `uid`/`componentID`/`internalID`), while
the endpoint zone identity comes from `Rooms.roomID`. The per-room zoned-service `service`
identifier remains the authoritative membership key in `ActiveService`/`ActiveServices`.
These archive relationships, not display names, allow several room endpoints to be
recognized as aliases of one server. The integration uses endpoint `PowerOff` then
`PowerOn` under a per-server lock for exact route replacement; no whole-route replacement
verb has been observed.

Music browsing is a same-URI RPC under
`music/<component>/<logical>/SVC_AV_SAVANTMUSIC/getRoot` and `/browse`. Captured requests
use `{clientType:"iPhone",limit:50,offset:0,requestId,version:1,node,arguments:null}`;
the response carries `{requestId,screenArguments,nodes}` on the same URI. For `/browse`,
preserve the selected node's opaque routing fields, especially its JSON-encoded
`arguments.item`, rather than deriving a request from its title. Nodes with
`actionType:"browsable"` are folders; action-node playback effects are not verified.
Typed search is capture-verified on `/search` with `clientType:"android"`, no outer
identity fields, and `arguments:{filter:"all",searchTerm,services:["plex","tunein",
"amazonmusic","playlists"],uuid}`. An initial `{searchReady:false,nodes:[]}` means wait
for `refreshLMQ` or `refreshLMQ3` containing that UUID, then repeat the same request with a
new `requestId`. Search results are `displayType:"searchList"` nodes. Follow their captured
`query:"browse"|"browseSearch"` on the matching endpoint; a recent-search track submitted
to `/browse` produced matching `CurrentSongName`, `CurrentPauseStatus:false`, and elapsed-time
state. Non-`all` filters and paging beyond `offset:0` remain unsupported.
Browse-node `artworkKey` values use the same `session/fileDownload` wrapper as now-playing art,
with `type:"thumbnailArtwork"`; serve the returned JPEG or PNG through Home Assistant's browse-image
proxy without exposing the opaque artwork key in media IDs.
Home Assistant browse IDs are deterministic opaque hashes of the returned node, so repeated
browse calls for an unchanged node return the same ID. Savant has no observed standard `Stop`
verb; `media_player.media_stop` therefore returns a validation error rather than sending an
invented request.
The captured track selection followed an earlier Music `PowerOn`; selecting a track while its
zone is off is not verified. Wait for the room's nonempty `ActiveService` state after `PowerOn`
before submitting the track node. Provider/browser-session expiry behavior is not captured.

### 5.4 Apple TV endpoints
The current host's config archive declares `SVC_AV_APPLEREMOTEMEDIASERVER` and
`SVC_AV_APPLEREMOTEMEDIASERVERAUDIO` endpoints for Apple TV. Their archive request maps
list `PowerOn`, `PowerOff`, `SetVolume`, `Play`, `Pause`, `Home`, `Menu`, directional OSD
controls, and `StopRepeat`. The native app sends `StopRepeat` immediately after `Play`;
the integration does the same. It exposes only the media-player controls Home Assistant can
represent (power, volume, play/pause). The observed `<room>.ActiveService` value is the
full archive service ID and authoritatively identifies the active Apple TV endpoint; its
empty value means the room is idle. `LastActiveService` retains the previous endpoint. No
Apple TV-specific state or metadata key has been captured, so playback state and volume are
optimistically reflected until `ActiveService` changes.

### 5.5 Global — `global.CurrentTemperature`, `global.LightsAreOn`, `global.SonosGroups`, `Energy.Grid.IsAvailable`.

### 5.6 Scenes
Subscribe to dashboard state `scenesAndFoldersReduced` using `dis/dashboard/register`.
The initial `dis/dashboard/update` is the complete current scene-summary list; summaries
include stable `id` and `name`, so integrations can reconcile scene entities as host scenes
are created or removed. Creation was observed to push a replacement list. A deletion push
has not been observed, so deletion reconciliation relies on a future list update or
reconnection. Activate a saved scene through `dis/dashboard/request` with
`{request:"ApplyScene",requestId:<string>,requestArgs:{id:<scene-id>,version:"2.0"}}`.
The correlated same-URI response reports `success:true,errorCode:0`; relevant device state
updates are the confirmation of the scene's effect. Do not derive activation from summary
`actions` or scene definition (sibling PROTOCOL.md §9.3).

## 5b. Config archive (authoritative device inventory)

The complete room/load/device inventory is **downloaded**, not pushed: `session/fileDownload
{filePath:"uiconfig.tar.gz"}` returns a framed gzip archive (binary WS frames, not msgpack)
whose `serviceImplementation.sqlite` holds the full model. The sibling repo's
`PROTOCOL.md` §13 documents the 38-table/one-view schema and relationships.
Key facts for this integration:

- Relationship columns conventionally reference integer row IDs, but not every table has
  an `id` and the database declares no foreign keys.
- Entity columns are class-specific. Lights/shades/fans/HVAC have `addresses` and
  `stateName`; locks and garages use specialized state fields; several other classes have
  neither. Configuration descriptors do not prove a request was exercised.
- Environmental zones are not physical rooms. Suggest an area only from an unambiguous
  `ZoneRoomMap` association; broad HVAC mappings do not establish ownership.
- `ServiceImplementationZonedService` is the canonical deduplicated AV endpoint view.
  `ServiceImplementationServiceResources` contains transport/path rows.

### 5b.1 Shared binary-transfer framing

After WebSocket reassembly, each transfer message has prefix `01`, flag `01` (data) or
`81` (final), an 8-byte big-endian total body length at bytes 2–9, a 4-byte big-endian
raw-label length at bytes 10–13, then the repeated raw label and this message's body.
Configuration uses label `uiconfig.tar.gz`; media images use the requested artwork key.
Correlate by connection plus exact raw label, require a consistent declaration, exact
accumulated body length, and a final marker. Exact bytes without final or a short final
remain incomplete. Complete media bodies include JPEG and PNG.

## 6. Device control — `service/request`

Each message:

```json
{ "component": "<component name>", "serviceType": "<SVC_...>", "zone": "<room|''>",
  "logicalComponent": "<subcomponent>", "variantID": "<n>", "request": "<verb>",
  "requestArgs": { ... }, "requestId": "<uuid>"? }
```

Observed verbs used by this integration:

| Verb | Scope | args (observed) |
|---|---|---|
| `SetVolume` | AV | `{VolumeValue:<int 0-50 raw>}` |
| `PowerOn` / `PowerOff` | music AV | no args (`SVC_AV_SAVANTMUSIC`) |
| `Play` / `Pause` / `SkipUp` / `SkipDown` | music AV | no args (`SVC_AV_SAVANTMUSIC`) |
| `Seek` | music AV | `{ProgressValue:<int 0-100>}` (`SVC_AV_SAVANTMUSIC`) |
| `__RoomSetBrightness` | lighting | `{BrightnessLevel:<0|100>,useLastDimmerValue:true}`; omits component/logical fields |
| `DimmerSet` | lighting | Standard dimmers omit color fields; color loads use nested `bleColor`; Lutron has its captured backend shape |
| `SetCoolPointTemperature` / `SetHeatPointTemperature` | HVAC | `{ThermostatAddress:"1", CoolPointTemperature|HeatPointTemperature:<float>}` |
| `SetHVACModeAuto` / `SetHVACModeCool` / `SetHVACModeHeat` / `SetHVACModeOff` | HVAC | `{ThermostatAddress:"1"}` |
| `SetFanModeOn` | HVAC | captured one-way setter; not exposed as a reversible Home Assistant fan-mode control |
| `ShadeSet` | shade | native address shape plus string `{ShadeLevel, FadeTime, DelayTime, PresetNumber, SceneNumber}` (`SVC_ENV_SHADE`; sibling PROTOCOL.md §7.5) |
| `RFShadeSet` | Lutron HomeworksQS shade | three state-confirmed native requests; no `variantID`, integer `{Address1, FadeTime:"0", DelayTime:"0", PresetNumber:"0", ShadeLevel}` (sibling PROTOCOL.md §7.5.1) |

HVAC scope is archive-derived: `component`/`logicalComponent` come from the entity's
`stateName`, `serviceType:"SVC_ENV_HVAC"`, and `variantID:"1"`. Native `zone` is either
empty or a room name; preserve an unambiguous configured room scope. Two-address
CoolMaster setter shapes and fan auto/cycle remain uncaptured and disabled.

---

## 7. Open questions (carried over from the sibling repo)

1. **`secretKey` purpose** — issued alongside `hostToken` on local login, but never
   re-sent in the capture; unknown what it signs/encrypts (sibling PROTOCOL.md §13.3).
2. **`hostToken` persistence** — issued fresh per local login; whether it can be cached
   across sessions is unconfirmed (the integration re-logs-in with `{user, password}`
   each reconnect).
3. Full verb list for **fans / door-locks / garage** control is not captured. Shade control is documented in the sibling
   `PROTOCOL.md` §6.1.1 and §7.5: `ShadeLevel` is 0 (closed) through 100 (open),
   `ShadeSet` updates may be asynchronous and controller positions may quantize by ±1.
4. Music non-`all` search filters and page traversal beyond `limit:50, offset:0` remain
   unverified.
5. Registration completion has no universal boundary; some registered keys never return.
6. Component/logical names are host-config-dependent (`<music-component>`,
   `<hvac-component>`, `<hvac-controller>`, `<host-component>`); this integration
   defaults to the observed names (`Music`, `HVAC Controller`, `HVAC_controller`) but
   does not yet re-derive them from the state-key namespace.
7. HVAC archive metadata supplies per-entity scale/range/capability flags on new imports;
   old persisted device records may require reconfiguration to acquire those fields.
