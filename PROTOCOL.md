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
| Payload | **MessagePack** object per WebSocket binary frame |

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

## 2. Message envelope

Every message is a msgpack map:

```json
{ "messages": [ ...1..N payload maps... ], "URI": "<path>", "uid": "<deviceUUID>", "user": "<name>" }
```

`messages` = payload(s); `URI` = endpoint; `uid` = client device UUID (constant per
install); `user` = display name (e.g. `"iPhone"`).

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
- `hostToken`/`secretKey` are base64 of UUID-format strings; re-presenting `hostToken`
  on reconnect skips the password.
- `authenticationResponse.permissions` appears only on the cloud path, not local login.
- **The client `uid` must NOT be UUID-shaped.** Live-verified: when `device.UID` / the
  envelope `uid` contains a 32-hex-character substring (e.g. `uuid4().hex`, or a
  dashed UUID), the host stays silent — it treats the uid as a returning device and
  waits for a `hostToken` instead of answering `deviceRecognized`. Use a clearly
  non-UUID uid (e.g. a `homeassistant-<16-hex>` prefix form); `homeId` is still
  accepted even when freshly generated.

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
| `dis/dashboard/request` | client → host | scene RPC (e.g. `GetAVAutomationScenes`) |
| `dis/dashboard/update` | host → client | scene-list pushes (`scenesAndFoldersReduced`) |

## 5. State model (dotted keys)

### 5.1 HVAC — `HVAC Controller.HVAC_controller.*` (unit index `_1`)
- Points: `ThermostatCurrentTemperature`, `...SetPoint`, `...HeatPoint`, `...CoolPoint`,
  `...Humidity`, `...HumiditySetPoint`.
- Mode: `ThermostatMode`, `ThermostatHVACState`, `ThermostatFanMode`,
  `ThermostatCurrentFanSpeed`.
- Boolean mode/speed flags: `IsCurrentHVACMode{Off,Cool,Heat,Auto,...}`, `IsCurrentFanSpeed*`,
  `IsThermostatCurrentFanMode{Auto,On,Off}`, `IsThermostatHolding`, `ThermostatAwayState`.

### 5.2 Rooms — `<Room>.*` (+ how to derive the room list)

Per-room attributes: `ActiveService`, `ActiveServices`, `LastActiveService`,
`CurrentVolume(int)`, `IsMuted(bool)`, `RelativeVolumeOnly(bool)`, `RoomLightsAreOn(bool)`,
`BrightnessLevel(int 0-100)`, `RoomFansAreOn(bool)`, `RoomShadesAreOpen(bool)`,
`RoomCurrentTemperature(string, e.g. "72")`, `SleepTimerActive(bool)`,
`SleepTimerRemainingTime(num)`.

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
only dimming preserves the host's existing color. Standard dimmers retain their
archive-derived flat color fields. Color-capable loads require their current RGBW payload
with level zero to turn off. They retain RGBW channels at level zero but do not restore
the prior brightness through `useLastDimmerValue`; the integration restores the last
host-reported active RGBW/brightness state (including state changed outside HA). Standard
dimmers retain the observed `useLastDimmerValue:true` power-on behavior.

**State push behaviour.** `state/register` takes a list of single-key maps
(`messages:[{"state":k}, …]`, ~70 keys in one frame) and the host answers with an
immediate per-key snapshot (`state/update` `{state,value}`; a key with no current value
pushes `value: ""`), then pushes `state/update` on change — including to every other
subscribed session (cross-client). A `DimmerSet` is confirmed by pushes of the load's
`CurrentDimmerLevel_N_<addr>` / `CurrentColor_N_<addr>` and the room's
`BrightnessLevel` / `RoomLightsAreOn`. An empty `state/update {messages:[]}` alone is
not a registration error (the native app receives one pre-auth). Earlier observations
that this host "pushes nothing" were caused by the client's msgpack `str8` encoding —
see §1; with legacy encoding the target host delivers the full snapshot. Lighting
entities still apply an optimistic local state on command so the UI reflects changes
instantly, but the pushed state is authoritative once it arrives.

**Deriving the room list (no dedicated "get rooms" endpoint).** Room names are arbitrary
host-defined strings; the full set is inferred from (PROTOCOL.md §6.1 of the sibling):

1. **State-key namespace** — a room is the first dotted segment `R` of any state key
   whose *second* segment is a per-room attribute (list above). No wildcard registration
   exists: subscribe to each `<room>.<attr>` key explicitly.
2. **Scene definitions** — `dis/dashboard` scene objects embed room names:
   `definition.power.rooms` (a map keyed by **every** room — the most complete single
   source), `power.lightingOff`, `av.<zone>.rooms`, `lighting.<host>.rooms`.
3. **`startZone`** on `session/authenticationResponse` names the room the session opens
   in (one room, not the full list).

This integration requests `uiconfig.tar.gz`, registers dashboard state `RecentServices`,
then registers state keys and sends `dcm/request {getMode}`, matching the observed native
startup order. It also uses `GetAVAutomationScenes` to harvest room names, takes
`startZone` from the auth response, and additionally derives rooms from any
`<room>.<attr>` state key it sees; each newly discovered room's per-room keys are then
registered.

### 5.3 Audio zones — `Music.Audio Zone N.SVC_AV_SAVANTMUSIC.*`
`ZonesActiveIn`, `CurrentSongName`, `CurrentArtistName`, `CurrentAlbumName`,
`CurrentArtworkPath`, `NowPlayingSource`, `CurrentStreamingService`, `CurrentPauseStatus`,
`CurrentElapsedTime`, `CurrentRemainingTime`, `CurrentProgress`, `TransportSet`, …

