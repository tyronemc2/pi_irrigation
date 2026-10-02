/*
  Greenhouse irrigation controller - ESP32 (Wemos/Lolin ESP32 OLED)

  Pin plan follows the Hydro Greenhouse Controller Build Manual v1.0, step 9:
    Soil 1/2/3  GPIO36/39/34   DS18B20  GPIO27 (4.7k pull-up)
    Float LOW   GPIO32         Float HIGH GPIO33   (INPUT_PULLUP, other wire to GND)
    Flow pulse  GPIO14 (via 10k/18k divider)
    MOSFETTI A  GPIO25 valve A (hydroponics)
    MOSFETTI B  GPIO26 valve B (veggie bed)
    MOSFETTI C  GPIO18 transfer-pump relay (refill)   - optional
    MOSFETTI D  GPIO19 main-pump relay                - optional
    OLED SDA/SCL GPIO5/4, address 0x3C

  HTTP API (used by the Raspberry Pi):
    GET  /api/status
    POST /api/valve?valve=A&seconds=600     (seconds=0 closes; A or B)
    POST /api/off                           close everything
    POST /api/refill?action=reset|start|stop
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

#define FW_VERSION "1.0.0"

// ---- pins (manual step 9) ----
const int SOIL_PINS[3] = {36, 39, 34};
const int TEMP_PIN = 27;
const int FLOAT_LOW_PIN = 32;
const int FLOAT_HIGH_PIN = 33;
const int FLOW_PIN = 14;
const int OUT_PINS[4] = {25, 26, 18, 19};  // MOSFETTI A, B, C, D
const int OLED_SDA = 5, OLED_SCL = 4;

enum { CH_A = 0, CH_B = 1, CH_REFILL = 2, CH_PUMP = 3 };

Adafruit_SSD1306 display(128, 64, &Wire, -1);
bool displayOk = false;
OneWire oneWire(TEMP_PIN);
DallasTemperature temps(&oneWire);
WebServer server(80);

volatile uint32_t flowPulses = 0;
void IRAM_ATTR flowISR() { flowPulses++; }

// ---- state ----
unsigned long valveUntil[2] = {0, 0};   // millis() deadline; 0 = closed
bool valveOn[2] = {false, false};
unsigned long valveOpenedAt[2] = {0, 0};
bool outState[4] = {false, false, false, false};

int soilRaw[3] = {0, 0, 0};
float tempC = -127.0;
int floatLow = 1, floatHigh = 1;        // debounced readings
int rawLow = 1, rawHigh = 1;
unsigned long lowChangedAt = 0, highChangedAt = 0;

bool refilling = false, refillFault = false;
unsigned long refillStartedAt = 0, refillStoppedAt = 0;
unsigned long lowDrySince = 0, highDrySince = 0;
String refillFaultReason = "";
bool pumpOn = false;

unsigned long lastSensorRead = 0, lastDisplay = 0, lastWifiCheck = 0;

// ---------------------------------------------------------------------------
void setOutput(int ch, bool on) {
  outState[ch] = on;
  digitalWrite(OUT_PINS[ch], on ? HIGH : LOW);
}

void allOff() {
  for (int i = 0; i < 2; i++) { valveOn[i] = false; valveUntil[i] = 0; }
  for (int ch = 0; ch < 4; ch++) setOutput(ch, false);
  pumpOn = false;
  if (refilling) { refilling = false; refillStoppedAt = millis(); }
}

bool lowWet()  { return floatLow == FLOAT_WET_VALUE; }
bool highWet() { return floatHigh == FLOAT_WET_VALUE; }

void openValve(int i, unsigned long seconds) {
  if (seconds == 0) {
    valveOn[i] = false; valveUntil[i] = 0;
    return;
  }
  if (seconds > MAX_VALVE_SECONDS) seconds = MAX_VALVE_SECONDS;
  if (!valveOn[i]) valveOpenedAt[i] = millis();
  valveOn[i] = true;
  valveUntil[i] = millis() + seconds * 1000UL;
}

// ---------------------------------------------------------------------------
void readSensors() {
  for (int s = 0; s < 3; s++) {
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
  for (int i = 0; i < 2; i++) {
    if (valveOn[i] && (long)(now - valveUntil[i]) >= 0) { valveOn[i] = false; valveUntil[i] = 0; }
    setOutput(i, valveOn[i]);
  }
}

void updateRefill() {
#if ENABLE_REFILL
  unsigned long now = millis();
  if (refilling) {
    if (highWet()) {
      refilling = false; refillStoppedAt = now;
    } else if (now - refillStartedAt > REFILL_MAX_SECONDS * 1000UL) {
      refilling = false; refillStoppedAt = now;
      refillFault = true;
      refillFaultReason = "Tank didn't reach HIGH in time";
    }
  } else if (!refillFault) {
    if (!lowWet()) { if (lowDrySince == 0) lowDrySince = now; } else lowDrySince = 0;
    if (!highWet()) { if (highDrySince == 0) highDrySince = now; } else highDrySince = 0;
    bool rested = refillStoppedAt == 0 || now - refillStoppedAt > REFILL_REST_SECONDS * 1000UL;
    bool low = lowDrySince && now - lowDrySince > 5000;
    bool topUp = REFILL_TOPUP && highDrySince && now - highDrySince > REFILL_TOPUP_DELAY_SECONDS * 1000UL;
    if ((low || topUp) && rested && !highWet()) {
      refilling = true; refillStartedAt = now;
    }
  }
  setOutput(CH_REFILL, refilling);
#endif
}

void updatePump() {
#if ENABLE_PUMP_CONTROL
  unsigned long now = millis();
  bool want = false;
  for (int i = 0; i < 2; i++)
    if (valveOn[i] && now - valveOpenedAt[i] >= PUMP_START_DELAY_MS) want = true;
  if (!lowWet()) want = false;  // dry-run protection
  pumpOn = want;
  setOutput(CH_PUMP, pumpOn);
#endif
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
  j += "\"soil_raw\":[" + String(soilRaw[0]) + "," + String(soilRaw[1]) + "," + String(soilRaw[2]) + "],";
  j += "\"temp_c\":" + String(tempC, 2) + ",";
  j += "\"float_low\":" + String(floatLow) + ",\"float_high\":" + String(floatHigh) + ",";
  j += "\"flow_pulses\":" + String(pulses) + ",";
  j += "\"valves\":{";
  j += "\"A\":{\"on\":" + String(valveOn[0] ? "true" : "false") + ",\"remaining_s\":" + String(remainingS(0)) + "},";
  j += "\"B\":{\"on\":" + String(valveOn[1] ? "true" : "false") + ",\"remaining_s\":" + String(remainingS(1)) + "}},";
  j += "\"pump\":{\"enabled\":" + String(ENABLE_PUMP_CONTROL ? "true" : "false") +
       ",\"on\":" + String(pumpOn ? "true" : "false") + "},";
  j += "\"refill\":{\"enabled\":" + String(ENABLE_REFILL ? "true" : "false") +
       ",\"running\":" + String(refilling ? "true" : "false") +
       ",\"running_s\":" + String(refilling ? (millis() - refillStartedAt) / 1000 : 0) +
       ",\"fault\":" + String(refillFault ? "true" : "false") +
       ",\"fault_reason\":\"" + refillFaultReason + "\"}";
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
  int idx = v == "A" ? 0 : v == "B" ? 1 : -1;
  if (idx < 0 || secs < 0) {
    server.send(400, "application/json", "{\"error\":\"valve must be A or B, seconds >= 0\"}");
    return;
  }
  openValve(idx, secs);
  updateValves(); updatePump();
  server.send(200, "application/json", statusJson());
}

void handleOff() {
  if (!authorised()) return;
  allOff();
  server.send(200, "application/json", statusJson());
}

void handleRefill() {
  if (!authorised()) return;
  String a = server.arg("action");
  if (a == "reset") { refillFault = false; refillFaultReason = ""; refillStoppedAt = 0; }
  else if (a == "stop") { if (refilling) { refilling = false; refillStoppedAt = millis(); } }
  else if (a == "start") {
    if (refillFault) { server.send(409, "application/json", "{\"error\":\"refill fault - reset first\"}"); return; }
    if (highWet()) { server.send(409, "application/json", "{\"error\":\"tank already full\"}"); return; }
    if (ENABLE_REFILL && !refilling) { refilling = true; refillStartedAt = millis(); }
  } else {
    server.send(400, "application/json", "{\"error\":\"action must be reset, start or stop\"}");
    return;
  }
  updateRefill();
  server.send(200, "application/json", statusJson());
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
  display.print(" P:"); display.println(pumpOn ? "ON" : "off");
  display.print("S "); display.print(soilRaw[0]); display.print(" ");
  display.print(soilRaw[1]); display.print(" "); display.println(soilRaw[2]);
  display.print("T "); display.print(tempC, 1); display.println("C");
  display.print("Tank "); display.print(highWet() ? "FULL" : lowWet() ? "OK" : "LOW");
  if (refillFault) display.print(" FAULT");
  else if (refilling) display.print(" filling");
  display.println();
  display.display();
}

void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void setup() {
  // Outputs off before anything else.
  for (int ch = 0; ch < 4; ch++) { pinMode(OUT_PINS[ch], OUTPUT); digitalWrite(OUT_PINS[ch], LOW); }
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
  Serial.println("Serial test: type A or B to pulse a valve for 2 seconds.");
}

void loop() {
  unsigned long now = millis();
  server.handleClient();
  ArduinoOTA.handle();

  if (Serial.available()) {  // commissioning test, same as the manual's sketch
    char ch = Serial.read();
    if (ch == 'A' || ch == 'a') openValve(0, 2);
    if (ch == 'B' || ch == 'b') openValve(1, 2);
  }

  debounceFloats();
  updateValves();
  updateRefill();
  updatePump();

  if (now - lastSensorRead >= 1000) { lastSensorRead = now; readSensors(); }
  if (now - lastDisplay >= 1000) { lastDisplay = now; drawDisplay(); }
  if (now - lastWifiCheck >= 30000) {
    lastWifiCheck = now;
    if (WiFi.status() != WL_CONNECTED) { WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASSWORD); }
  }
}
