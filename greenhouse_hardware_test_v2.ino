// greenhouse_hardware_test_v2.ino
// Hardware test for the Greenhouse Controller build book (pin plan = Step 9).
// Board: WEMOS LOLIN32 (26-pin ESP32 + OLED). Serial Monitor: 115200 baud.
// Type A + Enter to open Valve A for 2 s, B for Valve B, R to reset the flow count.

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ---- Pin plan (Step 9) ----
const int PIN_SOIL1      = 36;  // SVP  - Soil 1, Alley Pots
const int PIN_SOIL2      = 39;  // SVN  - Soil 2, bed
const int PIN_TEMP       = 13;  // DS18B20 data + 4.7k to 3V3
const int PIN_FLOAT_LOW  = 12;  // float to GND (strapping pin: nothing else here)
const int PIN_FLOAT_HIGH = 16;  // float to GND
const int PIN_FLOW       = 14;  // flow sensor via 10k/18k divider
const int PIN_VALVE_A    = 25;  // MOSFETTI A
const int PIN_VALVE_B    = 26;  // MOSFETTI B
const int OLED_SDA       = 5;
const int OLED_SCL       = 4;

const unsigned long VALVE_TEST_MS = 2000;
const unsigned long REPORT_MS     = 1000;

Adafruit_SSD1306 oled(128, 64, &Wire, -1);
OneWire oneWire(PIN_TEMP);
DallasTemperature tempSensor(&oneWire);
bool oledOk = false;

volatile uint32_t flowPulses = 0;
void IRAM_ATTR onFlowPulse() { flowPulses++; }

uint32_t readFlow() {
  noInterrupts();
  uint32_t p = flowPulses;
  interrupts();
  return p;
}

void showOled(int s1, int s2, float t, int low, int high, uint32_t flow, const char* status) {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);
  oled.printf("Soil1 %4d\nSoil2 %4d\n", s1, s2);
  if (t == DEVICE_DISCONNECTED_C) oled.println("Temp  --");
  else oled.printf("Temp  %.1f C\n", t);
  oled.printf("LOW %d  HIGH %d\nFlow  %lu\n", low, high, (unsigned long)flow);
  oled.println(status);
  oled.display();
}

void testValve(int pin, const char* name) {
  Serial.printf("Testing Valve %s\n", name);
  showOled(0, 0, DEVICE_DISCONNECTED_C, 0, 0, readFlow(), name[0] == 'A' ? "VALVE A OPEN" : "VALVE B OPEN");
  digitalWrite(pin, HIGH);
  delay(VALVE_TEST_MS);
  digitalWrite(pin, LOW);
  Serial.println("Valve OFF");
}

void setup() {
  // Valves first: hold both MOSFETTI inputs LOW so nothing opens at boot.
  digitalWrite(PIN_VALVE_A, LOW);
  digitalWrite(PIN_VALVE_B, LOW);
  pinMode(PIN_VALVE_A, OUTPUT);
  pinMode(PIN_VALVE_B, OUTPUT);

  Serial.begin(115200);
  delay(300);

  analogReadResolution(12);           // 0..4095
  analogSetAttenuation(ADC_11db);     // full 0..3.3V range

  pinMode(PIN_FLOAT_LOW, INPUT_PULLUP);
  pinMode(PIN_FLOAT_HIGH, INPUT_PULLUP);

  pinMode(PIN_FLOW, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_FLOW), onFlowPulse, FALLING);

  tempSensor.begin();

  Wire.begin(OLED_SDA, OLED_SCL);
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, 0x3C);

  Serial.println();
  Serial.println("Greenhouse hardware test v2");
  Serial.printf("DS18B20 found: %d\n", tempSensor.getDeviceCount());
  Serial.printf("OLED: %s\n", oledOk ? "OK" : "not found");
  Serial.println("Commands: A = Valve A 2 s, B = Valve B 2 s, R = reset flow count");
}

void loop() {
  // Commands from the Serial Monitor
  while (Serial.available()) {
    char c = toupper(Serial.read());
    if (c == 'A') testValve(PIN_VALVE_A, "A");
    else if (c == 'B') testValve(PIN_VALVE_B, "B");
    else if (c == 'R') { noInterrupts(); flowPulses = 0; interrupts(); Serial.println("Flow count reset"); }
  }

  // One report line every second
  static unsigned long lastReport = 0;
  if (millis() - lastReport >= REPORT_MS) {
    lastReport = millis();

    int soil1 = analogRead(PIN_SOIL1);
    int soil2 = analogRead(PIN_SOIL2);

    tempSensor.requestTemperatures();
    float tempC = tempSensor.getTempCByIndex(0);

    int low  = digitalRead(PIN_FLOAT_LOW);
    int high = digitalRead(PIN_FLOAT_HIGH);
    uint32_t flow = readFlow();

    Serial.printf("Soil: %d, %d | Temp ", soil1, soil2);
    if (tempC == DEVICE_DISCONNECTED_C) Serial.print("--");
    else Serial.print(tempC, 1);
    Serial.printf(" | LOW %d HIGH %d | Flow %lu\n", low, high, (unsigned long)flow);

    showOled(soil1, soil2, tempC, low, high, flow, "Valves OFF");
  }
}
