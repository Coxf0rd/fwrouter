# StealthSurf authenticated read-only API audit — 2026-10-01

Evidence: 33 sequential authenticated GET requests, observed 2026-09-30 22:49–22:51 UTC (2026-10-01 05:49–05:51 Asia/Krasnoyarsk). No provider mutation, FWRouter runtime/DB/UI/config change, restart, deployment or commit. Only sanitized audit artifacts/fixtures were created. Existing uncommitted secrets-preparation files were preserved.

**Observed directly from API** means a captured response. **Derived/inferred** means an interpretation explicitly separated from that response. **Documented only** never establishes account capability. The general `.env` was read locally; no API key or Authorization header was persisted.

## Executive summary

**Observed directly from API:** one ordinary config, ID 309293, location 26/NO, server 1456, protocol hysteria2, is_online=true. Paid-options and cloud-servers returned empty arrays. Locations: 28 total, 22 active. Discovery: 22 location/protocol combinations, 80 distinct servers, maximum five per response, two empty responses. Current server is present with seven available slots. All HTTP/envelope statuses were successful.

**Derived/inferred:** discovery is a bounded candidate sample, not an authoritative full member registry; local connectivity and runtime support are not proven by provider status.

**Documented only:** other primary VPN protocol identifiers and mutation semantics remain outside this authenticated GET evidence.

## Authenticated endpoints queried

**Observed directly from API:**

| GET endpoint | Calls | HTTP / envelope |
| --- | --- | --- |
| /configs | 3 | 200 / status=true, statusCode=200 |
| /locations | 1 | 200 / status=true, statusCode=200 |
| /paid-options | 1 | 200 / status=true, statusCode=200 |
| /cloud-servers | 1 | 200 / status=true, statusCode=200 |
| /configs/309293/serverStats | 2 | 200 / status=true, statusCode=200 |
| /configs/309293/subconfig/protocols | 2 | 200 / status=true, statusCode=200 |
| /configs/309293/subconfig | 1 | 200 / status=true, statusCode=200 |
| /configs/available-servers | 22 | 200 / status=true, statusCode=200 |

All discovery requests used protocol=hysteria2 and an actually returned active location ID. Requests were sequential with at least 1.2 seconds between start times, no burst, no cache-busting and no retries. No undocumented per-config detail route was probed: the canonical documented config read is GET /configs. The list was fetched three times; serverStats and subconfig capabilities twice. No paid/cloud child reads were needed because their collections were empty.

**Derived/inferred:** the current API snapshot is finite; no stronger pagination guarantee follows from one returned record.

**Documented only:** the endpoints were chosen from official documentation; their returned values below come exclusively from authenticated responses.

## Actual configs

**Observed directly from API:**

```json
{
  "status": true,
  "statusCode": 200,
  "data": [
    {
      "id": 309293,
      "server_id": 1456,
      "connection_url": "<redacted>",
      "location_id": 26,
      "protocol": "hysteria2",
      "title": null,
      "is_online": true,
      "expires_at": 1791361578,
      "created_at": 1790756778,
      "auto_renewal": false,
      "auto_renewal_days": null,
      "ipv6": "2a14:7c0:1002:1325::"
    }
  ]
}
```

All fields shown were present. title and auto_renewal_days were null; ipv6 was a non-null string. Absent: is_extended_settings_enabled, xray_config, awg_config, flags. No config-kind discriminator was returned. Ordinary config kind follows the /configs endpoint namespace, not an invented payload field. /paid-options and /cloud-servers each returned {status:true,statusCode:200,data:[]}. /configs/309293/subconfig returned data:null.

**Derived/inferred:** no active paid-option/cloud resource was available for child inspection. Empty collections do not establish that the account has never owned those resources.

**Documented only:** optional runtime material may exist for other protocols/settings; this snapshot does not show it.

## Actual locations

