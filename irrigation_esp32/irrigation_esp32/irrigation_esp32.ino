/*
  Greenhouse irrigation controller - ESP32 (Wemos/Lolin ESP32 OLED)

  Pin plan = Greenhouse Controller build book, step 9 (26-pin LOLIN32 + OLED).
  This board has no GPIO18, 19, 27, 32, 33 or 34.
    Soil 1      GPIO36 (SVP)  Alley Pots
    Soil 2      GPIO39 (SVN)  bed
    DS18B20     GPIO13 (4.7k from DATA to 3V3)
    Float LOW   GPIO12        Float HIGH GPIO16   (INPUT_PULLUP, other wire to GND)
                GPIO12 must be low at boot: nothing but the float goes there.
    Flow pulse  GPIO14 (via 10k/18k divider)
    MOSFETTI A  GPIO25 valve A
    MOSFETTI B  GPIO26 valve B
    MOSFETTI C  GPIO2  valve C (blue on-board LED may light while open)
    MOSFETTI D  not used (no free pin)
    OLED SDA/SCL GPIO5/4, address 0x3C
  Pump relays and automatic refill are not available on this board.

  HTTP API (used by the Raspberry Pi):
    GET  /api/status
    POST /api/valve?valve=A&seconds=600     (seconds=0 closes; A, B or C)
    POST /api/off                           close everything
    POST /api/refill                        (not available: returns 409)
  If API_KEY is set, every request needs the header  X-Api-Key: <key>

  Safety: every valve run has its own timer on this board, so valves close on
  time even if the Pi or WiFi fails. All outputs are LOW (off) at boot.

  Libraries: Adafruit GFX, Adafruit SSD1306, OneWire, DallasTemperature
  (WiFi, WebServer, ESPmDNS, ArduinoOTA ship with the ESP32 board package).
*/
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <OneWire.h>
#include <DallasTemperature.h>

#if __has_include("config.h")
#include "config.h"
#else
#error "Copy config.example.h to config.h and fill in your WiFi details"
#endif

#define FW_VERSION "1.1.0"

// ---- pins (manual step 9) ----
#define NUM_SOIL 2
#define NUM_VALVES 3
const int SOIL_PINS[NUM_SOIL] = {36, 39};
const int TEMP_PIN = 13;
const int FLOAT_LOW_PIN = 12;
const int FLOAT_HIGH_PIN = 16;
const int FLOW_PIN = 14;
const int OUT_PINS[NUM_VALVES] = {25, 26, 2};  // MOSFETTI A, B, C
const char VALVE_NAMES[NUM_VALVES] = {'A', 'B', 'C'};
const int OLED_SDA = 5, OLED_SCL = 4;

Adafruit_SSD1306 display(128, 64, &Wire, -1);
bool displayOk = false;
OneWire oneWire(TEMP_PIN);
DallasTemperature temps(&oneWire);
WebServer server(80);

volatile uint32_t flowPulses = 0;
void IRAM_ATTR flowISR() { flowPulses++; }

// ---- state ----
unsigned long valveUntil[NUM_VALVES] = {0, 0, 0};   // millis() deadline; 0 = closed
bool valveOn[NUM_VALVES] = {false, false, false};

int soilRaw[NUM_SOIL] = {0, 0};
float tempC = -127.0;
int floatLow = 1, floatHigh = 1;        // debounced readings
int rawLow = 1, rawHigh = 1;
unsigned long lowChangedAt = 0, highChangedAt = 0;


unsigned long lastSensorRead = 0, lastDisplay = 0, lastWifiCheck = 0;

// ---------------------------------------------------------------------------
void setOutput(int ch, bool on) {
  digitalWrite(OUT_PINS[ch], on ? HIGH : LOW);
}

void allOff() {
  for (int i = 0; i < NUM_VALVES; i++) { valveOn[i] = false; valveUntil[i] = 0; setOutput(i, false); }
}

bool lowWet()  { return floatLow == FLOAT_WET_VALUE; }
bool highWet() { return floatHigh == FLOAT_WET_VALUE; }

