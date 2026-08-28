# AGENTS.md

## Project intent

This repository holds a **Home Assistant integration** (HACS-compatible) that controls a
**Savant host** on the local network. It is the intended end state of the sibling
[`savant-app-re`](https://git.housedillon.com/wdillon/savant-app-re) project, which
reconstructed the Savant **app ↔ host** protocol by **black-box reverse engineering** —
passive packet capture, mDNS enumeration, and a live MITM — with **no** vendor
documentation, firmware, or source code.

The integration therefore targets the **reconstructed protocol**, not any vendor SDK or
spec:

- **Transport:** WSS (TLS with any certificate — the app pins nothing) carrying a
  WebSocket with subprotocol **`rpm-protocol`**.
- **Serialization:** **MessagePack** per WebSocket binary frame; host→client frames are
  gzip-compressed.
- **Discovery:** a msgpack query broadcast to UDP **9101** (`_control_.ws`) /
  **9103** (`_presence_.ws`), plus mDNS types `_soapi_sdo._tcp`, `_terA_*_asy._tcp`.
- **Session:** `session/devicePresent` → `session/authenticationRequest` /
  `session/authenticationResponse` → `state/register` → async `state/update` pushes.
- **Control:** `service/request` with observed verbs (`SetVolume`, `DimmerSet`,
  `SetHVACMode*`, `Set*PointTemperature`, `PowerOn`, …).

The canonical source of protocol truth is the sibling project's
[`PROTOCOL.md`](https://git.housedillon.com/wdillon/savant-app-re). This repo keeps a
condensed copy in `PROTOCOL.md` (§ "Provenance") so a fresh agent can resume without
re-reading the whole sibling history.

The canonical, chronological record of prompts for **this** repo is
[`savant_ha_prompts.csv`](savant_ha_prompts.csv). Treat it as the source of truth and
keep it up to date.

## Auditing rule: every change must be paired with a prompt

Because this work is produced by prompting an LLM, provenance is essential.

1. **Pair every change with its prompt.** The user prompt that drove a change must be
   quoted (verbatim) in the commit message, the PR description, or an entry in
   `savant_ha_prompts.csv`. An untraceable change is not acceptable as-is.
2. **Flag unprompted code.** Any code that cannot be paired with a prompt must be
   flagged in source with `# NOTE: unprompted change — needs review` and recorded in the
   "Flagged changes" section of `savant_ha_prompts.csv` (or a `JOURNAL.md` if one is
   later added).
3. **Audit on review.** Verify each file/hunk maps to a documented prompt; reject or
   flag orphaned changes.

## Prompt ground truth: `savant_ha_prompts.csv`

The user's actual prompts are the ground truth for provenance. They are exported from the
opencode database into [`savant_ha_prompts.csv`](savant_ha_prompts.csv) (columns
`timestamp_epoch_ms,timestamp_iso_utc,session_id,session_title,prompt`).

1. **Append every user prompt.** After each turn, append the user's verbatim prompt as a
   new CSV row (RFC 4180 quoting; double any internal quotes). This file survives model
   inconsistency and context compression — cite it; never reconstruct prompts from memory.
2. **Never fabricate a prompt.** If a change can't be traced to a real row in
   `savant_ha_prompts.csv`, flag it as unprompted (per the auditing rule) — do **not**
   invent a plausible prompt.

> Note: the sibling repo was burned once by a fabricated prompt (the "UDP 9101/9103
> discovery" commit initially claimed prompts that did not exist in its CSV). Do not
> repeat that here.

## Protocol provenance (the "no proprietary knowledge" rule)

The protocol knowledge used here is **reverse-engineered from live black-box
observation**, documented in the sibling repo under the same constraints. The following
are **allowed** as sources:

- The sibling `savant-app-re` repo — specifically `PROTOCOL.md`, `REVERSE_ENGINEERING.md`,
  and `savantre/schema.py` (the distilled, observed protocol surface).
- Observation of the host's own on-the-wire behavior, as recorded there.

The following are **forbidden** and must never be introduced into this repo:

- Vendor documentation, specifications, SDKs, or any material distributed under a
  proprietary license.
- Vendor firmware or **source code** (Savant is not open source).
- Any value or fact whose only basis is a guess presented as fact. Assumptions must be
  flagged in code (`# ASSUMPTION: …`) and listed in `PROTOCOL.md` § "Open questions".

Every protocol constant in the code should carry a comment pointing at the
`PROTOCOL.md` section (or sibling `PROTOCOL.md`) that documents where it was observed.

## PII redaction

No identifying values may be committed: raw host IDs / `homeId`s, device UIDs, hostnames,
MACs, IPs, cloud tokens/credentials, project/network names, or the operator's name.
Replace them with the bracketed placeholders used consistently across the sibling docs:

- `<HOST_IP>` `<HOST_MAC>` `<HOST_MDNS>` `<HOST_ID>` `<HOST_LAN>` `<HOST_LAN_BROADCAST>`
- `<PHONE_UDID>` `<PHONE_IP>` `<PHONE_MAC>` `<PHONE_NAME>` `<DEVICE_UID>`
- `<HOME_ID>` `<CLOUD_TOKEN>` `<CONFIGURATION_ID>` `<PROJECT_NAME>` `<PROJECT_TOKEN>`
- `<PIVOT_IP>` `<KALI_IP>` `<LOCAL_IP>` `<OTHER_HOST_IP>` `<SSH_USER>`
- `<HA_SWITCH>` `<HA_SWITCH_ENTITY>` `<HA_AREA>` `<WIFI_SSID>` `<LOCAL_USER>` `<PASSWORD>`

Redact both the files **and** the prompts appended to `savant_ha_prompts.csv` before
committing. Live credentials (e.g. the user's actual `cloudToken`/`hostToken`) must never
be committed anywhere.

## Conventions for future agents

- Read this file, `PROTOCOL.md`, and `savant_ha_prompts.csv` before changing anything.
- Do not invent device behavior; verify against the live host (`<HOST_IP>`) or
  document assumptions explicitly (open questions live in `PROTOCOL.md` § "Open
  questions").
- Keep the prompt CSV updated whenever a prompt meaningfully changes the code.
- Commit and push at the end of each prompt, with the prompt quoted in the commit message.
- Keep temporary/scratch files under `/tmp/` or the project directory — **do not** use
  `/var/...` paths (per the user's explicit instruction).