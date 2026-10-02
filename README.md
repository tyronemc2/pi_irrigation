# Greenhouse irrigation

Automatic watering for the Pringle Bay greenhouse: hydroponics and the veggie bed,
fed from the 250 L tank by one shared pump. A phone dashboard (installable as an app on
Android and iPhone) shows the tank, soil moisture and what's running, and lets you water,
schedule and pause.

```
  Raspberry Pi 5 (garage, Ethernet)          ESP32 box (greenhouse, WiFi)
  ┌──────────────────────────────┐   WiFi   ┌──────────────────────────────┐
  │ Dashboard / phone app        │ ───────▶ │ Valve A  hydroponics (GPIO25)│
  │ Schedules and hydro cycles   │   HTTP   │ Valve B  veggie bed (GPIO26) │
  │ Moisture skip, tank lockout  │ ◀─────── │ 3 soil probes, water temp    │
  │ Flow, leak and fault alerts  │          │ LOW/HIGH floats, flow sensor │
  │ ntfy phone notifications     │          │ Own safety timers            │
  └──────────────────────────────┘          └──────────────────────────────┘
```

The ESP32 does the switching and sensing. The Pi decides when to water. Every valve run
has a timer on the ESP32 itself, so if the Pi crashes or WiFi drops, water still stops on
time. Normally-closed valves also close if power fails.

## What it does

- **Hydroponics** runs in cycles, by default 15 minutes at the top of every hour from 06:00 to 18:00.
- **Veggie bed** waters once a day, by default 10 minutes at 06:30, and skips if the three probes average above 60%.
- **Tank lockout**: nothing waters while the 250 L tank is below the LOW float, which protects the pump from running dry. A run in progress stops if the level drops.
- **One zone at a time**, because there's one pump. A second request waits its turn.
- **No-flow stop**: if hydro is open but the flow sensor sees no water after 45 seconds, the run stops and you get an alert (pump off, empty tank, blocked filter).
- **Leak alert**: water flowing for 2 minutes with every valve closed.
- **Controller offline alert** after 1 minute without contact.
- **Notifications** for bed watering, skips, and every problem. Hydro cycles stay quiet unless something goes wrong.
- **Rain forecast** (Open-Meteo, no API key) is shown but doesn't skip anything, because the greenhouse is covered. Set `skip_if_rain: true` on a zone if that changes.
- Optional, once relays are fitted: **automatic tank refill** from the 2,000 L tank (LOW starts, HIGH stops, 20-minute fault timeout) and **pump dry-run interlock**.

## Setting it up

Do these in order. Each step depends on the one before it passing.

### 1. Build and commission the hardware

Follow the *Hydro Greenhouse Controller Build Manual* through step 22 (valve test). The
firmware uses exactly the manual's pin plan.

### 2. Flash the ESP32

1. In Arduino IDE, install the libraries from manual step 20: Adafruit GFX Library, Adafruit SSD1306, OneWire, DallasTemperature.
2. Open `firmware/irrigation_esp32/irrigation_esp32.ino`.
3. Copy `config.example.h` to `config.h` in the same folder and fill in your WiFi name and password. Pick an `API_KEY` (any long random text) if you want one.
4. Set `FLOAT_WET_VALUE` after testing your floats by hand in the tank (manual step 12).
5. Board **WEMOS LOLIN32**, upload. The OLED shows the ESP32's IP address once it's on WiFi.
6. Check it from any computer on your WiFi: open `http://irrigation-esp32.local/api/status` (or the IP). You should see sensor readings.

The serial-monitor test from the manual still works: type `A` or `B` to pulse a valve for 2 seconds.

Later firmware updates can go over WiFi: in Arduino IDE choose the network port `irrigation-esp32`.

### 3. Install on the Raspberry Pi

```bash
git clone https://github.com/tyronemc2/pi_irrigation.git
cd pi_irrigation
sudo ./deploy/install.sh --tailscale
```

Then edit the settings and restart:

```bash
sudo nano /etc/pi_irrigation/config.yaml
sudo systemctl restart pi-irrigation
```

In `config.yaml`, at minimum set `controller.api_key` to match the firmware (or leave both
empty), `tank.float_wet_value` to match the firmware, and your soil calibration values.

To update later: `cd pi_irrigation && git pull && sudo ./deploy/install.sh`. Your config and
schedules are kept.

### 4. Put the app on your phones

The dashboard is a web app that installs like a normal app. **Android needs HTTPS** to install
it properly, which is why the installer sets up Tailscale. Tailscale is free for personal use, gives the Pi
a secure address, and lets you check the greenhouse away from home.