### 5.4 Global — `global.CurrentTemperature`, `global.LightsAreOn`, `global.SonosGroups`, `Energy.Grid.IsAvailable`.

## 5b. Config archive (authoritative device inventory)

The complete room/load/device inventory is **downloaded**, not pushed: `session/fileDownload
{filePath:"uiconfig.tar.gz"}` returns a framed gzip archive (binary WS frames, not msgpack)
whose `serviceImplementation.sqlite` holds the full model. The sibling repo's
`PROTOCOL.md` §13.1 documents the complete 39-table schema and §13.2 the relationships.
Key facts for this integration:

- Every table uses `id INTEGER PRIMARY KEY`; `zoneID`/`roomID` are **integer** references
  to the named table's `id` (no foreign keys — by convention).
- Join chain for a device: `*Entities.zoneID` → `Zones.id` (an `Environmental` zone) →
  `ZoneRoomMap.zoneID` → `roomID` → `Rooms.id` → `name`.
- `*Entities` tables (`LightEntities`, `ShadeEntities`, `FanEntities`, `HVACEntities`,
  `DoorLockEntities`, `GarageEntities`, …) each hold one device class, with `name`,
  `addresses` (comma-separated load addresses, `(null)`-padded), and `stateName` (the
  dotted state key to subscribe to).
- Media/AV endpoints live in the master zoned-service list
  `ServiceImplementationServiceResources` (`serviceType` contains `SVC_AV`), not an
  Entities table.

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
| `SetVolume` | AV | `{VolumeValue:<0-100>}` (component `Music`/`Optical Input`/`RCA Inputs`, `SVC_AV_SAVANTMUSIC`/`SVC_AV_GENERALAUDIO`) |
| `PowerOn` | AV | none (component `Music`, `SVC_AV_SAVANTMUSIC`) |
| `__RoomSetBrightness` | lighting | `{BrightnessLevel:<0|100>}` |
| `DimmerSet` | lighting | `{Address1..6, DimmerLevel, FadeTime, DelayTime, Curve, bleColorRed, bleColorGreen, bleColorBlue, bleColorWhite, kelvin}` (`SVC_ENV_LIGHTING`) |
| `SetCoolPointTemperature` / `SetHeatPointTemperature` | HVAC | `{ThermostatAddress:"1", ThermostatAddress2:"(null)", CoolPointTemperature|HeatPointTemperature:<F°>}` |
| `SetHVACModeAuto` / `SetHVACModeCool` / `SetHVACModeHeat` / `SetHVACModeOff` | HVAC | `{ThermostatAddress:"1", ThermostatAddress2:"(null)"}` |
| `SetFanModeAuto` / `SetFanModeCycle` / `SetFanModeOn` | HVAC | `{ThermostatAddress:"1", ThermostatAddress2:"(null)"}` |
| `ShadeUp` / `ShadeDown` / `ShadeStop` | shade | `{Address1..5}` (`SVC_ENV_SHADE`) |
| `ShadeSet` | shade | native address shape plus string `{ShadeLevel, FadeTime, DelayTime, PresetNumber, SceneNumber}` (`SVC_ENV_SHADE`; sibling PROTOCOL.md §7.5) |

HVAC scope is archive-derived: `component`/`logicalComponent` come from the entity's
`stateName`, `serviceType:"SVC_ENV_HVAC"`, `variantID:"1"`, `zone:""`.

---

## 7. Open questions (carried over from the sibling repo)

1. **`secretKey` purpose** — issued alongside `hostToken` on local login, but never
   re-sent in the capture; unknown what it signs/encrypts (sibling PROTOCOL.md §13.3).
2. **`hostToken` persistence** — issued fresh per local login; whether it can be cached
   across sessions is unconfirmed (the integration re-logs-in with `{user, password}`
   each reconnect).
3. Full verb list for **fans / door-locks / garage** control (expected under
   `service/request`, not yet captured). Shade control is documented in the sibling
   `PROTOCOL.md` §6.1.1 and §7.5: `ShadeLevel` is 0 (closed) through 100 (open),
   `ShadeSet` updates may be asynchronous and controller positions may quantize by ±1.
4. **Play / pause / mute / power-off** verbs for media transport are not in the observed
   catalog yet (only `PowerOn`/`SetVolume`); media player entities expose the *known*
   commands only.
5. ~~`state/update` delta vs snapshot semantics~~ — RESOLVED: registration returns an
   immediate per-key snapshot (empty-string value when idle), then delta-only pushes
   on change (§5.2; sibling PROTOCOL.md §6.6).
6. Scene trigger verb — `dis/dashboard/request` verbs (`CaptureScene`, `UpsertScene`,
   `RemoveScene`, `GetAVAutomationScenes`) are known but the "run scene" verb is not
   yet confirmed.
7. Component/logical names are host-config-dependent (`<music-component>`,
   `<hvac-component>`, `<hvac-controller>`, `<host-component>`); this integration
   defaults to the observed names (`Music`, `HVAC Controller`, `HVAC_controller`) but
   does not yet re-derive them from the state-key namespace.
8. Temperature scale is observable via `SchedulerSettings.TemperatureScale`
   (`"Fahrenheit"`|`"Celsius"`); the integration currently assumes Fahrenheit instead of
   reading it.
