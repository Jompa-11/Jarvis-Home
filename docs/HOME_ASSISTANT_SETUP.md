# Home Assistant and Sonos setup

JARVIS controls Sonos **through Home Assistant**. It does not speak Sonos protocols itself.
Setup does not change any Windows networking or virtualization settings.

## 1. Run Home Assistant (choose one)

| Option | Effort | Windows changes | Sonos works well? |
|---|---|---|---|
| **A. Dedicated small device** (Raspberry Pi 4/5, Home Assistant Green, or an old mini PC) running Home Assistant OS | Low | None | ✅ Yes, recommended |
| B. Virtual machine on this PC (Hyper-V or VirtualBox) running Home Assistant OS | Medium | Enables virtualization features, needs a *bridged* network adapter | ✅ Yes, if bridged |
| C. Docker Desktop on Windows | Medium | Docker + WSL2 | ⚠️ Often not: Sonos discovery needs host networking, which Docker Desktop on Windows does not fully provide |

Option A is the most reliable and keeps your PC unchanged. It also keeps the house running when the PC is off.
Option B changes Windows features, so do it only if you choose to. JARVIS will not set it up automatically.

Install guide: https://www.home-assistant.io/installation/

## 2. Add Sonos to Home Assistant

1. Home Assistant → **Settings → Devices & services**. Sonos is usually discovered automatically.
   Otherwise: **Add integration → Sonos**.
2. Assign each speaker to an **Area** (Kitchen, Living room…). JARVIS uses areas as room names.
3. Favorites (optional): open the Sonos integration → entities → enable **Sonos favorites**
   (`sensor.sonos_favorites`, disabled by default).
4. Announcements (optional): add a text-to-speech engine, e.g. the **Piper** add-on (local, supports Swedish)
   or **Google Translate TTS**. JARVIS uses the first `tts.*` entity it finds, or `homeassistant.tts_entity`
   in `config\jarvis.toml`.

## 3. Connect JARVIS

1. Home Assistant → click your user name → **Security** → **Long-lived access tokens** → *Create token*.
   Consider creating a dedicated Home Assistant user for JARVIS.
2. In `.env`: `HA_TOKEN=<the token>`
3. In `config\jarvis.toml`:
   ```toml
   [homeassistant]
   url = "http://homeassistant.local:8123"   # or http://<ip>:8123
   ```
4. Restart JARVIS, or click **Rescan** in the Systems panel. Home Assistant should show **Online**, and your
   speakers should appear under **Audio zones**.

`python -m jarvis check` shows the connection result without opening the interface.

## What JARVIS may do in Home Assistant

Only these services can be called (`homeassistant.allowed_services`):
`media_player.media_play`, `media_pause`, `media_stop`, `media_next_track`, `media_previous_track`,
`volume_set`, `play_media`. Anything else is refused before a request is sent, even if a model asks for it.

Each action is also checked against the speaker's `supported_features` as reported by Home Assistant.
JARVIS then reads the state back to verify the effect where possible.

## Troubleshooting

| Systems panel shows | Meaning |
|---|---|
| Home Assistant **Not set** | URL or `HA_TOKEN` missing |
| **Auth** | Token rejected: create a new long-lived token |
| **No link** | URL unreachable from this PC (try the IP address instead of `homeassistant.local`) |
| Sonos **None** | Home Assistant is connected but has no Sonos entities |
| Announcement says “does not support announce” | That speaker model or integration does not support overlay announcements |
