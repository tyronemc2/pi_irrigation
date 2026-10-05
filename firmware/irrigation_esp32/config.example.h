// Copy this file to config.h and fill in your details.
// config.h is ignored by git so your WiFi password never ends up on GitHub.
#pragma once

#define WIFI_SSID      "your-wifi-name"
#define WIFI_PASSWORD  "your-wifi-password"
#define HOSTNAME       "irrigation-esp32"   // reachable as http://irrigation-esp32.local

// Shared secret with the Pi (controller.api_key in config.yaml). Also the OTA password.
// Leave "" to disable (only on a trusted home network).
#define API_KEY        ""

// Float switches read 0 or 1. Which value means "water is up at this float"?
// Test by hand in the tank (manual step 12) and set this to match.
#define FLOAT_WET_VALUE 0

// Safety cap for any single valve run, in seconds.
#define MAX_VALVE_SECONDS 1800

// ---- Channels C and D: the two 230 V pumps ----
// SAFETY: the MOSFETTI switches 12 V DC only. Each pump needs a relay/contactor
// with a 12 V coil (built-in flyback diode) rated for the pump motor, driven
// from MOSFETTI C or D. Mains wiring must be done by an electrician.
// Leave these at 0 until that hardware is fitted.

// Channel C (GPIO18): transfer pump, 2,000 L -> 250 L.
// 1 = refill automatically, always stopping at the HIGH float.
#define ENABLE_REFILL 0
// When to start refilling:
//  1 = top-up: once the tank has been below HIGH for REFILL_TOPUP_DELAY_SECONDS.
//      Keeps the tank near full so a bed watering never runs it down to LOW
//      (LOW then acts only as the pump-protection cutoff). Recommended.
//  0 = only when the LOW float goes dry.
#define REFILL_TOPUP 1
#define REFILL_TOPUP_DELAY_SECONDS 120
// If HIGH isn't reached in this time, stop and raise a fault (empty rain tank,
// stuck float, blocked pipe). Reset from the dashboard.
#define REFILL_MAX_SECONDS 1200
// Minimum rest between refills, seconds (stops rapid on/off at the float).
#define REFILL_REST_SECONDS 120

// Channel D (GPIO19): main pressure pump.
// 1 = pump may only run while valve A or B is open and the tank is above LOW
// (dry-run protection). With 0, the pump runs on its own pressure switch.
#define ENABLE_PUMP_CONTROL 0
// Open the valve first, then start the pump after this delay (milliseconds).
#define PUMP_START_DELAY_MS 1500