**Observed directly from API:** every location had exactly id, code, title, description, caption, is_active, ping_ip, tags. Full captions/descriptions and original response order are preserved in observed_responses.json.

| ID | Code | Title | Active | ping_ip | tags |
| --- | --- | --- | --- | --- | --- |
| 18 | RB | Умная локация | True | ping-ru.stealthsurf.network:2053 | [] |
| 22 | RB | Умная локация (Восток) | False | null | [] |
| 2 | NL | Нидерланды | True | ping-nl.stealthsurf.network:2053 | [] |
| 29 | RU | Россия (игровая) | False | ping-ru.stealthsurf.network:2053 | [] |
| 4 | DE | Германия | True | ping-nl.stealthsurf.network:2053 | [] |
| 1 | FI | Финляндия | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 3 | US | США | True | ping-us.stealthsurf.network:2053 | [] |
| 28 | AE | ОАЭ | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 25 | LT | Литва | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 5 | UK | Великобритания | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 7 | SE | Швеция | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 26 | NO | Норвегия | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 6 | FR | Франция | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 8 | MD | Молдова | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 23 | CH | Швейцария | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 9 | PL | Польша | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 10 | TR | Турция | True | ping-tr.stealthsurf.network:2053 | [] |
| 11 | BR | Бразилия | False | ping-br.stealthsurf.network:2053 | [] |
| 12 | RU | Россия | True | ping-ru.stealthsurf.network:2053 | [] |
| 13 | JP | Япония | True | ping-jp.stealthsurf.network:2053 | [] |
| 14 | HK | Гонконг | True | ping-hk.stealthsurf.network:2053 | [] |
| 15 | CA | Канада | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 16 | IT | Италия | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 24 | IE | Ирландия | True | ping-nl.stealthsurf.network:2053 | ["virtual"] |
| 17 | KZ | Казахстан | True | ping-kz.stealthsurf.network:2053 | [] |
| 19 | KR | Южная Корея | False | ping-kr.stealthsurf.network:2053 | [] |
| 20 | AU | Австралия | False | ping-au.stealthsurf.network:2053 | [] |
| 21 | AL | Албания | False | null | [] |

No duplicate IDs. Duplicate codes: RB = IDs 18/22, RU = IDs 29/12. Current config location: 26 (NO/Norway). Inactive IDs: 22,29,11,19,20,21. Description null in 24/28; caption null in 9/28; ping_ip null in 2/28; no fields missing. Tags observed: empty array or ["virtual"]. No emoji or has_servers field.

**Derived/inferred:** use location ID as identity; country code and ping target cannot identify a location uniquely. Several locations share the same ping hostname; a location ping target is not a member endpoint.

**Documented only:** locations are described as cached; cache backend operation was not observed.

## Actual protocols observed

**Observed directly from API:**

- Primary config: hysteria2. Its redacted connection URL has scheme hysteria2, userinfo, an sni query field and a fragment.
- Subconfig capability response: {"protocols":["http","socks5"]}. This is a proxy-subconfig capability, not the primary config protocol set.
- Subconfig instance: null.
- Discovery requests used the observed current protocol hysteria2; discovery responses contain no protocol field.
- Locations contain no protocol capability field.

**Derived/inferred:** the primary protocol capability intersection cannot be inferred from HTTP/SOCKS5 subconfig capabilities or successful Hysteria2 discovery.

