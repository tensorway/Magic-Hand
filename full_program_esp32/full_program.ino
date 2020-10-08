#include "BluetoothSerial.h" //Header File for Serial Bluetooth, will be added by default into Arduino
#include "esp32-hal-ledc.h"

BluetoothSerial ESP_BT; //Object for Bluetooth

#define COUNT_LOW 2100
#define COUNT_HIGH 7500
#define TIMER_WIDTH 16
#define N_MOTORS 3
#define LED_BUILTIN 2
#define BUFFLEN_FULL 4
#define BLINK_DELTA 200 //in millis
#define BLINK_DELTA_T_LAST_COMM 1000  //blink if have good connection in millis


int incoming;
byte buff[BUFFLEN_FULL] = {};
int bufflen = 0;
int motor_pins[N_MOTORS] = {12, 14, 27};
int t_last_received_comm = millis();


void setup() {
  Serial.begin(9600); //Start Serial monitor in 9600
  ESP_BT.begin("ESP32_ROBOTIC_ARM"); //Name of your Bluetooth Signal
  Serial.println("Bluetooth Device is Ready to Pair");
  pinMode (LED_BUILTIN, OUTPUT);//Specify that LED pin is output

  for (int i = 0; i < N_MOTORS; i++)
  {
    ledcSetup(i, 50, TIMER_WIDTH); // channel i, 50 Hz, 16-bit width
    ledcAttachPin(motor_pins[i], i);   // GPIO assigned to channel i
  }
}


void loop() {
  if (ESP_BT.available()) //Check if we receive anything from Bluetooth
  {
    incoming = ESP_BT.read(); //Read what we recevive
    if (incoming > 30 && incoming < 60) {
      t_last_received_comm = millis();
      buff[bufflen] = incoming;
      bufflen ++;
      //Serial.print("Received:");
      //Serial.println(incoming);
      if (incoming == '!')
        clean_buff();
      else if (bufflen == BUFFLEN_FULL) {
        int motor_id = get_motor_id();
        int deg = get_motor_deg();
        int comm = deg2command(deg);
        //Serial.print("Moving motor "); Serial.print(motor_id); Serial.print("   to degree "); Serial.print(deg); Serial.print("   using command "); Serial.println(comm);
        ledcWrite(motor_id, comm);
      }
    }
  }
  delay(1);
  blink_if_called_recently();
}





void clean_buff() {
  for (int i = 0; i < 5; i++)
    buff[i] = 0;
  bufflen = 0;
}
int get_motor_id() {
  return buff[0] - '0';
}
int get_motor_deg() {
  //convert char array to int
  int mul = 1;
  int sum = 0;
  for (int i = BUFFLEN_FULL - 1; i >= 1; i--) {
    sum += (buff[i] - '0') * mul;
    mul *= 10;
  }
  return sum;
}
int deg2command(int deg) {
  int range = COUNT_HIGH - COUNT_LOW;
  return range * deg / 360 + COUNT_LOW;
}
void blink_if_called_recently() {
  static bool led_state = true;
  static int t_last_led_state_change = 0;
  if (millis() - t_last_led_state_change > BLINK_DELTA) {
    led_state != led_state;
    t_last_led_state_change = millis();
  }
  if (millis() - t_last_received_comm < BLINK_DELTA_T_LAST_COMM) {
    digitalWrite(LED_BUILTIN, led_state);
  }
  else
    digitalWrite(LED_BUILTIN, false);
}