1. Install the Tailscale app on each phone and sign in with the same account you used on the Pi.
2. Open the HTTPS address the installer printed (like `https://raspberrypi.tail1234.ts.net`).
3. **Android (Chrome):** tap **Install app** on the dashboard, or menu ⋮ then **Install app**.
4. **iPhone (Safari):** tap Share, then **Add to Home Screen**.

On home WiFi you can also use `http://<pi-ip>:8080`. iPhones can add that to the home screen,
but on Android it becomes a plain browser shortcut rather than an app.

If the installer asks you to enable HTTPS, turn on **MagicDNS** and **HTTPS certificates** in the
Tailscale admin console (DNS page), then run the installer again.

### 5. Calibrate

- **Soil probes** (manual step 23): note each probe's raw value dry in air and in fully wet bed soil. The raw values are in `http://<pi>:8080/api/status` under `soil`. Put them in `soil_sensors` in config.yaml.
- **Flow sensor** (manual step 24): run exactly 1 litre through the hydro line and count pulses (`flow_pulses` in the ESP32 status, before and after). Set `flow.pulses_per_litre`.

### 6. Phone notifications (optional)

Install the free **ntfy** app, subscribe to a topic with a hard-to-guess name (like
`greenhouse-k7q2x9`), set `notifications.enabled: true` and the same `ntfy_topic` in
config.yaml, restart, then tap **Send test notification** on the dashboard.

### 7. Pump relays (optional, needs an electrician)

Both pumps are 230 V. The MOSFETTI can't switch mains, so each needs a relay or contactor
with a **12 V coil** (with flyback diode) rated for the motor, driven from MOSFETTI C
(transfer pump) or D (pressure pump). Once fitted and tested, set `ENABLE_REFILL` and/or
`ENABLE_PUMP_CONTROL` to 1 in the firmware's `config.h` and re-upload. Until then, the
pressure pump runs on its own pressure switch and you top up the 250 L tank by hand.

## Final commissioning checklist

Run through this with water connected and someone watching:

1. Dashboard shows **Online**, tank state matches what you see, all three probes read.
2. Water hydroponics 5 min from the dashboard: valve A opens, flow shows on the hydro card, it stops on time, Activity shows litres.
3. Water veggie bed 5 min: valve B opens, jets spray, stops on time.
4. Start hydro, then bed: bed waits, then runs after hydro.
5. Tap **Stop all water** mid-run: everything closes.
6. Close the hydro hand valve and start hydro: it stops after ~45 s with "No water flowed".
7. Lift the LOW float (or drain below it): watering refuses to start.
8. Unplug the ESP32 for 2 minutes: offline alert arrives, dashboard says offline. Plug back in: it recovers.
9. Restart the Pi (`sudo reboot`): dashboard comes back by itself.
10. Leave the default schedules running for a day and check Activity.

## Try it without hardware

```bash
pip install -r requirements.txt
python -m irrigation --config - --simulate --state /tmp/state.json
```

Open http://localhost:8080. The simulated garden has a tank, drying soil and a flow sensor.

## Troubleshooting

| Problem | Check |
|---|---|
| Dashboard says *Greenhouse offline* | ESP32 power and WiFi; open `http://irrigation-esp32.local/api/status`. If `.local` names don't work on your network, put the ESP32's IP in `controller.url`. |
| *Can't reach the Pi* | `sudo systemctl status pi-irrigation` and `journalctl -u pi-irrigation -n 50` |
| Tank always *Low* | `float_wet_value` is the wrong way round (in both config.yaml and config.h) |
| Soil % looks wrong | Recalibrate `dry_raw` / `wet_raw` for that probe |
| Hydro stops with *No water flowed* but water is running | Flow sensor wiring/divider (manual step 13), arrow direction, or `pulses_per_litre` far too high |
| Bed never waters | Soil above `skip_above_percent`, or schedules paused. Check Activity. |
| Water temp *No reading* | DS18B20 and its 4.7k pull-up (manual step 11) |

## Project layout

```
irrigation/        Pi app: config, ESP32 client and simulator, controller,
                   scheduler, forecast, notifications, Flask dashboard + PWA
firmware/          ESP32 firmware (Arduino)
deploy/            installer and systemd service
tests/             automated tests (pytest), run on GitHub for every push
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The ESP32 HTTP API: `GET /api/status`; `POST /api/valve?valve=A&seconds=600` (0 closes);
`POST /api/off`; `POST /api/refill?action=start|stop|reset`. Send `X-Api-Key` if set.