**Documented only:** primary identifiers not observed as account response values: vless, trojan, trojan-2901, vless-2410, shadowsocks-2022, wg, amnezia-wg-2. See [configs](https://docs.stealthsurf.net/api/methods/configs). No mutation was used to materialize those formats.

## Actual available-server responses

**Observed directly from API:** all returned objects had exactly id:integer, ip:string, available_slots:integer. Each list below retains response order. Full individual IP values follow; no count/availability/status field was added.

| Location | Code | Count | Ordered server IDs (slots) |
| --- | --- | --- | --- |
| 18 | RB | 2 | 1026 (1), 1385 (1) |
| 2 | NL | 5 | 2495 (11), 2502 (7), 1746 (3), 2446 (1), 2492 (1) |
| 4 | DE | 5 | 2503 (23), 1678 (3), 2307 (1), 2448 (1), 1660 (2) |
| 1 | FI | 5 | 1903 (12), 2314 (9), 2397 (1), 2070 (12), 1489 (11) |
| 3 | US | 3 | 2329 (2), 1288 (1), 2394 (2) |
| 28 | AE | 5 | 1919 (13), 2315 (11), 2426 (11), 1856 (11), 1761 (10) |
| 25 | LT | 5 | 1511 (2), 1760 (2), 1517 (2), 2385 (2), 2404 (2) |
| 5 | UK | 5 | 1679 (10), 2305 (10), 2399 (9), 1951 (10), 1857 (8) |
| 7 | SE | 5 | 1500 (12), 2388 (6), 2401 (6), 1507 (12), 2076 (12) |
| 26 | NO | 5 | 2310 (8), 1456 (7), 2400 (6), 1683 (7), 1918 (7) |
| 6 | FR | 5 | 1855 (8), 1914 (7), 2403 (5), 1937 (7), 1838 (6) |
| 8 | MD | 5 | 1939 (7), 2304 (4), 1469 (6), 1514 (6), 1501 (5) |
| 23 | CH | 5 | 2077 (11), 1723 (6), 1453 (10), 1940 (10), 1866 (8) |
| 9 | PL | 5 | 1510 (5), 2402 (5), 2389 (4), 1515 (4), 1916 (4) |
| 10 | TR | 2 | 622 (1), 2074 (1) |
| 12 | RU | 1 | 2272 (1) |
| 13 | JP | 0 | [] |
| 14 | HK | 1 | 573 (11) |
| 15 | CA | 1 | 2380 (1) |
| 16 | IT | 5 | 1896 (21), 2391 (20), 2405 (20), 1734 (20), 1455 (19) |
| 24 | IE | 5 | 1897 (18), 2406 (17), 2392 (11), 1563 (13), 1457 (9) |
| 17 | KZ | 0 | [] |

| Location | Position | Server ID | IP | available_slots |
| --- | --- | --- | --- | --- |
| 18 | 1 | 1026 | 109.248.169.197 | 1 |
| 18 | 2 | 1385 | 144.48.11.80 | 1 |
| 2 | 1 | 2495 | 94.249.195.253 | 11 |
| 2 | 2 | 2502 | 94.249.177.20 | 7 |
| 2 | 3 | 1746 | 217.69.167.98 | 3 |
| 2 | 4 | 2446 | 5.231.119.65 | 1 |
| 2 | 5 | 2492 | 94.249.187.193 | 1 |
| 4 | 1 | 2503 | 94.249.177.21 | 23 |
| 4 | 2 | 1678 | 217.69.167.34 | 3 |
| 4 | 3 | 2307 | 94.249.198.185 | 1 |
| 4 | 4 | 2448 | 94.249.186.128 | 1 |
| 4 | 5 | 1660 | 217.69.167.38 | 2 |
| 1 | 1 | 1903 | 217.69.167.143 | 12 |
| 1 | 2 | 2314 | 94.249.198.172 | 9 |
| 1 | 3 | 2397 | 94.249.184.34 | 1 |
| 1 | 4 | 2070 | 217.69.167.137 | 12 |
| 1 | 5 | 1489 | 217.69.167.168 | 11 |
| 3 | 1 | 2329 | 23.26.4.46 | 2 |
| 3 | 2 | 1288 | 178.130.47.207 | 1 |
| 3 | 3 | 2394 | 23.26.4.183 | 2 |
| 28 | 1 | 1919 | 217.69.167.249 | 13 |
| 28 | 2 | 2315 | 94.249.198.195 | 11 |
| 28 | 3 | 2426 | 94.249.188.202 | 11 |
| 28 | 4 | 1856 | 217.69.167.250 | 11 |
| 28 | 5 | 1761 | 217.69.167.251 | 10 |
| 25 | 1 | 1511 | 217.69.167.238 | 2 |
| 25 | 2 | 1760 | 94.249.198.104 | 2 |
| 25 | 3 | 1517 | 217.69.167.237 | 2 |
| 25 | 4 | 2385 | 94.249.198.105 | 2 |
| 25 | 5 | 2404 | 94.249.198.203 | 2 |
| 5 | 1 | 1679 | 217.69.167.183 | 10 |
| 5 | 2 | 2305 | 94.249.198.187 | 10 |
| 5 | 3 | 2399 | 94.249.188.22 | 9 |
| 5 | 4 | 1951 | 217.69.167.179 | 10 |
| 5 | 5 | 1857 | 217.69.167.182 | 8 |
| 7 | 1 | 1500 | 217.69.167.197 | 12 |
| 7 | 2 | 2388 | 94.249.198.136 | 6 |
| 7 | 3 | 2401 | 94.249.188.26 | 6 |
| 7 | 4 | 1507 | 217.69.167.196 | 12 |
| 7 | 5 | 2076 | 217.69.167.192 | 12 |
| 26 | 1 | 2310 | 94.249.198.194 | 8 |
| 26 | 2 | 1456 | 217.69.167.246 | 7 |
| 26 | 3 | 2400 | 94.249.188.24 | 6 |
| 26 | 4 | 1683 | 94.249.198.106 | 7 |
| 26 | 5 | 1918 | 217.69.167.242 | 7 |
| 6 | 1 | 1855 | 94.249.198.86 | 8 |
| 6 | 2 | 1914 | 217.69.167.187 | 7 |
| 6 | 3 | 2403 | 94.249.188.30 | 5 |
| 6 | 4 | 1937 | 217.69.167.186 | 7 |
| 6 | 5 | 1838 | 217.69.167.190 | 6 |
| 8 | 1 | 1939 | 217.69.167.199 | 7 |
| 8 | 2 | 2304 | 94.249.198.190 | 4 |
| 8 | 3 | 1469 | 217.69.167.204 | 6 |
| 8 | 4 | 1514 | 217.69.167.201 | 6 |
| 8 | 5 | 1501 | 217.69.167.203 | 5 |
| 23 | 1 | 2077 | 217.69.167.222 | 11 |
| 23 | 2 | 1723 | 94.249.198.135 | 6 |
| 23 | 3 | 1453 | 217.69.167.228 | 10 |
| 23 | 4 | 1940 | 217.69.167.224 | 10 |
| 23 | 5 | 1866 | 217.69.167.226 | 8 |
| 9 | 1 | 1510 | 217.69.167.213 | 5 |
| 9 | 2 | 2402 | 94.249.188.28 | 5 |
| 9 | 3 | 2389 | 94.249.198.137 | 4 |
| 9 | 4 | 1515 | 217.69.167.212 | 4 |
| 9 | 5 | 1916 | 217.69.167.206 | 4 |
| 10 | 1 | 622 | 83.217.9.138 | 1 |
| 10 | 2 | 2074 | 83.217.9.140 | 1 |
| 12 | 1 | 2272 | 109.248.169.10 | 1 |
| 14 | 1 | 573 | 62.60.232.111 | 11 |
| 15 | 1 | 2380 | 94.249.198.138 | 1 |
| 16 | 1 | 1896 | 217.69.167.217 | 21 |
| 16 | 2 | 2391 | 94.249.198.139 | 20 |
| 16 | 3 | 2405 | 94.249.188.32 | 20 |
| 16 | 4 | 1734 | 217.69.167.219 | 20 |
| 16 | 5 | 1455 | 217.69.167.221 | 19 |
| 24 | 1 | 1897 | 217.69.167.229 | 18 |
| 24 | 2 | 2406 | 94.249.188.34 | 17 |
| 24 | 3 | 2392 | 94.249.198.103 | 11 |
| 24 | 4 | 1563 | 217.69.167.231 | 13 |
| 24 | 5 | 1457 | 94.249.198.102 | 9 |

80 returned records, 80 unique IDs/IPs; no repeated ID within or across queried combinations. Slots range 1–23; zero/negative slots and busy/down fields were not observed. Empty responses: JP/13 and KZ/17, despite is_active=true. Maximum observed length=5. Order is not consistently descending by slots; e.g. NO [8,7,6,7,7], DE [23,3,1,1,2].

**Derived/inferred:** do not treat list order as FWRouter ranking. Empty discovery does not prove a location outage or a current-member failure. Cross-protocol identity overlap was not tested because only one primary protocol was observed.

**Documented only:** discovery is a public-server sample capped at five, with subnet diversity and a 10-second cache; not a full inventory. [Official discovery docs](https://docs.stealthsurf.net/api/methods/configs).

## Actual current-server status

**Observed directly from API:**

| Field | Actual value/type |
| --- | --- |
| host | "217-69-167-246.stealthsurf.network" |
| status | "up" |
| uptime_days | "22" |
| cpu_model | "Intel(R) Xeon(R) CPU E5-2697 v4 @ 2.30GHz" |

stats: array of 76 objects, each with date:string, cpu:number, ram:number, up:number, down:number; integer and fractional numeric values both occurred. First sample: {"date": "2026-09-30T10:10", "cpu": 16.99, "ram": 11.11, "up": 12.45, "down": 12.3}; last sample: {"date": "2026-09-30T22:40", "cpu": 5.04, "ram": 11.67, "up": 4.93, "down": 5.2}. No server_id, location_id, protocol, latency, RTT, connection count, slot count, capacity, explicit metric units/timezone or fetched_at/cache timestamp appeared in data. No null/error status response was observed. uptime_days="22" is a string. The historical stats dates contain T and hours/minutes.

**Derived/inferred:** status=up is provider evidence only; local client connectivity/Health was not probed. The latest sample predates the GET, so it is not a synchronous runtime probe.

**Documented only:** units and server metric cache semantics must not be assigned to unnamed numeric fields from this snapshot alone.

## Current vs available comparison

**Observed directly from API:** /configs says server_id=1456, location_id=26. Discovery for location=26/protocol=hysteria2 returned server 1456 at position 2, ip=217.69.167.246, slots=7. serverStats returned host=217-69-167-246.stealthsurf.network and status=up, but no server ID.

**Derived/inferred:** the hostname embeds the matching IP text, suggesting the same endpoint; no DNS resolution or independent identity readback was performed. No current member with slots=0 or missing current member was observed. The documented response cap means missing members in future responses cannot automatically be pruned or declared dead; that scenario was not exercised.

**Documented only:** list capping is documentation evidence, not proof that an omitted sixth member exists in this account snapshot.

## Actual response schemas

**Observed directly from API:** every response envelope was {status:boolean,statusCode:integer,data:...}; no pagination metadata, request ID or error object appeared.

| Response | Actual data shape |
| --- | --- |
| configs | array[1]; id/server_id/location_id/expires_at/created_at:integer; connection_url/protocol/ipv6:string; is_online/auto_renewal:boolean; title/auto_renewal_days:null |
| locations | array[28]; id:integer; code/title:string; description/caption/ping_ip:string\|null; is_active:boolean; tags:array[string] |
| available-servers | array[0..5]; id:integer, ip:string, available_slots:integer |
| serverStats | object; host/status/uptime_days/cpu_model:string; stats:array[76] with date:string, cpu/ram/up/down:number |
| subconfig/protocols | object; protocols:array[string] |
| subconfig | null |
| paid-options/cloud-servers | empty arrays |

The full recursively generated value-type schema is in observed_responses.json and fixture metadata.json. Null and missing are preserved separately. A field observed as null once does not establish its entire allowed type union; e.g. non-null title/auto_renewal_days were not observed.

**Derived/inferred:** optionality across other account/protocol variants remains unknown; do not convert this single-record snapshot into a required universal schema.

**Documented only:** advertised optional fields stay optional, and unobserved variants require further fixtures.

## Sensitive-field structural inventory

**Observed directly from API:**

| Field/material | Presence | Type/shape |
| --- | --- | --- |
| connection_url | present | string; hysteria2 scheme; userinfo present; query name sni; fragment present; complete value <redacted> |
| xray_config | missing | not observed |
| awg_config / wg_config / wireguard_config | missing | not observed |
| private/public keys, certificates, UUID, auth/password fields | missing as standalone JSON fields | credential-bearing material is contained in connection_url; no credential value printed |
| HTTP/SOCKS5 subconfig connection_url | no instance | subconfig data=null |
| ipv6 | present | non-null string; fictionalized in fixtures |

No nested runtime configuration JSON was returned. URL credential content, query values and fragment were not persisted. The protocol-specific credential meaning and raw password format were not inferred from URL userinfo.

**Derived/inferred:** Hysteria2 URI parser coverage is required for this actual config; Xray/AWG material cannot be implemented from these account fixtures alone. Redacted response envelopes are structural fixtures, not directly usable runtime credentials.

**Documented only:** other protocol material may be returned under different settings.

## Rate-limit/cache/header observations

**Observed directly from API:** all responses carried server=ddos-guard, Date, Content-Type=application/json; charset=utf-8, Vary=Origin, ETag and X-RateLimit-Limit/Remaining/Reset.

| Endpoint family | Limit | Remaining observed | Reset observed |
| --- | --- | --- | --- |
| configs / locations / paid / cloud / subconfig | 60 | 58–59 | 25 or 60 |
| available-servers | 5 | 0–4 | 1–5 |
| serverStats | 3 | 2 | 5 |

No Cache-Control, Age, Expires, Last-Modified, Retry-After, CF-Cache-Status or X-Cache header was observed. Three config reads had the same ETag and the same value-type schema; the safely compared non-secret returned config values were stable. 401/403/429/5xx and error envelopes were not observed or provoked. API response metadata does not establish cache-hit versus miss or TTL.

**Derived/inferred:** Reset looks like a relative countdown, not a Unix timestamp; honor this as an adapter compatibility finding without assuming every deployment behaves identically. ETag equality alone does not prove cache use.

**Documented only:** general docs describe Reset as Unix timestamp and list cache/rate limits. [Official API docs](https://docs.stealthsurf.net/api).

## Sanitized fixtures created

**Observed directly from API:** backend/tests/fixtures/stealthsurf_api/2026-10-01 contains 33 numbered sanitized response files, metadata.json, sensitive_structure.json and README.md. Numeric entity IDs and endpoint IDs are consistently fictionalized; literal IPs use documentation ranges; the status host uses .example.invalid; ETags are redacted. No original mapping is stored. Credentials-bearing URL is <redacted>. Earlier conservative snapshots redacted uptime/protocol-list/IPv6 strings; final repeated reads retain these non-secret values before fixture IP replacement. Exact type evidence remains in metadata.

Raw means original JSON envelope/field structure, not byte-for-byte whitespace preservation. Unsanitized responses existed only in process memory. Secret scans confirmed the configured key, API-key syntax and credential-bearing protocol URLs are absent from generated files. JSON/schema/ID-reference checks passed. No production tests or runtime apply were run.

**Derived/inferred:** future tests can use empty discovery/null subconfig/discovery ordering fixtures directly; executable URI fixtures need synthetic credentials, not recovered real secrets.

**Documented only:** none.

## Docs vs actual differences

| Area | Docs say | Actual API returned | Difference |
| --- | --- | --- | --- |
| Config schema | Optional extended/runtime fields | Hysteria2 ordinary config; extended flag/Xray/AWG/flags absent; title and renewal-days null | Optional variants unobserved, not a contradiction |
| Protocols | Multiple primary VPN identifiers | hysteria2 primary; http/socks5 subconfig capabilities | No authenticated primary capability list observed |
| Locations | ping_ip IP/null; description/caption strings | hostname:2053 or null; description/caption also null | Accept hostname:port and nullable text |
| Discovery members | Up to five; descending slots | 0–5, id/ip/slots only; ten lists not descending | Preserve order as evidence; rank with existing selector |
| available_slots | Free capacity | integers 1–23 only | Zero/busy semantics not observed |
| Status | uptime_days number; stats date YYYY-MM-DD | uptime_days string "22"; stats date YYYY-MM-DDTHH:mm | Normalize observed string/date shapes |
| Optional resources | Separate paid/cloud schemas | Both lists empty; subconfig null | Child schemas not authenticated here |
| Cache/rate limits | Cached responses; Reset Unix timestamp | No explicit cache headers; Reset 1–60 | TTL unverified; Reset differs |
| Error envelope | Documented codes/status variants | No error responses | Remain unobserved; do not invent fixtures |

Documentation sources: [configs](https://docs.stealthsurf.net/api/methods/configs), [locations](https://docs.stealthsurf.net/api/methods/locations), [paid options](https://docs.stealthsurf.net/api/methods/paid-options), [cloud servers](https://docs.stealthsurf.net/api/methods/cloud-servers), [API headers](https://docs.stealthsurf.net/api). Actual responses take precedence; successful sampling cannot disprove support for unobserved variants.

## Implications for Provider Adapter

**Observed directly from API:** ordinary config 309293 provides config/server/location/protocol binding; discovery provides capacity samples; status provides cached metrics without member ID.

**Derived/inferred / recommended:** keep config kind/ID distinct from member/server ID and location; preserve current member separately from bounded discovery; use discovery members with positive slots as available evidence only, with freshness, not automatic local Health; never rank by response order; support nullable fields and hostname:port ping targets. Map available_slots/status evidence separately from local latency/connectivity. Do not use subconfig protocols as the main subscription protocol capability set. API error handling still needs synthetic contract tests, explicitly labeled synthetic. No adapter implementation was performed.

**Documented only:** switch/protocol mutation request semantics were not verified by mutation and remain documentation-only.

## Implications for Protocol Adapters

**Observed directly from API:** the current primary format is a credential-bearing hysteria2 URI; no Xray/AWG JSON material.

**Derived/inferred / recommended:** the next protocol adapter must support this URI format and then pass pinned-runtime validation; provider support alone proves no FWRouter/Mihomo/Xray capability. Test missing optional extended-config fields and IPv6. HTTP/SOCKS5 capability discovery is separate from the primary protocol import. Additional real protocol format fixtures require legitimate existing configs or later authorized user protocol changes; do not invent them from docs.

**Documented only:** other main protocol identifiers do not become supported merely by appearing in docs.

## Remaining unknowns

**Observed directly from API:** no errors, zero slots, private member flags, busy state, missing current member or paid/cloud child records were returned.

**Derived/inferred / limits:** no provider switch/protocol change was attempted; accepted-switch/readback/fallback semantics remain unmeasured. No exhaustive inventory, cross-protocol shared identity, distinct primary capability endpoint, runtime parse/validation, local latency or connectivity was established. No uptime normalization implementation was added. API cache TTL and error/429 envelopes remain unverified. Non-null title/renewal-days and extended Xray/AWG material were not sampled. Discovery current presence is point-in-time, not a future guarantee.

**Documented only:** provider mutation/caching/error promises require later explicitly authorized verification or documented synthetic tests. Existing roadmap/specs were not modified.
