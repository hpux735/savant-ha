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
  devicePresent ------------------>   device{UID,make,app,model,OS,type,name}, homeId,
                                      cloudToken, configurationID, messageFormat:2,
                                      protocolVersion:"4"
  authenticationRequest{hostToken} -> hostToken = base64 start/connection token
  <-- authenticationResponse ------- {authorized, permissions{admin,remote,
                                      notifications, blacklists}, configurationHash,
                                      configurationUID}
  state/register [keys...] --------> subscribe
  <-- state/update {state,value} --- async pushes
  service/request -------------->   device control (verbs)
  ping "E" <-> pong "E" (2s) -      keepalive
```

`authenticationResponse.permissions`: `admin`, `remote`, `notifications` (bool) +
`serviceBlacklist`/`componentBlacklist`/`zoneBlacklist` (lists).

## 4. Endpoints used by this integration

| URI | Direction | Purpose |
|---|---|---|
| `session/devicePresent` | client → host | register device |
| `session/authenticationRequest` | client → host | present `hostToken` |
| `session/authenticationResponse` | host → client | auth result |
| `state/register` / `state/unregister` | client → host | subscribe/unsubscribe state keys |
| `state/update` | host → client | `{state, value}` pushes |
| `service/request` | client → host | **device control** (§6) |
| `dcm/request` | client → host | device-control-manager RPC (e.g. `getMode`) |

## 5. State model (dotted keys)

### 5.1 HVAC — `HVAC Controller.HVAC_controller.*` (unit index `_1`)
- Points: `ThermostatCurrentTemperature`, `...SetPoint`, `...HeatPoint`, `...CoolPoint`,
  `...Humidity`, `...HumiditySetPoint`.
- Mode: `ThermostatMode`, `ThermostatHVACState`, `ThermostatFanMode`,
  `ThermostatCurrentFanSpeed`.
- Boolean mode/speed flags: `IsCurrentHVACMode{Off,Cool,Heat,Auto,...}`, `IsCurrentFanSpeed*`,
  `IsThermostatCurrentFanMode{Auto,On,Off}`, `IsThermostatHolding`, `ThermostatAwayState`.

### 5.2 Rooms — `<Room>.*`
`ActiveService`, `CurrentVolume(int)`, `IsMuted(bool)`, `RoomLightsAreOn(bool)`,
`BrightnessLevel(int 0-100)`, `RoomFansAreOn(bool)`, `RoomShadesAreOpen(bool)`,
`RoomCurrentTemperature(num)`, `SleepTimerActive(bool)`, `SleepTimerRemainingTime(num)`.

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

1. **`hostToken` derivation** — base64 of what? The local-user login path is not yet
   fully decoded; the integration therefore accepts an explicit `hostToken` /
   `cloudToken` / `configurationID` from the user and documents its best-effort default
   (see `const.py`).
2. Full verb list for **shades / fans / door-locks / garage** control (expected under
   `service/request`, not yet captured) — shade/fan command verbs are NOT implemented
   here; their state keys are observed but no set-verb is known.
3. **Play / pause / mute / power-off** verbs for media transport are not in the observed
   catalog yet (only `PowerOn`/`SetVolume`); media player entities expose the *known*
   commands only.
4. `state/update` delta vs. snapshot semantics; multi-frame (`fin=0`) gzip streaming.
5. Scene trigger verb — `dis/dashboard/request` verbs (`CaptureScene`, `UpsertScene`,
   `RemoveScene`, `GetAVAutomationScenes`) are known but the "run scene" verb is not
   yet confirmed.