void openValve(int i, unsigned long seconds) {
  if (seconds == 0) {
    valveOn[i] = false; valveUntil[i] = 0;
    return;
  }
  if (seconds > MAX_VALVE_SECONDS) seconds = MAX_VALVE_SECONDS;
  valveOn[i] = true;
  valveUntil[i] = millis() + seconds * 1000UL;
}

// ---------------------------------------------------------------------------
void readSensors() {
  for (int s = 0; s < NUM_SOIL; s++) {
    uint32_t sum = 0;
    for (int k = 0; k < 8; k++) sum += analogRead(SOIL_PINS[s]);
    soilRaw[s] = sum / 8;
  }
  // DS18B20 runs non-blocking: read last conversion, start the next one.
  float t = temps.getTempCByIndex(0);
  tempC = t;
  temps.requestTemperatures();
}

void debounceFloats() {
  unsigned long now = millis();
  int l = digitalRead(FLOAT_LOW_PIN), h = digitalRead(FLOAT_HIGH_PIN);
  if (l != rawLow) { rawLow = l; lowChangedAt = now; }
  if (h != rawHigh) { rawHigh = h; highChangedAt = now; }
  if (now - lowChangedAt > 2000) floatLow = rawLow;     // 2 s stable = real
  if (now - highChangedAt > 2000) floatHigh = rawHigh;
}

void updateValves() {
  unsigned long now = millis();
  for (int i = 0; i < NUM_VALVES; i++) {
    if (valveOn[i] && (long)(now - valveUntil[i]) >= 0) { valveOn[i] = false; valveUntil[i] = 0; }
    setOutput(i, valveOn[i]);
  }
}

// ---------------------------------------------------------------------------
bool authorised() {
  if (strlen(API_KEY) == 0) return true;
  if (server.header("X-Api-Key") == API_KEY) return true;
  server.send(401, "application/json", "{\"error\":\"bad api key\"}");
  return false;
}

unsigned long remainingS(int i) {
  if (!valveOn[i]) return 0;
  return (valveUntil[i] - millis() + 999) / 1000;
}

String statusJson() {
  noInterrupts(); uint32_t pulses = flowPulses; interrupts();
  String j = "{";
  j += "\"fw\":\"" FW_VERSION "\",";
  j += "\"uptime_s\":" + String(millis() / 1000) + ",";
  j += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  // Three slots so the Pi's soil ids 1-3 stay valid; slot 3 has no probe (0 = not fitted).
  j += "\"soil_raw\":[" + String(soilRaw[0]) + "," + String(soilRaw[1]) + ",0],";
  j += "\"temp_c\":" + String(tempC, 2) + ",";
  j += "\"float_low\":" + String(floatLow) + ",\"float_high\":" + String(floatHigh) + ",";
  j += "\"flow_pulses\":" + String(pulses) + ",";
  j += "\"valves\":{";
  for (int i = 0; i < NUM_VALVES; i++) {
    if (i) j += ",";
    j += "\"" + String(VALVE_NAMES[i]) + "\":{\"on\":" + String(valveOn[i] ? "true" : "false") +
         ",\"remaining_s\":" + String(remainingS(i)) + "}";
  }
  j += "},";
  j += "\"pump\":{\"enabled\":false,\"on\":false},";
  j += "\"refill\":{\"enabled\":false,\"running\":false,\"running_s\":0,\"fault\":false,\"fault_reason\":\"\"}";
  j += "}";
  return j;
}

void handleStatus() {
  if (!authorised()) return;
  server.send(200, "application/json", statusJson());
}

void handleValve() {
  if (!authorised()) return;
  String v = server.arg("valve");
  v.toUpperCase();
  long secs = server.arg("seconds").toInt();
  int idx = v == "A" ? 0 : v == "B" ? 1 : v == "C" ? 2 : -1;
  if (idx < 0 || secs < 0) {
    server.send(400, "application/json", "{\"error\":\"valve must be A, B or C, seconds >= 0\"}");
    return;
  }
  openValve(idx, secs);
  updateValves();
  server.send(200, "application/json", statusJson());
}

