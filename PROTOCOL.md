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
  authenticationRequest ---------->   one of two forms:
      {hostToken}                       cached/cloud credential
      {user, password}                  LOCAL LOGIN — host account (§4.1)
  <-- authenticationResponse -------   local:  {authorized, hostToken, secretKey,
                                      startZone}
                                       cloud:  {authorized, permissions{admin,remote,
                                      notifications, blacklists}, configurationHash,
                                      configurationUID}
  state/register [keys...] -------->   subscribe
  dis/<svc>/register ... ---------->
  <-- state/update / dis/<svc>/update  async pushes
  service/request -------------->   device control (verbs)
  ping "E" <-> pong "E" (2s) -      keepalive
```

### 3.1 Local login (`{user, password}`) — cloud-free auth

The `hostToken` is **not** obtained out-of-band: a host-local account authenticates
directly (PROTOCOL.md §4.1/§4.9 of the sibling repo). Observed live:

```
-> session/authenticationRequest  {user:"<LOCAL_USER>", password:"<PASSWORD>"}
<- session/authenticationResponse {authorized:true,
                                   secretKey:"<b64 UUID>",   // purpose unknown
                                   hostToken:"<b64 UUID>",   // fresh, issued per login
                                   startZone:"<room>"}
```

So a headless integration can log in with only local credentials — no `cloudToken` /
`configurationID` required (that session's `devicePresent` carried neither).

## 4. Endpoints used by this integration

| URI | Direction | Purpose |
|---|---|---|
| `session/devicePresent` | client → host | register device |
| `session/authenticationRequest` | client → host | present `hostToken` **or** `{user, password}` |
| `session/authenticationResponse` | host → client | auth result (`authorized`, `hostToken`, `secretKey`, `startZone`) |
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
`RoomCurrentTemperature(num)`, `SleepTimerActive(bool)`, `SleepTimerRemainingTime(num)`.

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

This integration subscribes to `dis/dashboard` (register + `GetAVAutomationScenes`) to
harvest room names, takes `startZone` from the auth response, and additionally derives
rooms from any `<room>.<attr>` state key it sees; each newly discovered room's per-room
keys are then registered.

### 5.3 Audio zones — `Music.Audio Zone N.SVC_AV_SAVANTMUSIC.*`
`ZonesActiveIn`, `CurrentSongName`, `CurrentArtistName`, `CurrentAlbumName`,
`CurrentArtworkPath`, `NowPlayingSource`, `CurrentStreamingService`, `CurrentPauseStatus`,
`CurrentElapsedTime`, `CurrentRemainingTime`, `CurrentProgress`, `TransportSet`, …

### 5.4 Global — `global.CurrentTemperature`, `global.LightsAreOn`, `global.SonosGroups`, `Energy.Grid.IsAvailable`.

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
| `__RoomSetBrightness` | lighting | `{BrightnessLevel:<0|100>, useLastDimmerValue:true}` |
| `DimmerSet` | lighting | `{Address1..6, DimmerLevel, FadeTime, DelayTime, Curve, useLastDimmerValue, bleColor{red,green,blue,white,kelvin}}` (`SVC_ENV_LIGHTING`) |
| `SetCoolPointTemperature` / `SetHeatPointTemperature` | HVAC | `{ThermostatAddress:"1", CoolPointTemperature|HeatPointTemperature:<F°>}` |
| `SetHVACModeAuto` / `SetHVACModeCool` / `SetHVACModeHeat` / `SetHVACModeOff` | HVAC | `{ThermostatAddress:"1"}` |
| `SetFanModeOn` | HVAC | `{ThermostatAddress:"1"}` |

HVAC scope: `component:"HVAC Controller"`, `serviceType:"SVC_ENV_HVAC"`, `variantID:"1"`,
`logicalComponent:"HVAC_controller"`, `zone:""`.

---

## 7. Open questions (carried over from the sibling repo)

1. **`secretKey` purpose** — issued alongside `hostToken` on local login, but never
   re-sent in the capture; unknown what it signs/encrypts (sibling PROTOCOL.md §13.3).
2. **`hostToken` persistence** — issued fresh per local login; whether it can be cached
   across sessions is unconfirmed (the integration re-logs-in with `{user, password}`
   each reconnect).
3. Full verb list for **shades / fans / door-locks / garage** control (expected under
   `service/request`, not yet captured) — shade/fan command verbs are NOT implemented
   here; their state keys are observed but no set-verb is known.
4. **Play / pause / mute / power-off** verbs for media transport are not in the observed
   catalog yet (only `PowerOn`/`SetVolume`); media player entities expose the *known*
   commands only.
5. `state/update` delta vs. snapshot semantics; multi-frame (`fin=0`) gzip streaming.
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