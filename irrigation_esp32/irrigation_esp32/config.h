// Copy this file to config.h and fill in your details.
// config.h is ignored by git so your WiFi password never ends up on GitHub.
#pragma once

#define WIFI_SSID      "CopperClock-WiFi"
#define WIFI_PASSWORD  "W1fi@CCW2020"
#define HOSTNAME       "veggie-esp32"   // reachable as http://irrigation-esp32.local

// Shared secret with the Pi (controller.api_key in config.yaml). Also the OTA password.
// Leave "" to disable (only on a trusted home network).
#define API_KEY        "Morganjam14@vs"

// Float switches read 0 or 1. Which value means "water is up at this float"?
// Lift each float by hand: the number it shows when lifted goes here.
// Must match tank.float_wet_value in the Pi's config.yaml.
#define FLOAT_WET_VALUE 1

// Safety cap for any single valve run, in seconds.
#define MAX_VALVE_SECONDS 1800

// Pump relays and automatic tank refill are not available on this
// 26-pin board: MOSFETTI C drives Valve C (GPIO2) and there is no free pin for D.