void handleOff() {
  if (!authorised()) return;
  allOff();
  server.send(200, "application/json", statusJson());
}

void handleRefill() {
  if (!authorised()) return;
  server.send(409, "application/json", "{\"error\":\"refill is not available on this board\"}");
}

// ---------------------------------------------------------------------------
void drawDisplay() {
  if (!displayOk) return;
  display.clearDisplay();
  display.setCursor(0, 0);
  if (WiFi.status() == WL_CONNECTED) display.println(WiFi.localIP());
  else display.println("WiFi connecting...");
  display.print("A:"); display.print(valveOn[0] ? "ON " : "off");
  display.print(" B:"); display.print(valveOn[1] ? "ON " : "off");
  display.print(" C:"); display.println(valveOn[2] ? "ON" : "off");
  display.print("S "); display.print(soilRaw[0]); display.print(" "); display.println(soilRaw[1]);
  display.print("T "); display.print(tempC, 1); display.println("C");
  display.print("Tank "); display.println(highWet() ? "FULL" : lowWet() ? "OK" : "LOW");
  display.display();
}

void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void setup() {
  // Outputs off before anything else (GPIO2 must stay low for Valve C to stay shut).
  for (int ch = 0; ch < NUM_VALVES; ch++) { digitalWrite(OUT_PINS[ch], LOW); pinMode(OUT_PINS[ch], OUTPUT); }
  Serial.begin(115200);
  pinMode(FLOAT_LOW_PIN, INPUT_PULLUP);
  pinMode(FLOAT_HIGH_PIN, INPUT_PULLUP);
  pinMode(FLOW_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), flowISR, RISING);
  analogSetAttenuation(ADC_11db);
  rawLow = floatLow = digitalRead(FLOAT_LOW_PIN);
  rawHigh = floatHigh = digitalRead(FLOAT_HIGH_PIN);

  temps.begin();
  temps.setWaitForConversion(false);
  temps.requestTemperatures();

  Wire.begin(OLED_SDA, OLED_SCL);
  displayOk = display.begin(SSD1306_SWITCHCAPVCC, 0x3C, false, false);
  if (displayOk) { display.setTextColor(SSD1306_WHITE); display.setTextSize(1); }

  setupWifi();
  if (MDNS.begin(HOSTNAME)) MDNS.addService("http", "tcp", 80);
  ArduinoOTA.setHostname(HOSTNAME);
  if (strlen(API_KEY)) ArduinoOTA.setPassword(API_KEY);
  ArduinoOTA.onStart([]() { allOff(); });
  ArduinoOTA.begin();

  const char *headers[] = {"X-Api-Key"};
  server.collectHeaders(headers, 1);
  server.on("/api/status", HTTP_GET, handleStatus);
  server.on("/api/valve", HTTP_POST, handleValve);
  server.on("/api/off", HTTP_POST, handleOff);
  server.on("/api/refill", HTTP_POST, handleRefill);
  server.onNotFound([]() { server.send(404, "application/json", "{\"error\":\"not found\"}"); });
  server.begin();

  Serial.println("Greenhouse irrigation controller " FW_VERSION " ready.");
  Serial.println("Serial test: type A, B or C to pulse a valve for 2 seconds.");
}

void loop() {
  unsigned long now = millis();
  server.handleClient();
  ArduinoOTA.handle();

  if (Serial.available()) {  // commissioning test, same as the manual's sketch
    char ch = Serial.read();
    if (ch == 'A' || ch == 'a') openValve(0, 2);
    if (ch == 'B' || ch == 'b') openValve(1, 2);
    if (ch == 'C' || ch == 'c') openValve(2, 2);
  }

  debounceFloats();
  updateValves();

  if (now - lastSensorRead >= 1000) { lastSensorRead = now; readSensors(); }
  if (now - lastDisplay >= 1000) { lastDisplay = now; drawDisplay(); }
  if (now - lastWifiCheck >= 30000) {
    lastWifiCheck = now;
    if (WiFi.status() != WL_CONNECTED) { WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASSWORD); }
  }
}
